"""The self-service travel API against a fake Jellyfin, on a real socket (127.0.0.1, port 0).

No real Jellyfin and no real page service have been involved.
"""

import http.client
import json
import socket
import threading
import time

import pytest

from lombo_visa import audit, api, core, state
from lombo_visa.cli import main
from lombo_visa.config import PREFIX, ConfigError, from_env

TOKEN = "tok-" + "AbCdEfGhIj0123456789" * 2
UID = "11111111222233334444555566667777"
ADMIN = "aaaaaaaabbbbccccddddeeeeffff0000"
DAY = 86400
T0 = 1_800_000_000.0
LIVE = {"LOMBO_VISA_DRY_RUN": "false"}


def users():
    return [
        {"Id": UID, "Name": "Pat", "Policy": {"IsAdministrator": False}},
        {"Id": ADMIN, "Name": "Boss", "Policy": {"IsAdministrator": True}},
    ]


class Running:
    def __init__(self, server, logs):
        self.server, self.logs = server, logs
        self.port = server.server_address[1]

    def call(self, method, path, body=None, token=TOKEN, headers=None, ctype="application/json", raw=None):
        h = dict(headers or {})
        if token is not None:
            h["Authorization"] = f"Bearer {token}"
        data = raw if raw is not None else (json.dumps(body).encode() if body is not None else None)
        if data is not None and ctype:
            h["Content-Type"] = ctype
        c = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        try:
            c.request(method, path, body=data, headers=h)
            r = c.getresponse()
            payload = r.read()
            return r.status, (json.loads(payload) if payload else None), r
        finally:
            c.close()

    def post(self, days=3, user=UID, **kw):
        return self.call("POST", "/v1/travel", {"user_id": user, "days": days}, **kw)


class Clock:
    def __init__(self, now=T0):
        self.now = now

    def __call__(self):
        return self.now


@pytest.fixture
def start(fake, env):
    fake.users = users()
    running = []

    def make(extra=None, clock=None, **kw):
        cfg = from_env({**env, "LOMBO_VISA_API_TOKEN": TOKEN, **(extra or {})})
        logs: list[str] = []
        server = api.make_server(cfg, bind=("127.0.0.1", 0), clock=clock or time.time, log=logs.append, **kw)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        r = Running(server, logs)
        running.append(r)
        return r

    yield make
    for r in running:
        r.server.shutdown()
        r.server.server_close()


def windows(env):
    return state.load(env["LOMBO_VISA_STATE_FILE"], quarantine=False)["windows"]


# ---------------------------------------------------------------- auth, limiter, routes
def test_missing_or_wrong_token_is_401_and_says_nothing_else(start):
    a = start(LIVE)
    for token in (None, "wrong", TOKEN[:-1], TOKEN + "x"):
        code, body, r = a.call("GET", f"/v1/travel/{UID}", token=token)
        assert (code, body) == (401, {"ok": False, "message": "unauthorized"})
        assert r.getheader("WWW-Authenticate") == "Bearer"
    code, _, r = a.call("GET", f"/v1/travel/{UID}", headers={"Authorization": f"Basic {TOKEN}"}, token=None)
    assert code == 401


def test_every_answer_is_json_no_store_and_has_no_cors_headers(start):
    a = start(LIVE)
    for _code, _, r in (a.call("GET", f"/v1/travel/{UID}"), a.call("GET", "/nope"), a.call("GET", "/v1/travel/x")):
        assert r.getheader("Content-Type") == "application/json"
        assert r.getheader("Cache-Control") == "no-store"
        assert not [k for k, _ in r.getheaders() if k.lower().startswith("access-control")]


def test_ten_failures_then_429_even_for_the_right_token_until_the_minute_is_up(start):
    now = [1000.0]
    a = start(LIVE, limiter_clock=lambda: now[0])
    for _ in range(10):
        assert a.call("GET", f"/v1/travel/{UID}", token="bad")[0] == 401
    code, body, r = a.call("GET", f"/v1/travel/{UID}")
    assert code == 429
    assert r.getheader("Retry-After") == "60"
    assert body["ok"] is False
    now[0] += 61
    assert a.call("GET", f"/v1/travel/{UID}")[0] == 200


def test_unknown_path_404_and_wrong_method_405_with_allow(start):
    a = start(LIVE)
    assert a.call("GET", "/")[0] == 404
    assert a.call("GET", "/v1/health")[0] == 404
    assert a.call("GET", "/v1/travel/")[1]["message"] == "that is not a Jellyfin user id"
    assert a.call("GET", f"/v1/travel/{UID}/x")[0] == 400
    code, _, r = a.call("GET", "/v1/travel")
    assert (code, r.getheader("Allow")) == (405, "POST")
    for method in ("POST", "PUT", "DELETE", "PATCH"):
        code, _, r = a.call(method, f"/v1/travel/{UID}", {"user_id": UID, "days": 1})
        assert (code, r.getheader("Allow")) == (405, "GET")
    assert a.call("PUT", "/v1/travel", {"user_id": UID, "days": 1})[0] == 405
    assert a.call("OPTIONS", "/v1/travel")[0] == 405


# ---------------------------------------------------------------- request validation
@pytest.mark.parametrize(
    "raw",
    [
        b"not json",
        b"",
        b"\xff\xfe",
        b"[]",
        b'"x"',
        b"{}",
        b'{"user_id": "' + UID.encode() + b'"}',
        b'{"days": 3}',
        b'{"user_id": "' + UID.encode() + b'", "days": 3, "country": "DE"}',
        b'{"user_id": "' + UID.encode() + b'", "user_id": "' + UID.encode() + b'", "days": 3}',
        b'{"user_id": "' + UID.encode() + b'", "days": NaN}',
        b'{"user_id": "' + UID.encode() + b'", "days": Infinity}',
        b'{"user_id": "' + UID.encode() + b'", "days": 2.5}',
        b'{"user_id": "' + UID.encode() + b'", "days": "3"}',
        b'{"user_id": "' + UID.encode() + b'", "days": true}',
        b'{"user_id": 5, "days": 3}',
        b'{"user_id": "short", "days": 3}',
        b'{"user_id": "' + b"g" * 32 + b'", "days": 3}',
        b'{"user_id": "' + UID.encode() + b'00", "days": 3}',
        b"[" * 4000,
    ],
)
def test_bad_bodies_are_400_and_write_nothing(start, env, raw):
    a = start(LIVE)
    code, body, _ = a.call("POST", "/v1/travel", raw=raw)
    assert code == 400
    assert body["ok"] is False
    assert not windows(env)


def test_wrong_content_type_is_415_and_a_big_body_413(start):
    a = start(LIVE)
    assert a.post(ctype="text/plain")[0] == 415
    assert a.post(ctype="application/json; charset=utf-8")[0] == 200
    code, _, _ = a.call("POST", "/v1/travel", raw=b" " * (api.MAX_BODY + 1))
    assert code == 413


def test_content_length_is_required(start):
    a = start(LIVE)
    with socket.create_connection(("127.0.0.1", a.port), timeout=10) as s:
        s.sendall(
            f"POST /v1/travel HTTP/1.1\r\nHost: x\r\nAuthorization: Bearer {TOKEN}\r\n"
            "Content-Type: application/json\r\n\r\n".encode()
        )
        reply = s.recv(4096).decode()
    assert reply.startswith("HTTP/1.0 411") or " 411 " in reply.splitlines()[0]


def test_dashed_and_upper_case_ids_are_normalised_and_junk_is_400(start):
    a = start(LIVE)
    dashed = "11111111-2222-3333-4444-555566667777"
    assert a.call("GET", f"/v1/travel/{dashed}")[0] == 200
    assert a.call("GET", f"/v1/travel/{UID.upper()}")[0] == 200
    for bad in ("abc", "g" * 32, UID + "0", "-" * 32, "1111%2d1111"):
        assert a.call("GET", f"/v1/travel/{bad}")[0] == 400
    code, body, _ = a.post(user=dashed)
    assert code == 200
    assert body["active"]["id"] == 1


# ---------------------------------------------------------------- the rules
def test_days_out_of_range_are_422(start, env):
    a = start({**LIVE, "LOMBO_VISA_SELF_MAX_DAYS": "10"})
    for days in (0, -1, 11, 10**9):
        code, body, _ = a.post(days=days)
        assert code == 422
        assert "1 to 10" in body["message"]
    assert not windows(env)
    assert a.post(days=10)[0] == 200


def test_unknown_user_and_administrator_are_422(start, env):
    a = start(LIVE)
    code, body, _ = a.post(user="0" * 32)
    assert (code, body["ok"]) == (422, False)
    code, body, _ = a.post(user=ADMIN)
    assert code == 422
    assert "admin" in body["message"]
    assert not windows(env)


def test_jellyfin_down_is_a_generic_502_and_writes_nothing(start, env):
    dead = socket.socket()
    dead.bind(("127.0.0.1", 0))
    url = f"http://127.0.0.1:{dead.getsockname()[1]}"
    dead.close()
    a = start({**LIVE, "LOMBO_VISA_JELLYFIN_URL": url})
    code, body, _ = a.post()
    assert code == 502
    assert "127.0.0.1" not in body["message"]
    assert url not in body["message"]
    assert not windows(env)


def test_jellyfin_answering_nonsense_is_502(start, fake, env):
    fake.users = "not a list"  # type: ignore[assignment]
    a = start(LIVE)
    assert a.post()[0] == 502
    assert not windows(env)


def test_happy_path_writes_a_self_window_for_any_country(start, env):
    clock = Clock()
    a = start(LIVE, clock=clock)
    code, body, _ = a.post(days=5)
    assert code == 200
    assert body["ok"] is True
    assert body["message"]
    assert body["active"] == {"id": 1, "ends": int(T0 + 5 * DAY)}
    (entry,) = windows(env).values()
    assert entry == {
        "user_id": UID,
        "user_name": "Pat",
        "country": None,
        "starts_at": T0,
        "ends_at": T0 + 5 * DAY,
        "revoked_at": None,
        "self": True,
    }
    (w,) = state.windows(state.load(env["LOMBO_VISA_STATE_FILE"]))
    assert w.active(T0 + 1)  # the session mode would honour it


def test_status_route(start, env):
    clock = Clock()
    a = start(LIVE, clock=clock)
    code, body, _ = a.call("GET", f"/v1/travel/{UID}")
    assert (code, body) == (200, {"ok": True, "active": None, "next_allowed": None, "max_days": 14})
    a.post(days=2)
    code, body, _ = a.call("GET", f"/v1/travel/{UID}")
    assert body["active"] == {"id": 1, "ends": int(T0 + 2 * DAY)}
    assert body["next_allowed"] == int(T0 + 2 * DAY + 30 * DAY)
    assert a.call("GET", "/v1/travel/" + "0" * 32)[1]["active"] is None  # someone else sees nothing


def test_an_admin_made_or_upcoming_window_counts_as_active_and_a_revoked_one_does_not(start, env):
    clock = Clock()
    a = start(LIVE, clock=clock)
    path = env["LOMBO_VISA_STATE_FILE"]
    with state.locked(path):
        st = state.load(path)
        state.put_window(st, core.Window(4, UID, "Pat", "DE", T0 + DAY, T0 + 9 * DAY))  # upcoming, by an admin
        state.save(path, st)
    assert a.call("GET", f"/v1/travel/{UID}")[1]["active"] == {"id": 4, "ends": int(T0 + 9 * DAY)}
    assert a.call("GET", f"/v1/travel/{UID}")[1]["next_allowed"] is None  # not self-opened: no cool-down
    code, body, _ = a.post()
    assert code == 422
    assert "already" in body["message"]
    with state.locked(path):
        st = state.load(path)
        state.put_window(st, core.Window(4, UID, "Pat", "DE", T0 + DAY, T0 + 9 * DAY, T0))
        state.save(path, st)
    assert a.call("GET", f"/v1/travel/{UID}")[1]["active"] is None
    assert a.post()[0] == 200


def test_a_second_window_while_one_is_open_is_422(start, env):
    a = start(LIVE, clock=Clock())
    assert a.post()[0] == 200
    code, body, _ = a.post()
    assert code == 422
    assert "already" in body["message"]
    assert len(windows(env)) == 1


def test_cooldown_counts_from_the_end_of_the_window(start, env):
    clock = Clock()
    a = start({**LIVE, "LOMBO_VISA_SELF_COOLDOWN_DAYS": "10"}, clock=clock)
    assert a.post(days=3)[0] == 200
    end = T0 + 3 * DAY
    clock.now = end + 5 * DAY  # the window is over, the cool-down is not
    code, body, _ = a.post()
    assert code == 422
    assert time.strftime("%Y-%m-%d", time.gmtime(end + 10 * DAY)) in body["message"]
    status = a.call("GET", f"/v1/travel/{UID}")[1]
    assert status["active"] is None
    assert status["next_allowed"] == int(end + 10 * DAY)
    clock.now = end + 10 * DAY + 1
    assert a.call("GET", f"/v1/travel/{UID}")[1]["next_allowed"] is None
    assert a.post()[0] == 200
    assert len(windows(env)) == 2


def test_cooldown_can_count_from_when_the_window_was_made(start):
    clock = Clock()
    a = start({**LIVE, "LOMBO_VISA_SELF_COOLDOWN_DAYS": "10", "LOMBO_VISA_SELF_COOLDOWN_FROM": "start"}, clock=clock)
    assert a.post(days=3)[0] == 200
    clock.now = T0 + 5 * DAY
    assert a.call("GET", f"/v1/travel/{UID}")[1]["next_allowed"] == int(T0 + 10 * DAY)
    assert a.post()[0] == 422
    clock.now = T0 + 10 * DAY + 1
    assert a.post()[0] == 200


def test_the_admin_list_counts_the_last_seven_days_by_mode(start, tmp_path):
    clock = Clock()
    admin = "d" * 32
    log = str(tmp_path / "audit.jsonl")
    for mode, decision, age in [
        ("gateway", "deny", 1),
        ("gateway", "deny", 2),
        ("session", "deny", 1),
        ("gateway", "deny", 8),
    ]:
        audit.record(log, mode, decision, "JP", "alex", None, T0 - age * DAY)
    a = start({"LOMBO_VISA_ADMIN_TOKEN": admin, "LOMBO_VISA_AUDIT_FILE": log}, clock=clock)
    body = a.call("GET", "/v1/admin/windows", token=admin)[1]
    assert body["last7"] == {"gateway": {"deny": 2}, "session": {"deny": 1}}
    bare = start({"LOMBO_VISA_ADMIN_TOKEN": admin}, clock=clock)
    assert bare.call("GET", "/v1/admin/windows", token=admin)[1]["last7"] is None


def test_cooldown_zero_allows_a_new_window_right_after_the_last_one(start):
    clock = Clock()
    a = start({**LIVE, "LOMBO_VISA_SELF_COOLDOWN_DAYS": "0"}, clock=clock)
    assert a.post(days=1)[0] == 200
    clock.now = T0 + DAY + 1
    assert a.call("GET", f"/v1/travel/{UID}")[1]["next_allowed"] is None
    assert a.post()[0] == 200


def test_a_revoked_self_window_still_starts_the_cooldown_from_the_revoke(start, env):
    clock = Clock()
    a = start(LIVE, clock=clock)
    a.post(days=10)
    path = env["LOMBO_VISA_STATE_FILE"]
    clock.now = T0 + 2 * DAY
    with state.locked(path):  # what `travel revoke` does: it rewrites the window
        st = state.load(path)
        (w,) = state.windows(st)
        state.put_window(st, core.Window(w.id, w.user_id, w.user_name, w.country, w.starts_at, w.ends_at, clock.now))
        state.save(path, st)
    assert windows(env)["1"]["self"] is True  # the mark survives the rewrite
    status = a.call("GET", f"/v1/travel/{UID}")[1]
    assert status["active"] is None
    assert status["next_allowed"] == int(T0 + 2 * DAY + 30 * DAY)
    assert a.post()[0] == 422


def test_an_admin_window_of_someone_else_does_not_block_this_person(start, env):
    a = start(LIVE, clock=Clock())
    path = env["LOMBO_VISA_STATE_FILE"]
    with state.locked(path):
        st = state.load(path)
        state.put_window(st, core.Window(1, "f" * 32, "Other", None, T0, T0 + DAY, None), own=True)
        state.save(path, st)
    assert a.post()[0] == 200


# ---------------------------------------------------------------- dry run, lock, audit, logs
def test_dry_run_answers_but_writes_nothing(start, env, tmp_path):
    audit_file = tmp_path / "audit.jsonl"
    a = start({"LOMBO_VISA_AUDIT_FILE": str(audit_file)})  # DRY_RUN defaults to true
    code, body, _ = a.post()
    assert code == 200
    assert body["ok"] is True
    assert body["message"].startswith("dry run: nothing was changed")
    assert not (tmp_path / "state.json").exists()
    assert a.call("GET", f"/v1/travel/{UID}")[1]["active"] is None
    assert a.post(days=99)[0] == 422  # the rules still apply, so a dry run shows what would be refused
    assert [json.loads(x)["decision"] for x in audit_file.read_text().splitlines()] == ["would-self-window"]


def test_it_waits_for_the_state_file_lock_the_other_modes_hold(start, env):
    a = start(LIVE)
    path = env["LOMBO_VISA_STATE_FILE"]
    done = []
    with state.locked(path):
        t = threading.Thread(target=lambda: done.append(a.post()[0]))
        t.start()
        time.sleep(0.4)
        assert not done  # the request is waiting for the lock a round (or `travel add`) holds
        assert not windows(env)
    t.join(10)
    assert done == [200]
    assert len(windows(env)) == 1


def test_two_requests_at_once_open_one_window(start, env):
    a = start(LIVE)
    codes = []
    threads = [threading.Thread(target=lambda: codes.append(a.post()[0])) for _ in range(5)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(15)
    assert sorted(codes) == [200, 422, 422, 422, 422]
    assert len(windows(env)) == 1


def test_a_damaged_state_file_is_a_502_and_is_left_alone(start, env):
    path = env["LOMBO_VISA_STATE_FILE"]
    with open(path, "w") as f:
        f.write("{broken")
    a = start(LIVE)
    assert a.post()[0] == 502
    assert a.call("GET", f"/v1/travel/{UID}")[0] == 502
    with open(path) as f:
        assert f.read() == "{broken"


def test_the_audit_record_has_no_token_and_no_address(start, env, tmp_path):
    audit_file = tmp_path / "audit.jsonl"
    a = start({**LIVE, "LOMBO_VISA_AUDIT_FILE": str(audit_file)}, clock=Clock())
    assert a.post()[0] == 200
    (row,) = [json.loads(x) for x in audit_file.read_text().splitlines()]
    assert row == {"ts": int(T0), "mode": "api", "decision": "self-window", "country": "", "user": "Pat", "ip_hash": ""}
    for p in tmp_path.iterdir():
        content = p.read_bytes()
        assert TOKEN.encode() not in content
        assert b"127.0.0.1" not in content


def test_the_log_never_holds_the_token_user_id_or_body(start):
    a = start(LIVE)
    a.post()
    a.call("GET", f"/v1/travel/{UID}")
    a.call("GET", f"/v1/travel/{UID}", token="wrong-token-value")
    a.post(user=ADMIN)
    text = "\n".join(a.logs)
    assert a.logs
    for secret in (TOKEN, "wrong-token-value", UID, ADMIN, "Pat", "127.0.0.1"):
        assert secret not in text


def test_an_unexpected_error_is_a_generic_500(start, monkeypatch):
    a = start(LIVE)

    def boom(self, user_id):
        raise RuntimeError("secret detail")

    monkeypatch.setattr(api.Travel, "status", boom)
    code, body, _ = a.call("GET", f"/v1/travel/{UID}")
    assert (code, body) == (500, {"ok": False, "message": "error"})
    assert "secret" not in "".join(a.logs)
    assert a.call("GET", "/v1/travel/" + "0" * 32)[0] == 500  # still serving


# ---------------------------------------------------------------- state and pruning
def test_prune_keeps_self_windows_for_the_cooldown_and_drops_others():
    st = state.empty()
    state.put_window(st, core.Window(1, UID, "Pat", None, T0, T0 + DAY), own=True)
    state.put_window(st, core.Window(2, UID, "Pat", None, T0, T0 + DAY))
    state.prune(st, T0 + 100 * DAY)
    assert set(st["windows"]) == {"1"}
    state.prune(st, T0 + 500 * DAY)
    assert not st["windows"]


# ---------------------------------------------------------------- settings and commands
def base(env, **extra):
    return {**env, **extra}


def test_api_is_off_without_a_token(env):
    cfg = from_env(env)
    assert cfg.api_token == ""
    assert cfg.api_bind == ("127.0.0.1", 8097)
    assert (cfg.self_max_days, cfg.self_cooldown_days) == (14, 30)


@pytest.mark.parametrize(
    ("name", "value", "text"),
    [
        ("API_TOKEN", "short", "at least 24"),
        ("API_TOKEN", "x" * 23, "at least 24"),
        ("API_TOKEN", "a" * 12 + " " + "a" * 12, "no spaces"),
        ("API_TOKEN", "a" * 30 + "é", "printable ASCII"),
        ("API_BIND", "localhost:8097", "API_BIND"),
        ("API_BIND", "127.0.0.1", "API_BIND"),
        ("API_BIND", "127.0.0.1:0", "API_BIND"),
        ("API_BIND", "127.0.0.1:70000", "API_BIND"),
        ("SELF_MAX_DAYS", "0", "at least 1"),
        ("SELF_MAX_DAYS", "many", "whole number"),
        ("SELF_MAX_DAYS", "31", "MAX_WINDOW_DAYS"),
        ("SELF_COOLDOWN_DAYS", "-1", "at least 0"),
        ("SELF_COOLDOWN_DAYS", "366", "between 0 and 365"),
        ("SELF_COOLDOWN_DAYS", "1.5", "whole number"),
    ],
)
def test_bad_api_settings_stop_start_up_and_name_the_variable(env, name, value, text):
    with pytest.raises(ConfigError) as e:
        from_env(base(env, **{PREFIX + name: value}))
    assert PREFIX in str(e.value)
    assert text in str(e.value)
    assert (name in str(e.value)) or name == "SELF_MAX_DAYS"


def test_self_max_days_follows_a_smaller_max_window_days_unless_set(env):
    assert from_env(base(env, LOMBO_VISA_MAX_WINDOW_DAYS="7")).self_max_days == 7
    assert from_env(base(env, LOMBO_VISA_MAX_WINDOW_DAYS="60", LOMBO_VISA_SELF_MAX_DAYS="60")).self_max_days == 60
    with pytest.raises(ConfigError):
        from_env(base(env, LOMBO_VISA_MAX_WINDOW_DAYS="7", LOMBO_VISA_SELF_MAX_DAYS="8"))


def test_the_api_command_refuses_to_start_without_a_token(env, capsys):
    assert main(["api"], env) == 2
    assert "LOMBO_VISA_API_TOKEN" in capsys.readouterr().err


def test_the_api_command_serves_until_stopped(env, monkeypatch, capsys):
    class Fake:
        server_port = 4321

        def serve_forever(self):
            raise KeyboardInterrupt

        def server_close(self):
            self.closed = True

    monkeypatch.setattr(api, "make_server", lambda cfg: Fake())
    assert main(["api"], base(env, LOMBO_VISA_API_TOKEN=TOKEN)) == 0
    out = capsys.readouterr()
    assert "listening on 127.0.0.1:4321" in out.out
    assert "dry run" in out.out
    assert TOKEN not in out.out + out.err


# ---------------------------------------------------------------- doctor
def doctor_output(env, capsys):
    main(["doctor"], env)
    return capsys.readouterr().out


def test_doctor_says_the_api_is_off_by_default(env, capsys):
    assert "self-service API: off" in doctor_output(env, capsys)


def test_doctor_explains_the_api_when_it_is_on(env, capsys):
    out = doctor_output(base(env, LOMBO_VISA_API_TOKEN=TOKEN), capsys)
    assert "self-service API: on at 127.0.0.1:8097" in out
    assert "14 day(s)" in out
    assert "30 day(s)" in out
    assert "no window is written" in out  # dry run is the default
    assert "[WARN] self-service API" not in out
    assert TOKEN not in out


def test_doctor_warns_about_a_weak_token_the_jellyfin_key_and_a_public_bind(env, capsys):
    out = doctor_output(base(env, LOMBO_VISA_API_TOKEN="a" * 30), capsys)
    assert "looks weak" in out
    key = "K3y-" + "AbCdEfGhIj0123456789" * 2
    out = doctor_output(base(env, LOMBO_VISA_API_TOKEN=key, LOMBO_VISA_JELLYFIN_API_KEY=key), capsys)
    assert "same as the Jellyfin key" in out
    out = doctor_output(base(env, LOMBO_VISA_API_TOKEN=TOKEN, LOMBO_VISA_API_BIND="0.0.0.0:8097"), capsys)
    assert "not a loopback address" in out


def test_doctor_notes_a_zero_cooldown_and_live_mode(env, capsys):
    out = doctor_output(base(env, LOMBO_VISA_API_TOKEN=TOKEN, LOMBO_VISA_SELF_COOLDOWN_DAYS="0", **LIVE), capsys)
    assert "(cool-down off)" in out
    assert "no window is written" not in out
