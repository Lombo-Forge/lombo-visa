"""The gateway: refuse a sign-in from abroad BEFORE Jellyfin sees it (optional second mode, `lombo-visa gateway`).

A reverse proxy sends only Jellyfin's sign-in paths here. For each one the gateway works out the country of the
client, looks for a travel window and decides with the same rules as the session mode (`core.decide`). Allowed: the
request goes to Jellyfin as it came and Jellyfin's answer comes back as it is. Refused: a plain 403.

It is never a general proxy: any other path, and any method but POST, gets a 404 and goes nowhere. The one
exception, off unless ASK_PAGE=true, is its own page at /visa/ask (ask.py), which is never forwarded.
Every doubt allows: no country database, an unknown address, unreadable travel windows, mode `off` or `log`, a
Jellyfin that cannot be asked who someone is. Nothing here reads or keeps a password, a token or the Authorization
header beyond passing them on to the configured upstream; request bodies are never logged or stored.
"""

import contextlib
import http.client
import ipaddress
import json
import os
import signal
import socket
import sys
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import unquote, urlparse

from . import ask, audit, core, geoip, state
from .config import Config
from .jellyfin import Jellyfin, JellyfinError

MAX_BODY = 64 * 1024
MAX_RESPONSE = 8 * 1024 * 1024
MAX_HEADERS = 64
MAX_HEADER_BYTES = 16 * 1024
MAX_CONNECTIONS = 64
READ_TIMEOUT = 5.0  # one read from the client may wait this long
UPSTREAM_TIMEOUT = 10.0  # connecting to, and each read from, Jellyfin
REQUEST_SECONDS = 30.0  # a whole request, however slowly the client or Jellyfin trickles
REFUSAL = "Sign-in from this country is not allowed. Ask the server owner to allow your trip."

PASSWORD, QUICKCONNECT, LEGACY = "password", "quickconnect", "password-legacy"
_HOP = {
    "connection",
    "keep-alive",
    "proxy-authenticate",
    "proxy-authorization",
    "te",
    "trailer",
    "trailers",
    "transfer-encoding",
    "upgrade",
    "content-length",
}


@dataclass(frozen=True)
class Reply:
    status: int
    reason: str
    headers: list[tuple[str, str]]
    body: bytes


def _json_reply(status: int, reason: str, message: str) -> Reply:
    body = json.dumps({"title": reason, "status": status, "detail": message}).encode()
    return Reply(status, reason, [("Content-Type", "application/json")], body)


# ---------------------------------------------------------------- which paths
def classify(target: str) -> tuple[str, str] | None:
    """(kind, user id from the path or "") for a sign-in request target, or None for anything else.

    The path is decoded exactly once and then compared in lower case, segment by segment, so
    `/users/AuthenticateByName`, `/Users/Authenticate%42yName` and a trailing slash are the same path.
    Anything that could be read two ways is refused: a second layer of encoding (`%25`), an encoded slash or
    backslash, control characters, empty or dot segments.
    """
    path = target.partition("?")[0]
    if not path.startswith("/") or any(not 0x20 < ord(c) < 0x7F for c in target):
        return None
    try:
        decoded = unquote(path, errors="strict")
    except UnicodeDecodeError:
        return None
    if "%" in decoded or "\\" in decoded or decoded.count("/") != path.count("/"):
        return None
    if any(ord(c) < 0x20 or ord(c) == 0x7F for c in decoded):
        return None
    parts = decoded.lower().split("/")[1:]
    if parts and parts[-1] == "":
        parts.pop()
    if not parts or any(p in ("", ".", "..") for p in parts):
        return None
    if parts[0] in ("emby", "mediabrowser"):
        parts = parts[1:]
    if parts[:1] != ["users"]:
        return None
    if parts == ["users", "authenticatebyname"]:
        return PASSWORD, ""
    if parts == ["users", "authenticatewithquickconnect"]:
        return QUICKCONNECT, ""
    if len(parts) == 3 and parts[2] == "authenticate":
        return LEGACY, parts[1]
    return None


# ---------------------------------------------------------------- who is calling
def _in(ip: str, nets: tuple[core.Network, ...]) -> bool:
    a = ipaddress.ip_address(ip)
    return any(a in n for n in nets if n.version == a.version)


def client_address(peer: str, forwarded: list[str], trusted: tuple[core.Network, ...]) -> tuple[str | None, bool]:
    """(client address or None, whether a forwarded header was ignored).

    `X-Forwarded-For` is believed only when the connecting peer is in `trusted`; then the rightmost hop that is not
    itself trusted is the client. Otherwise the header is ignored and the peer is the client. None = cannot tell.
    """
    peer_ip = core.normalise_ip(peer)
    if peer_ip is None:
        return None, False
    if not _in(peer_ip, trusted):
        return peer_ip, bool(forwarded)
    hops = [h.strip() for value in forwarded for h in value.split(",") if h.strip()]
    first: str | None = None
    for hop in reversed(hops):
        ip = core.normalise_ip(hop)
        if ip is None:
            return None, False
        if not _in(ip, trusted):
            return ip, False
        first = ip
    return (first or peer_ip), False


def username(body: bytes) -> tuple[str, bool]:
    """(the typed user name, ambiguous) from a password sign-in body. Jellyfin matches the key without regard to
    case, so two different values under keys that differ only in case cannot be told apart: ambiguous."""
    try:
        pairs = json.loads(body, object_pairs_hook=lambda p: p)
    except (ValueError, RecursionError):
        return "", False
    if not isinstance(pairs, list):
        return "", False
    values = [v for k, v in pairs if str(k).lower() == "username"]
    if any(not isinstance(v, str) for v in values) or len(set(values)) > 1:
        return "", True
    return (values[0][:80] if values else ""), False


# ---------------------------------------------------------------- the gateway
class _Countries:
    """The country lookup, reopened when the database file changes (a monthly update needs no restart)."""

    def __init__(self, path: str) -> None:
        self._path = path
        self._key: tuple[int, int] | None = None
        self._lookup: geoip.Lookup | None = None
        self._lock = threading.Lock()

    def __call__(self, ip: str) -> str | None:
        with self._lock:
            try:
                st = os.stat(self._path)
                key = (st.st_mtime_ns, st.st_size)
            except OSError:
                self._key, self._lookup = None, None
                return None
            if key != self._key:
                self._key, self._lookup = key, geoip.open_lookup(self._path)
            lookup = self._lookup
        return lookup.country(ip) if lookup else None


class Gateway:
    def __init__(
        self, cfg: Config, clock: Callable[[], float] = time.time, log: Callable[[str], None] | None = None
    ) -> None:
        self.cfg = cfg
        self.clock = clock
        self._log = log or (lambda m: print(m, flush=True))
        self._countries = _Countries(cfg.geoip_db)
        self._warned_header = False
        up = urlparse(cfg.gateway_upstream)
        self._tls = up.scheme == "https"
        self._host = up.hostname or ""
        self._port = up.port or (443 if self._tls else 80)
        self._netloc = up.netloc
        self._exempt_ids = {core.clean_id(x) for x in cfg.exempt_users}
        self.asks = ask.Asks(cfg, self.log)

    # -- helpers
    def log(self, text: str) -> None:
        self._log(f"gateway: {text}")

    def note_ignored_header(self) -> None:
        if not self._warned_header:
            self._warned_header = True
            self.log(
                "a request carried X-Forwarded-For from an address that is not in LOMBO_VISA_TRUSTED_PROXIES: "
                "the header is ignored and the connecting address is used"
            )

    def _windows(self) -> list[core.Window] | None:
        try:
            return state.windows(state.load(self.cfg.state_file, quarantine=False))
        except Exception:  # noqa: BLE001 (fail open: unreadable windows mean "unknown", never "refuse")
            return None

    def _resolve_id(self, name: str) -> str | None:
        """The account id for a typed name; "" = Jellyfin answered and has no such account; None = could not ask."""
        try:
            users = Jellyfin(self.cfg.jellyfin_url, self.cfg.jellyfin_api_key, timeout=5).users()
        except (JellyfinError, ValueError, OSError):
            return None
        low = name.lower()
        return next((core.clean_id(u.get("Id")) for u in users if str(u.get("Name", "")).lower() == low), "")

    def judge(self, country: str, name: str, uid: str | None, ambiguous: bool) -> str:
        """The verdict for one sign-in (core's names). `uid` None means "look it up from the name when needed"."""
        if (name and name.lower() in self.cfg.exempt_users) or (uid and core.clean_id(uid) in self._exempt_ids):
            return core.EXEMPT
        home = self.cfg.home_countries
        if country == core.LAN or country in home:
            return core.ALLOW_HOME
        if country == core.UNKNOWN:
            return core.UNKNOWN
        windows = self._windows()
        if windows is None:
            return core.UNKNOWN
        if ambiguous or (not name and not uid):
            return core.DENY  # a foreign address and no way to say who: nothing can match a window
        if uid is None:
            uid = self._resolve_id(name)
            if uid is None:  # Jellyfin could not be asked: match the windows by the typed name instead
                uid = next((w.user_id for w in windows if w.user_name.lower() == name.lower()), "")
        return core.decide(country, home, windows, uid, self.clock())[0]

    def _record(self, kind: str, decision: str, country: str, user: str, ip: str | None, shown: str) -> None:
        audit.record(self.cfg.audit_file, "gateway", decision, country, user, ip, self.clock())
        self.log(f"{shown} ({kind}) country={country} user={core.clean_text(user)}")

    def refusal(self) -> Reply:
        reply = _json_reply(403, "Forbidden", self.cfg.gateway_message or REFUSAL)
        if not self.cfg.ask_page:
            return reply
        body = json.dumps({**json.loads(reply.body), "away": ask.PATH}).encode()
        return Reply(reply.status, reply.reason, reply.headers, body)

    def asked(self, content_type: str, body: bytes, ip: str | None) -> Reply:
        country = core.country_of(ip or "", self._countries, self.cfg.lan_subnets)
        status, message = self.asks.ask(ask.name_from(content_type, body), ip or "?", country)
        reason = {200: "OK", 422: "Unprocessable Content", 429: "Too Many Requests"}[status]
        return Reply(status, reason, [("Content-Type", "text/plain; charset=utf-8")], message.encode())

    # -- forwarding
    def forward(self, method: str, target: str, headers: list[tuple[str, str]], body: bytes) -> Reply:
        """Send the request to the configured upstream and return its answer untouched. Never follows a redirect."""
        listed = {t.strip().lower() for k, v in headers if k.lower() == "connection" for t in v.split(",") if t.strip()}
        # Not Accept-Encoding: the answer is read to see who signed in (Quick Connect), and a compressed one
        # cannot be read, which counted as a doubt and let a foreign sign-in through.
        drop = _HOP | listed | {"accept-encoding"}
        out = [(k, v) for k, v in headers if k.lower() not in drop]
        cls = http.client.HTTPSConnection if self._tls else http.client.HTTPConnection
        conn = cls(self._host, self._port, timeout=UPSTREAM_TIMEOUT)
        try:
            conn.putrequest(method, target, skip_host=True, skip_accept_encoding=True)
            if not any(k.lower() == "host" for k, _ in out):
                conn.putheader("Host", self._netloc)
            for k, v in out:
                conn.putheader(k, v)
            conn.putheader("Content-Length", str(len(body)))
            conn.endheaders(body)
            resp = conn.getresponse()
            data = resp.read(MAX_RESPONSE + 1)
            if len(data) > MAX_RESPONSE:
                raise http.client.HTTPException("answer too large")
            back = [
                (k, v)
                for k, v in resp.getheaders()
                if k.lower() not in _HOP and k.lower() not in _tokens(resp.getheader("Connection"))
            ]
            return Reply(resp.status, resp.reason, back, data)
        finally:
            conn.close()

    def _safe_forward(self, method: str, target: str, headers: list[tuple[str, str]], body: bytes) -> Reply:
        try:
            return self.forward(method, target, headers, body)
        except ValueError:
            return _json_reply(400, "Bad Request", "The request headers are not valid.")
        except (OSError, http.client.HTTPException) as e:
            self.log(f"could not reach the upstream ({type(e).__name__})")
            return _json_reply(502, "Bad Gateway", "Could not reach Jellyfin.")

    def _logout(self, token: str) -> None:
        """End a session whose sign-in turned out to be refusable (Quick Connect). Best effort; nothing is logged."""
        with contextlib.suppress(OSError, http.client.HTTPException, ValueError):
            self.forward("POST", "/Sessions/Logout", [("Authorization", f'MediaBrowser Token="{token}"')], b"")

    # -- one request
    def process(
        self,
        kind: str,
        path_id: str,
        method: str,
        target: str,
        headers: list[tuple[str, str]],
        body: bytes,
        ip: str | None,
    ) -> Reply:
        mode = self.cfg.gateway_effective_mode

        def send() -> Reply:
            return self._safe_forward(method, target, headers, body)

        if mode == "off":
            return send()
        country = core.country_of(ip or "", self._countries, self.cfg.lan_subnets)
        enforce = mode == "enforce"
        if kind == QUICKCONNECT:
            return self._quickconnect(country, ip, enforce, send)
        name, ambiguous = username(body) if kind == PASSWORD else ("", False)
        verdict = self.judge(country, name, (path_id or None) if kind == LEGACY else None, ambiguous)
        user = name or path_id
        if verdict == core.DENY and enforce:
            self._record(kind, core.DENY, country, user, ip, "refused")
            return self.refusal()
        shown = "would-deny" if verdict == core.DENY else verdict
        self._record(kind, shown, country, user, ip, shown)
        reply = send()
        if verdict == core.ALLOW_WINDOW:
            self._remember_device(reply, country)
        return reply

    def _remember_device(self, reply: Reply, country: str) -> None:
        """Note the device a travel window just let in, so `run` signs it out when the window ends. `run` alone
        only sees the address Jellyfin recorded, which behind another proxy looks local. Never fails a sign-in."""
        if self.cfg.dry_run or reply.status != 200:
            return
        try:
            data = json.loads(reply.body)
            who, device = data["User"], str(data["SessionInfo"]["DeviceId"])
            uid, now = str(who["Id"]), self.clock()
            window = core.decide(country, self.cfg.home_countries, self._windows() or [], uid, now)[1]
            if window is None or not device:
                return
            with state.locked(self.cfg.state_file):
                st = state.load(self.cfg.state_file, quarantine=False)
                key = core.device_key(uid, device)
                if key in st["devices"] or len(st["devices"]) < state.MAX_DEVICES:
                    st["devices"][key] = {"user": core.clean_text(who.get("Name")), "first": now, "window": window}
                    state.save(self.cfg.state_file, st)
        except Exception as e:  # noqa: BLE001 (bookkeeping: the person is already signed in)
            self.log(f"could not note the device for its travel window ({type(e).__name__})")

    def _quickconnect(self, country: str, ip: str | None, enforce: bool, send: Callable[[], Reply]) -> Reply:
        """The request names nobody: the person is known only from Jellyfin's answer. So a sign-in from abroad is
        forwarded first, and if it has to be refused the new session is ended again before the answer is returned."""
        if country == core.LAN or country in self.cfg.home_countries or country == core.UNKNOWN:
            verdict = core.UNKNOWN if country == core.UNKNOWN else core.ALLOW_HOME
            self._record(QUICKCONNECT, verdict, country, "", ip, verdict)
            return send()
        reply = send()
        if reply.status != 200:
            return reply
        try:
            data: Any = json.loads(reply.body)
            who = data.get("User") or {}
            name, uid, token = str(who.get("Name") or ""), str(who.get("Id") or ""), data.get("AccessToken")
        except (ValueError, RecursionError, AttributeError):
            self._record(QUICKCONNECT, core.UNKNOWN, country, "", ip, core.UNKNOWN)
            return reply
        # Jellyfin's own answer did not say who it is: that is a doubt, not a reason to end a session.
        verdict = self.judge(country, name, uid or None, False) if (name or uid) else core.UNKNOWN
        if verdict == core.DENY and enforce:
            if isinstance(token, str) and token:
                self._logout(token)
            self._record(QUICKCONNECT, core.DENY, country, name, ip, "refused")
            return self.refusal()
        shown = "would-deny" if verdict == core.DENY else verdict
        self._record(QUICKCONNECT, shown, country, name, ip, shown)
        if verdict == core.ALLOW_WINDOW:
            self._remember_device(reply, country)
        return reply


def _tokens(value: str | None) -> set[str]:
    return {t.strip().lower() for t in (value or "").split(",") if t.strip()}


# ---------------------------------------------------------------- the server
class _Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.0"  # one request per connection: no keep-alive to hold slots open
    timeout = READ_TIMEOUT
    server: "GatewayServer"

    def setup(self) -> None:
        super().setup()
        self._timer = threading.Timer(REQUEST_SECONDS, self._expire)
        self._timer.daemon = True
        self._timer.start()

    def finish(self) -> None:
        self._timer.cancel()
        super().finish()

    def _expire(self) -> None:
        with contextlib.suppress(OSError):
            self.request.shutdown(socket.SHUT_RDWR)

    def log_message(self, format: str, *args: Any) -> None:
        """The standard library would log every request line, which carries the path and query: say nothing."""

    def _send(self, reply: Reply) -> None:
        self.send_response_only(reply.status, reply.reason)
        for k, v in reply.headers:
            self.send_header(k, v)
        self.send_header("Content-Length", str(len(reply.body)))
        self.send_header("Connection", "close")
        self.end_headers()
        with contextlib.suppress(OSError):  # the client hung up: nothing to report
            self.wfile.write(reply.body)
        self._drain()

    def _drain(self) -> None:
        """Say we are done writing, then read away what the client still sends (a bounded amount): closing a socket that
        has unread data makes the system reset the connection, and the client would lose the answer."""
        with contextlib.suppress(OSError):
            self.wfile.flush()
            self.request.shutdown(socket.SHUT_WR)
            left = 2 * MAX_BODY
            while left > 0:
                chunk = self.request.recv(min(left, 65536))
                if not chunk:
                    break
                left -= len(chunk)

    def _not_found(self) -> None:
        self._send(_json_reply(404, "Not Found", "Not found."))

    def __getattr__(self, name: str) -> Any:
        if name.startswith("do_"):  # every other method: not ours
            return self._not_found
        raise AttributeError(name)

    def _asking(self) -> bool:
        return self.server.gateway.cfg.ask_page and self.path.partition("?")[0] == ask.PATH

    def do_GET(self) -> None:
        if not self._asking():
            self._not_found()
            return
        headers = [("Content-Type", "text/html; charset=utf-8"), ("Cache-Control", "no-store")]
        self._send(Reply(200, "OK", headers, ask.page(self.server.gateway.cfg)))

    def do_POST(self) -> None:
        self._send(self._answer())

    def _answer(self) -> Reply:
        found = classify(self.path)
        if found is None and not self._asking():
            return _json_reply(404, "Not Found", "Not found.")
        pairs = list(self.headers.items())
        if len(pairs) > MAX_HEADERS or sum(len(k) + len(v) for k, v in pairs) > MAX_HEADER_BYTES:
            return _json_reply(431, "Request Header Fields Too Large", "Too many or too large headers.")
        if self.headers.get("Transfer-Encoding") is not None:
            return _json_reply(400, "Bad Request", "Send a Content-Length instead of a chunked body.")
        raw = self.headers.get("Content-Length", "0").strip()
        if not raw.isascii() or not raw.isdigit():
            return _json_reply(400, "Bad Request", "Content-Length is not a number.")
        length = int(raw)
        if length > MAX_BODY:
            return _json_reply(413, "Payload Too Large", "The request is too large.")
        try:
            body = self.rfile.read(length)
        except OSError:  # the client was too slow (timeout) or went away
            body = b""
        if len(body) != length:
            return _json_reply(400, "Bad Request", "The body is shorter than Content-Length.")
        gw = self.server.gateway
        forwarded = self.headers.get_all("X-Forwarded-For") or []
        ip, ignored = client_address(str(self.client_address[0]), forwarded, gw.cfg.proxy_networks)
        if ignored:
            gw.note_ignored_header()
        if found is None:
            return gw.asked(self.headers.get("Content-Type", ""), body, ip)
        kind, path_id = found
        return gw.process(kind, path_id, self.command, self.path, pairs, body, ip)


class GatewayServer(ThreadingHTTPServer):
    daemon_threads = True
    request_queue_size = 32

    def __init__(self, address: tuple[str, int], gateway: Gateway) -> None:
        self.gateway = gateway
        self.address_family = socket.AF_INET6 if ":" in address[0] else socket.AF_INET
        self._slots = threading.BoundedSemaphore(MAX_CONNECTIONS)
        super().__init__(address, _Handler)

    def process_request(self, request: Any, client_address: Any) -> None:
        if not self._slots.acquire(blocking=False):  # busy: say so and hang up, rather than queue without limit
            with contextlib.suppress(OSError):
                request.sendall(b"HTTP/1.0 503 Service Unavailable\r\nContent-Length: 0\r\nConnection: close\r\n\r\n")
            self.shutdown_request(request)
            return
        try:
            super().process_request(request, client_address)
        except BaseException:
            self._slots.release()
            raise

    def process_request_thread(self, request: Any, client_address: Any) -> None:
        try:
            super().process_request_thread(request, client_address)
        finally:
            self._slots.release()


def serve(cfg: Config) -> int:
    """Run until stopped (Ctrl-C or SIGTERM)."""
    mode = cfg.gateway_effective_mode
    server = GatewayServer(cfg.gateway_bind, Gateway(cfg))
    print(
        f"gateway: listening on {cfg.gateway_bind[0]}:{server.server_port}, "
        f"upstream {cfg.gateway_upstream}, mode {mode}",
        flush=True,
    )
    if cfg.dry_run and cfg.gateway_mode == "enforce":
        print("gateway: LOMBO_VISA_DRY_RUN=true, so mode enforce acts as log: nothing is refused.", flush=True)
    if mode == "off":
        print("gateway: mode off: sign-ins are passed through without a decision.", flush=True)
    signal.signal(signal.SIGTERM, lambda *_: threading.Thread(target=server.shutdown, daemon=True).start())
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    print("gateway: stopped", file=sys.stderr, flush=True)
    return 0
