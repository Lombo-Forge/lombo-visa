"""The self-service travel API behind `lombo-visa api` (docs/contracts.md, docs/security.md).

Two routes: `GET /v1/travel/{user_id}` (is a window open, when may a new one be made) and `POST /v1/travel`
(a person opens their own window). Every request needs `Authorization: Bearer <token>`. The caller (a page
service such as Shelf) is trusted to say WHO is asking; this module checks the token, the shape of the request
and the rules, and never lets a request make an administrator window, a country window or a longer one than
`SELF_MAX_DAYS`. No CORS headers on purpose: browsers never call this.
"""

import hmac
import json
import re
import socket
import sys
import threading
import time
from collections import deque
from collections.abc import Callable
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import urlsplit

from . import audit, core, state
from .config import Config
from .jellyfin import Jellyfin, JellyfinError

MAX_BODY = 4096  # bytes of request body
MAX_FAILURES = 10  # bad or missing tokens per client per window, then 429
WINDOW = 60.0  # seconds
MAX_CLIENTS = 1024  # failure records kept at once
PASSWORD_TRIES, PASSWORD_WINDOW = 2, 3600.0  # wrong passwords per account per hour
TIMEOUT = 10.0  # seconds a client may stall on one request

Reply = tuple[int, dict[str, Any], dict[str, str] | None]  # status, JSON body, extra headers


def fail(code: int, message: str, extra: dict[str, str] | None = None) -> Reply:
    return code, {"ok": False, "message": message}, extra


class FailureLimiter:
    """Counts bad tries per client address. Asked before the token is compared, so guessing is slowed
    for everyone, a right guess included."""

    def __init__(self, clock: Callable[[], float] = time.monotonic) -> None:
        self.clock = clock
        self.failures: dict[str, deque[float]] = {}
        self.lock = threading.Lock()

    def _recent(self, client: str, now: float) -> deque[float]:
        times = self.failures.get(client)
        if times is not None:
            while times and now - times[0] >= WINDOW:
                times.popleft()
            if not times:
                del self.failures[client]
        return times if times is not None and client in self.failures else deque()

    def blocked(self, client: str) -> bool:
        with self.lock:
            return len(self._recent(client, self.clock())) >= MAX_FAILURES

    def record(self, client: str) -> None:
        with self.lock:
            now = self.clock()
            times = self._recent(client, now)
            times.append(now)
            self.failures[client] = times
            if len(self.failures) > MAX_CLIENTS:  # the oldest client is forgotten; memory stays bounded
                del self.failures[next(iter(self.failures))]


def parse_user_id(raw: str) -> str | None:
    """A Jellyfin user id as 32 lower-case hex characters (dashes accepted), or None."""
    text = core.clean_id(raw)
    return text if re.fullmatch(r"[0-9a-f]{32}", text) and re.fullmatch(r"[0-9a-fA-F-]{32,36}", raw) else None


def strict_json(raw: bytes) -> tuple[Any, str]:
    """(value, "") or (None, why). No duplicate keys, no NaN."""

    def no_duplicates(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        out: dict[str, Any] = {}
        for key, value in pairs:
            if key in out:
                raise ValueError("duplicate key")
            out[key] = value
        return out

    def refuse(name: str) -> Any:
        raise ValueError(name)

    try:
        data = json.loads(raw.decode("utf-8"), object_pairs_hook=no_duplicates, parse_constant=refuse)
    except (ValueError, RecursionError):  # UnicodeDecodeError is a ValueError
        return None, "the body must be strict JSON"
    return data, ""


def parse_open_body(raw: bytes, password: bool = False) -> tuple[tuple[str, int, str] | None, str]:
    """((user_id, days, password), "") for a valid body, else (None, why). Strict: exactly the expected keys."""
    data, why = strict_json(raw)
    if why:
        return None, why
    keys = {"user_id", "days", "password"} if password else {"user_id", "days"}
    if not isinstance(data, dict) or set(data) != keys:
        return None, f"the body must have exactly these keys: {', '.join(sorted(keys))}"
    given = data.get("password", "")
    if not isinstance(given, str) or len(given) > 200:
        return None, "password must be a string of at most 200 characters"
    days = data["days"]
    if not isinstance(data["user_id"], str) or not isinstance(days, int) or isinstance(days, bool):
        return None, "user_id must be a string and days a whole number"
    user_id = parse_user_id(data["user_id"])
    if user_id is None:
        return None, "user_id must be a Jellyfin user id (32 hex characters)"
    return (user_id, days, given), ""


def parse_admin_body(raw: bytes) -> tuple[tuple[str, int, str | None] | None, str]:
    """((user_id, days, country), "") for a valid admin body, else (None, why)."""
    data, why = strict_json(raw)
    if why:
        return None, why
    if not isinstance(data, dict) or not {"user_id", "days"} <= set(data) <= {"user_id", "days", "country"}:
        return None, 'the body must be {"user_id": "...", "days": 7} with an optional "country"'
    days, country, raw_id = data["days"], data.get("country"), data["user_id"]
    if not isinstance(raw_id, str) or not isinstance(days, int) or isinstance(days, bool):
        return None, "user_id must be a string and days a whole number"
    if country is not None and not isinstance(country, str):
        return None, "country must be two letters or left out"
    user_id = parse_user_id(raw_id)
    if user_id is None:
        return None, "user_id must be a Jellyfin user id (32 hex characters)"
    return (user_id, days, country or None), ""


def _end(w: core.Window) -> float:
    """When a window stopped counting: its end, or the moment it was revoked if that came first."""
    return w.ends_at if w.revoked_at is None else min(w.ends_at, w.revoked_at)


class Travel:
    """The rules. Reads and writes the state file (under its lock for writes); asks Jellyfin who a user is."""

    def __init__(self, cfg: Config, clock: Callable[[], float] = time.time) -> None:
        self.cfg, self.clock = cfg, clock
        self.wrong: dict[str, deque[float]] = {}  # wrong passwords per account; under 3, Jellyfin's usual lockout
        self.wrong_lock = threading.Lock()

    def _password_refusal(self, user_id: str, name: str, password: str) -> Reply | None:
        with self.wrong_lock:  # one check at a time, so parallel guesses cannot all slip under the limit
            now = self.clock()
            tries = self.wrong.setdefault(user_id, deque())
            while tries and now - tries[0] >= PASSWORD_WINDOW:
                tries.popleft()
            if len(tries) >= PASSWORD_TRIES:
                return fail(429, "too many wrong passwords; wait an hour")
            if password and Jellyfin(self.cfg.jellyfin_url, self.cfg.jellyfin_api_key).check_password(name, password):
                return None
            tries.append(now)
            return fail(422, "that password is not right")

    def _view(self, st: dict[str, Any], user_id: str, now: float) -> dict[str, Any]:
        mine = [w for w in state.windows(st) if w.user_id == user_id]
        open_now = [w for w in mine if w.revoked_at is None and w.ends_at > now]
        best = max(open_now, key=lambda w: w.ends_at, default=None)
        own = state.self_ids(st)
        last = max((_end(w) for w in mine if w.id in own), default=None)
        cool = self.cfg.self_cooldown_days
        next_allowed = last + cool * core.DAY if last is not None and cool > 0 else None
        return {
            "active": {"id": best.id, "ends": int(best.ends_at)} if best else None,
            "next_allowed": int(next_allowed) if next_allowed is not None and next_allowed > now else None,
            "max_days": self.cfg.self_max_days,
        }

    def status(self, user_id: str) -> Reply:
        st = state.load(self.cfg.state_file, quarantine=False)  # a damaged file is reported, never moved aside here
        return 200, {"ok": True, **self._view(st, user_id, self.clock())}, None

    def open(self, user_id: str, days: int, password: str = "") -> Reply:
        cfg = self.cfg
        if not 1 <= days <= cfg.self_max_days:
            return fail(422, f"days must be a whole number from 1 to {cfg.self_max_days}")
        users = Jellyfin(cfg.jellyfin_url, cfg.jellyfin_api_key).users()
        user = next((u for u in users if core.clean_id(u.get("Id")) == user_id), None)
        if user is None:
            return fail(422, "that account was not found")
        if (user.get("Policy") or {}).get("IsAdministrator"):
            return fail(422, "administrator accounts cannot do this; use your admin tools")
        name = str(user.get("Name", ""))
        if cfg.self_require_password:
            refused = self._password_refusal(user_id, name, password)
            if refused:
                return refused
        now = self.clock()
        with state.locked(cfg.state_file):  # the same lock `run`, `travel add` and the gateway's rounds use
            st = state.load(cfg.state_file, quarantine=False)
            view = self._view(st, user_id, now)
            if view["active"] is not None:
                return fail(422, "a travel window is already open or coming up for this account")
            if view["next_allowed"] is not None:
                when = time.strftime("%Y-%m-%d", time.gmtime(view["next_allowed"]))
                return fail(422, f"the next trip can be set up from {when} (UTC)")
            if cfg.dry_run:
                audit.record(cfg.audit_file, "api", "would-self-window", "", name, None, now)
                return 200, {"ok": True, "message": "dry run: nothing was changed", "active": None}, None
            try:
                w = core.new_window(state.windows(st), user_id, name, days, None, now, cfg.self_max_days)
            except ValueError:  # cannot happen after the checks above; never let it reach the caller as text
                return fail(422, "that request is not allowed")
            state.put_window(st, w, own=True)
            state.save(cfg.state_file, st)
        audit.record(cfg.audit_file, "api", "self-window", "", name, None, now)
        message = f"travel window opened for {days} day(s)"
        return 200, {"ok": True, "message": message, "active": {"id": w.id, "ends": int(w.ends_at)}}, None

    # -- the owner's own tools (a dashboard), behind ADMIN_TOKEN. Same effect as `lombo-visa travel`.
    def list_windows(self) -> Reply:
        st = state.load(self.cfg.state_file, quarantine=False)
        own, now = state.self_ids(st), self.clock()
        rows = [
            {"id": w.id, "user_id": w.user_id, "name": w.user_name, "country": w.country,
             "starts": int(w.starts_at), "ends": int(w.ends_at), "self": w.id in own}
            for w in sorted(state.windows(st), key=lambda w: w.ends_at)
            if w.revoked_at is None and w.ends_at > now
        ]  # fmt: skip
        return 200, {"ok": True, "windows": rows, "max_days": self.cfg.max_window_days}, None

    def admin_add(self, user_id: str, days: int, country: str | None) -> Reply:
        cfg = self.cfg
        users = Jellyfin(cfg.jellyfin_url, cfg.jellyfin_api_key).users()
        user = next((u for u in users if core.clean_id(u.get("Id")) == user_id), None)
        if user is None:
            return fail(422, "that account was not found")
        if (user.get("Policy") or {}).get("IsAdministrator"):
            return fail(422, "administrator accounts do not get travel windows")
        name, now = str(user.get("Name", "")), self.clock()
        with state.locked(cfg.state_file):
            st = state.load(cfg.state_file, quarantine=False)
            try:
                w = core.new_window(state.windows(st), user_id, name, days, country, now, cfg.max_window_days)
            except ValueError as e:  # our own plain messages about days and country
                return fail(422, str(e))
            state.put_window(st, w)
            state.save(cfg.state_file, st)
        audit.record(cfg.audit_file, "api", "admin-window", "", name, None, now)
        return 200, {"ok": True, "message": f"travel window opened for {days} day(s)", "id": w.id}, None

    def revoke(self, window_id: int) -> Reply:
        now = self.clock()
        with state.locked(self.cfg.state_file):
            st = state.load(self.cfg.state_file, quarantine=False)
            old = next((w for w in state.windows(st) if w.id == window_id and w.revoked_at is None), None)
            if old is None:
                return fail(404, "no such open window")
            state.put_window(
                st, core.Window(old.id, old.user_id, old.user_name, old.country, old.starts_at, old.ends_at, now)
            )
            state.save(self.cfg.state_file, st)
        audit.record(self.cfg.audit_file, "api", "admin-revoke", "", old.user_name, None, now)
        return 200, {"ok": True, "message": "window revoked"}, None


def make_server(
    cfg: Config,
    bind: tuple[str, int] | None = None,
    clock: Callable[[], float] = time.time,
    limiter_clock: Callable[[], float] = time.monotonic,
    log: Callable[[str], None] | None = None,
) -> ThreadingHTTPServer:
    """Build the server (not started). `bind` overrides API_BIND (tests use port 0)."""
    token, admin_token = cfg.api_token.encode(), cfg.admin_token.encode()
    limiter = FailureLimiter(limiter_clock)
    travel = Travel(cfg, clock)

    def say(text: str) -> None:
        (log or (lambda t: print(t, file=sys.stderr, flush=True)))(text)

    class Handler(BaseHTTPRequestHandler):
        server_version = "lombo-visa"
        sys_version = ""
        timeout = TIMEOUT

        def log_message(self, format: str, *args: Any) -> None:  # silence the default access log
            pass

        def _send(self, code: int, payload: dict[str, Any], extra: dict[str, str] | None = None) -> None:
            data = json.dumps(payload).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            for name, value in (extra or {}).items():
                self.send_header(name, value)
            self.end_headers()
            self.wfile.write(data)
            # Only the method and the route's shape: never the user id, the headers, the body or an address.
            shape = "/v1/travel" if urlsplit(self.path).path == "/v1/travel" else "other"
            if urlsplit(self.path).path.startswith("/v1/travel/"):
                shape = "/v1/travel/{id}"
            if urlsplit(self.path).path.startswith("/v1/admin/"):
                shape = "/v1/admin"
            say(f"[api] {core.clean_text(self.command, 8)} {shape} -> {code}")

        def _authorised(self, expected: bytes) -> bool:
            client = self.client_address[0]
            if limiter.blocked(client):
                self._send(*fail(429, "too many failed tries; wait a minute", {"Retry-After": str(int(WINDOW))}))
                return False
            scheme, _, given = (self.headers.get("Authorization") or "").partition(" ")
            # An unset token matches nothing: with only one of the two set, its routes alone answer.
            if expected and scheme.lower() == "bearer" and hmac.compare_digest(given.strip().encode(), expected):
                return True
            limiter.record(client)
            self._send(*fail(401, "unauthorized", {"WWW-Authenticate": "Bearer"}))
            return False

        def _dispatch(self) -> None:
            route = urlsplit(self.path).path
            admin = route.startswith("/v1/admin/")
            if self._authorised(admin_token if admin else token):
                self._send(*self._handle(route))

        def _handle(self, route: str) -> Reply:
            who = route.removeprefix("/v1/travel/") if route.startswith("/v1/travel/") else None
            if route.startswith("/v1/admin/"):
                return self._guarded(lambda: self._admin(route))
            if route != "/v1/travel" and who is None:
                return fail(404, "not found")
            allowed = "POST" if who is None else "GET"
            if self.command != allowed:
                return fail(405, f"use {allowed} here", {"Allow": allowed})

            def self_service() -> Reply:
                if who is None:
                    return self._open()
                user_id = parse_user_id(who)
                return fail(400, "that is not a Jellyfin user id") if user_id is None else travel.status(user_id)

            return self._guarded(self_service)

        def _admin(self, route: str) -> Reply:
            if route == "/v1/admin/windows":
                if self.command == "GET":
                    return travel.list_windows()
                if self.command != "POST":
                    return fail(405, "use GET or POST here", {"Allow": "GET, POST"})
                raw, problem = self._body()
                if problem:
                    return problem
                parsed, why = parse_admin_body(raw)
                return fail(400, why) if parsed is None else travel.admin_add(*parsed)
            number = route.removeprefix("/v1/admin/windows/")
            if number == route or not re.fullmatch(r"[0-9]{1,9}", number):
                return fail(404, "not found")
            if self.command != "DELETE":
                return fail(405, "use DELETE here", {"Allow": "DELETE"})
            return travel.revoke(int(number))

        def _guarded(self, work: Callable[[], Reply]) -> Reply:
            try:
                return work()
            except (JellyfinError, state.StateError) as e:  # nothing was written; the detail goes to the log only
                say(f"[FAIL] api: {core.clean_text(e, 200)}")
                return fail(502, "could not complete the request; try again later")
            except Exception as e:  # noqa: BLE001  # one bad request must never stop the server
                say(f"[FAIL] api: {type(e).__name__}")
                return fail(500, "error")

        def _body(self) -> tuple[bytes, Reply | None]:
            ctype = (self.headers.get("Content-Type") or "").split(";")[0].strip().lower()
            length_text = self.headers.get("Content-Length")
            if ctype != "application/json":
                return b"", fail(415, "send Content-Type: application/json")
            if length_text is None or not length_text.isdigit():
                return b"", fail(411, "send a Content-Length")
            if int(length_text) > MAX_BODY:
                return b"", fail(413, f"the body may be at most {MAX_BODY} bytes")
            return self.rfile.read(int(length_text)), None

        def _open(self) -> Reply:
            raw, problem = self._body()
            if problem:
                return problem
            parsed, why = parse_open_body(raw, cfg.self_require_password)
            if parsed is None:
                return fail(400, why)
            return travel.open(*parsed)

        do_GET = do_POST = do_PUT = do_DELETE = do_PATCH = do_HEAD = do_OPTIONS = _dispatch

    host, port = bind or cfg.api_bind

    class Server(ThreadingHTTPServer):
        daemon_threads = True
        address_family = socket.AF_INET6 if ":" in host else socket.AF_INET

    return Server((host, port), Handler)


def serve(cfg: Config) -> int:
    """Run until stopped (Ctrl-C or SIGTERM)."""
    server = make_server(cfg)
    print(
        f"api: listening on {cfg.api_bind[0]}:{server.server_port} "
        f"({'dry run: nothing is written' if cfg.dry_run else 'live'})",
        flush=True,
    )
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    print("api: stopped", file=sys.stderr, flush=True)
    return 0
