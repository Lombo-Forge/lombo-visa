"""The gateway, against a fake Jellyfin on a real socket (127.0.0.1, port 0). No real Jellyfin has been involved."""

import http.client
import ipaddress
import json
import socket
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

from lombo_visa import core, gateway, state
from lombo_visa.cli import main
from lombo_visa.config import ConfigError, from_env

HOME = "12.0.1.5"  # US in the test database
ABROAD = "11.0.1.9"  # DE in the test database
SECRETS = ["hunter2-pass", "tok-SECRET-token", "emby-auth-SECRET"]
SIGN_IN = "/Users/AuthenticateByName"
QUICK = "/Users/AuthenticateWithQuickConnect"
BODY = json.dumps({"Username": "Pat", "Pw": SECRETS[0]}).encode()
AUTH = {
    "Authorization": f'MediaBrowser Token="{SECRETS[1]}"',
    "X-Emby-Authorization": f'MediaBrowser Client="x", Token="{SECRETS[2]}"',
}


class Upstream:
    """A fake Jellyfin sign-in endpoint that records exactly what reached it."""

    def __init__(self):
        self.requests: list[dict] = []
        self.status = 200
        self.reason = "OK"
        self.headers = [
            ("Content-Type", "application/json"),
            ("X-Up", "1"),
            ("Set-Cookie", "a=1"),
            ("Set-Cookie", "b=2"),
        ]
        self.body = b'{"AccessToken":"up-token"}\xff\x00\r\n'
        self.delay = 0.0
        self.script = None  # optional: request dict -> (status, headers, body)
        outer = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.0"

            def log_message(self, *a):
                pass

            def handle_any(self):
                body = self.rfile.read(int(self.headers.get("Content-Length", 0)))
                req = {"method": self.command, "path": self.path, "headers": list(self.headers.items()), "body": body}
                outer.requests.append(req)
                time.sleep(outer.delay)
                status, headers, data = outer.script(req) if outer.script else (outer.status, outer.headers, outer.body)
                self.send_response_only(status, outer.reason)
                for k, v in headers:
                    self.send_header(k, v)
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            do_POST = do_GET = handle_any

        self.server = HTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.server.server_port}"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()


@pytest.fixture
def up():
    u = Upstream()
    yield u
    u.server.shutdown()


class Running:
    def __init__(self, server, logs, audit_file):
        self.server, self.logs, self.audit_file = server, logs, audit_file
        self.port = server.server_address[1]

    def send(self, path=SIGN_IN, body=BODY, ip=ABROAD, method="POST", headers=None):
        h = {**AUTH, "Content-Type": "application/json"}
        if ip:
            h["X-Forwarded-For"] = ip
        h.update(headers or {})
        c = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        c.putrequest(method, path, skip_accept_encoding=True)
        for k, v in h.items():
            c.putheader(k, v)
        c.putheader("Content-Length", str(len(body)))
        c.endheaders(body)
        r = c.getresponse()
        data = r.read()
        c.close()
        return r.status, r.getheaders(), data

    def audit_rows(self):
        try:
            with open(self.audit_file) as f:
                return [json.loads(x) for x in f.read().splitlines()]
        except FileNotFoundError:
            return []


@pytest.fixture
def start(env, tmp_path, up):
    servers = []

    def make(**over):
        e = {
            **env,
            "LOMBO_VISA_DRY_RUN": "false",
            "LOMBO_VISA_GATEWAY_MODE": "enforce",
            "LOMBO_VISA_GATEWAY_UPSTREAM": up.url,
            "LOMBO_VISA_TRUSTED_PROXIES": "127.0.0.1",
            "LOMBO_VISA_AUDIT_FILE": str(tmp_path / "audit.jsonl"),
            **over,
        }
        cfg = from_env(e)
        logs: list[str] = []
        server = gateway.GatewayServer(("127.0.0.1", 0), gateway.Gateway(cfg, log=logs.append))
        threading.Thread(target=server.serve_forever, daemon=True).start()
        servers.append(server)
        return Running(server, logs, cfg.audit_file)

    yield make
    for s in servers:
        s.shutdown()
        s.server_close()


def give_window(env, user_id="aaaa-bbbb", name="Pat", country=None, days=7):
    path = env["LOMBO_VISA_STATE_FILE"]
    st = state.load(path)
    state.put_window(st, core.new_window([], user_id, name, days, country, time.time(), 30))
    state.save(path, st)


# ---------------------------------------------------------------- allowed: byte for byte
def test_allowed_request_and_answer_are_forwarded_byte_for_byte(start, up):
    gw = start()
    target = SIGN_IN + "?Foo=Bar%20baz&x=1"
    status, headers, body = gw.send(target, ip=HOME, headers={"X-Custom": "keep me", "Connection": "keep-alive"})
    assert (status, body) == (200, up.body)
    assert ("X-Up", "1") in headers
    assert [v for k, v in headers if k == "Set-Cookie"] == ["a=1", "b=2"]
    (req,) = up.requests
    assert (req["method"], req["path"], req["body"]) == ("POST", target, BODY)  # path and query exactly as sent
    got = {k.lower(): v for k, v in req["headers"]}
    assert got["authorization"] == AUTH["Authorization"]
    assert got["x-emby-authorization"] == AUTH["X-Emby-Authorization"]
    assert got["x-custom"] == "keep me"
    assert got["x-forwarded-for"] == HOME
    assert got["content-length"] == str(len(BODY))
    assert "keep-alive" not in got.get("connection", "")  # hop-by-hop headers stop here


def test_a_redirect_from_jellyfin_is_returned_not_followed(start, up):
    up.status, up.reason, up.headers, up.body = 302, "Found", [("Location", "http://elsewhere.invalid/")], b""
    status, headers, _ = start().send(ip=HOME)
    assert status == 302
    assert ("Location", "http://elsewhere.invalid/") in headers
    assert len(up.requests) == 1


def test_upstream_that_is_down_gives_502(start):
    status, _, body = start(LOMBO_VISA_GATEWAY_UPSTREAM="http://127.0.0.1:1").send(ip=HOME)
    assert status == 502
    assert json.loads(body)["status"] == 502


def test_slow_upstream_times_out(start, up, monkeypatch):
    monkeypatch.setattr(gateway, "UPSTREAM_TIMEOUT", 0.3)
    up.delay = 1.5
    t = time.time()
    assert start().send(ip=HOME)[0] == 502
    assert time.time() - t < 1.4


# ---------------------------------------------------------------- refused
def test_foreign_sign_in_without_a_window_is_refused_before_jellyfin(start, up):
    gw = start()
    status, headers, body = gw.send(ip=ABROAD)
    assert status == 403
    assert ("Content-Type", "application/json") in headers
    assert json.loads(body)["status"] == 403
    assert up.requests == []
    (row,) = gw.audit_rows()
    assert (row["mode"], row["decision"], row["country"], row["user"]) == ("gateway", "deny", "DE", "Pat")


def test_a_travel_window_lets_the_sign_in_through(start, up, env):
    give_window(env)
    gw = start()
    assert gw.send(ip=ABROAD)[0] == 200
    assert gw.audit_rows()[-1]["decision"] == "allow-window"
    assert len(up.requests) == 1


def test_a_window_for_another_country_does_not_help(start, env):
    give_window(env, country="FR")
    assert start().send(ip=ABROAD)[0] == 403


def test_window_matches_by_name_when_jellyfin_cannot_be_asked(start, env):
    give_window(env)
    gw = start(LOMBO_VISA_JELLYFIN_URL="http://127.0.0.1:1")
    assert gw.send(ip=ABROAD)[0] == 200  # the typed name matches the window's name
    assert gw.send(body=json.dumps({"Username": "Nobody"}).encode())[0] == 403


def test_user_name_key_is_matched_without_case_and_ambiguous_names_are_refused(start, env):
    give_window(env)
    gw = start()
    assert gw.send(body=json.dumps({"username": "pat"}).encode())[0] == 200
    two = b'{"Username": "Pat", "username": "someone-else"}'
    assert gw.send(body=two)[0] == 403
    assert gw.send(body=b"not json")[0] == 403  # foreign, and no way to say who
    assert gw.send(body=b"not json", ip=HOME)[0] == 200  # at home that never matters


def test_exempt_users_are_never_refused(start):
    assert start(LOMBO_VISA_EXEMPT_USERS="pat").send(ip=ABROAD)[0] == 200


def test_legacy_sign_in_path_uses_the_id_in_the_path(start, env):
    gw = start()
    assert gw.send("/Users/aaaa-bbbb/Authenticate", body=b'{"Pw":"x"}')[0] == 403
    give_window(env)
    assert gw.send("/Users/aaaabbbb/Authenticate", body=b'{"Pw":"x"}')[0] == 200


# ---------------------------------------------------------------- every doubt allows
def test_no_database_allows(start):
    gw = start(LOMBO_VISA_GEOIP_DB="/nonexistent/country.mmdb")
    assert gw.send(ip=ABROAD)[0] == 200
    assert gw.audit_rows()[-1]["decision"] == "unknown"


def test_database_that_appears_later_is_picked_up(start, tmp_path, db_file):
    target = tmp_path / "late.mmdb"
    gw = start(LOMBO_VISA_GEOIP_DB=str(target))
    assert gw.send(ip=ABROAD)[0] == 200
    target.write_bytes(db_file.read_bytes())
    assert gw.send(ip=ABROAD)[0] == 403


@pytest.mark.parametrize("hop", ["not-an-ip", "", "999.1.1.1", "11.0.1.9, junk"])
def test_unknown_address_allows(start, hop):
    assert start().send(ip=hop)[0] == 200


def test_unreadable_windows_allow(start, env):
    with open(env["LOMBO_VISA_STATE_FILE"], "w") as f:
        f.write("{broken")
    gw = start()
    assert gw.send(ip=ABROAD)[0] == 200
    assert gw.audit_rows()[-1]["decision"] == "unknown"


@pytest.mark.parametrize("mode", ["off", "log"])
def test_modes_off_and_log_never_refuse(start, up, mode):
    gw = start(LOMBO_VISA_GATEWAY_MODE=mode)
    assert gw.send(ip=ABROAD)[0] == 200
    assert len(up.requests) == 1
    rows = gw.audit_rows()
    if mode == "log":
        assert rows[-1]["decision"] == "would-deny"
    else:
        assert rows == []


def test_dry_run_makes_enforce_act_as_log(start, up):
    gw = start(LOMBO_VISA_DRY_RUN="true")
    assert gw.send(ip=ABROAD)[0] == 200
    assert gw.audit_rows()[-1]["decision"] == "would-deny"
    assert len(up.requests) == 1


def test_serve_says_so_when_dry_run_forces_log(env, monkeypatch, capsys):
    monkeypatch.setattr(gateway.GatewayServer, "serve_forever", lambda self, *a, **k: None)
    e = {**env, "LOMBO_VISA_GATEWAY_MODE": "enforce", "LOMBO_VISA_GATEWAY_BIND": "127.0.0.1:18099"}
    assert main(["gateway"], e) == 0
    out = capsys.readouterr().out
    assert "mode log" in out
    assert "DRY_RUN=true" in out
    assert "nothing is refused" in out
    main(["gateway"], {**e, "LOMBO_VISA_GATEWAY_MODE": "off"})
    assert "passed through" in capsys.readouterr().out


# ---------------------------------------------------------------- Quick Connect
def qc_answer(name="Pat", uid="aaaabbbb"):
    return json.dumps({"User": {"Name": name, "Id": uid}, "AccessToken": SECRETS[1]}).encode()


def test_quick_connect_from_abroad_is_ended_and_refused(start, up):
    up.script = lambda req: (200, [("Content-Type", "application/json")], qc_answer())
    gw = start()
    status, _, body = gw.send(QUICK, body=b'{"Secret":"s"}')
    assert status == 403
    assert SECRETS[1].encode() not in body
    logout = up.requests[-1]
    assert logout["path"] == "/Sessions/Logout"
    assert dict(logout["headers"])["Authorization"] == f'MediaBrowser Token="{SECRETS[1]}"'
    assert gw.audit_rows()[-1]["decision"] == "deny"


def test_quick_connect_log_mode_home_window_and_odd_answers_allow(start, up, env):
    answer = qc_answer()
    up.script = lambda req: (200, [], answer)
    assert start(LOMBO_VISA_GATEWAY_MODE="log").send(QUICK, body=b"{}")[2] == answer
    assert len(up.requests) == 1  # nothing was ended
    up.requests.clear()
    gw = start()
    assert gw.send(QUICK, body=b"{}", ip=HOME)[2] == answer
    assert len(up.requests) == 1
    give_window(env)
    assert gw.send(QUICK, body=b"{}")[2] == answer
    up.script = lambda req: (401, [], b"no")
    status, _, body = gw.send(QUICK, body=b"{}")
    assert (status, body) == (401, b"no")
    up.script = lambda req: (200, [], b"not json")
    assert gw.send(QUICK, body=b"{}")[2] == b"not json"
    up.script = lambda req: (200, [], b'{"AccessToken": "t"}')
    assert gw.send(QUICK, body=b"{}")[0] == 200  # who it is was not said: allowed
    up.script = lambda req: (200, [], b"[1]")
    assert gw.send(QUICK, body=b"{}")[0] == 200


# ---------------------------------------------------------------- which paths
@pytest.mark.parametrize(
    ("target", "kind"),
    [
        ("/Users/AuthenticateByName", "password"),
        ("/users/authenticatebyname", "password"),
        ("/USERS/AUTHENTICATEBYNAME/", "password"),
        ("/Users/Authenticate%42yName", "password"),
        ("/emby/Users/AuthenticateByName", "password"),
        ("/MediaBrowser/users/authenticatebyname?x=1", "password"),
        ("/Users/AuthenticateWithQuickConnect", "quickconnect"),
        ("/Users/abc123/Authenticate", "password-legacy"),
        ("/emby/Users/abc123/authenticate", "password-legacy"),
    ],
)
def test_sign_in_paths_are_recognised(target, kind):
    found = gateway.classify(target)
    assert found is not None
    assert found[0] == kind


@pytest.mark.parametrize(
    "target",
    [
        "/",
        "/System/Info",
        "/Users",
        "/Users/Me",
        "/Users/AuthenticateByNameX",
        "/Users/AuthenticateByName/extra",
        "/Users/a/b/Authenticate",
        "/Users/AuthenticateByName%2f",
        "/Users%2fAuthenticateByName",
        "/Users/Authenticate%252542yName",
        "/Users/Authenticate%2542yName",
        "/Users//AuthenticateByName",
        "//Users/AuthenticateByName",
        "/Users/./AuthenticateByName",
        "/Users/x/../AuthenticateByName",
        "/Users/AuthenticateByName%00",
        "/Users/AuthenticateByName%0d%0a",
        "/Users/AuthenticateByName;x=1",
        "/Users\\AuthenticateByName",
        "/Users/AuthenticateByName%5c",
        "/Users/Authenticate%ffByName",
        "/Users/AuthenticateByName ",
        "/Users/AuthenticateByName\t",
        "/Users/AuthenticateByName%3fx",
        "http://example.invalid/Users/AuthenticateByName",
        "Users/AuthenticateByName",
        "/emby/emby/Users/AuthenticateByName",
        "/QuickConnect/Initiate",
        f"/Users/AuthenticateByN{chr(0x430)}me",  # a look-alike letter
    ],
)
def test_other_paths_and_tricks_are_not_sign_ins(target):
    assert gateway.classify(target) is None


def test_nothing_else_is_ever_forwarded(start, up):
    gw = start()
    for method, path in [
        ("POST", "/System/Info"),
        ("POST", "/Users/AuthenticateByName%2f"),
        ("POST", "/Users/Authenticate%252542yName"),
        ("GET", SIGN_IN),
        ("PUT", SIGN_IN),
        ("DELETE", SIGN_IN),
        ("FANCY", SIGN_IN),
        ("POST", "/Items"),
    ]:
        status, _, body = gw.send(path, method=method, ip=HOME)
        assert status == 404, (method, path)
        assert json.loads(body)["status"] == 404
    assert up.requests == []


# ---------------------------------------------------------------- who the client is
TRUST = (ipaddress.ip_network("127.0.0.0/8"), ipaddress.ip_network("10.0.0.0/8"))


def test_forwarded_header_is_believed_only_from_a_trusted_peer():
    ca = gateway.client_address
    assert ca("127.0.0.1", [ABROAD], TRUST) == (ABROAD, False)
    assert ca("11.0.1.1", [HOME], TRUST) == ("11.0.1.1", True)  # not trusted: the header is ignored (and noted)
    assert ca("11.0.1.1", [], TRUST) == ("11.0.1.1", False)
    assert ca("127.0.0.1", [], TRUST) == ("127.0.0.1", False)
    assert ca("127.0.0.1", [HOME], ()) == ("127.0.0.1", True)  # nobody is trusted
    assert ca("::ffff:127.0.0.1", [ABROAD], TRUST) == (ABROAD, False)


def test_the_rightmost_untrusted_hop_is_the_client():
    ca = gateway.client_address
    assert ca("127.0.0.1", [f"{HOME}, {ABROAD}, 10.0.0.2"], TRUST)[0] == ABROAD  # a forged left part is ignored
    assert ca("127.0.0.1", [HOME, f"{ABROAD}, 10.0.0.2"], TRUST)[0] == ABROAD  # two header lines count as one list
    assert ca("127.0.0.1", ["10.0.0.5, 10.0.0.2"], TRUST)[0] == "10.0.0.5"  # all trusted: the first
    assert ca("127.0.0.1", [f"{ABROAD}, junk"], TRUST)[0] is None  # cannot tell
    assert ca("junk", [ABROAD], TRUST)[0] is None


def test_without_a_trusted_proxy_the_header_is_ignored_and_said_so_once(start):
    gw = start(LOMBO_VISA_TRUSTED_PROXIES="")
    assert gw.send(ip=ABROAD)[0] == 200  # the peer is 127.0.0.1: own network
    assert gw.audit_rows()[-1]["country"] == core.LAN
    gw.send(ip=ABROAD)
    assert len([m for m in gw.logs if "ignored" in m]) == 1


def test_trusted_proxy_makes_the_header_count(start):
    assert start().send(ip=ABROAD)[0] == 403


def test_the_peer_address_is_used_when_there_is_no_header(start):
    gw = start()
    assert gw.send(ip=None)[0] == 200
    assert gw.audit_rows()[-1]["country"] == core.LAN


# ---------------------------------------------------------------- limits
def raw(port, data: bytes, wait=3.0) -> bytes:
    s = socket.create_connection(("127.0.0.1", port), timeout=wait)
    s.sendall(data)
    out = b""
    try:
        while chunk := s.recv(4096):
            out += chunk
    except OSError:
        pass
    s.close()
    return out


def test_oversize_body_is_refused(start, up):
    gw = start()
    assert gw.send(body=b"x" * (gateway.MAX_BODY + 1), ip=HOME)[0] == 413
    assert up.requests == []
    assert gw.send(body=b"x" * gateway.MAX_BODY, ip=HOME)[0] == 200


def test_malformed_requests(start, up):
    gw = start()
    head = f"POST {SIGN_IN} HTTP/1.0\r\nX-Forwarded-For: {HOME}\r\n"
    assert raw(gw.port, f"{head}Transfer-Encoding: chunked\r\n\r\n0\r\n\r\n".encode()).startswith(b"HTTP/1.0 400")
    assert raw(gw.port, f"{head}Content-Length: abc\r\n\r\n".encode()).startswith(b"HTTP/1.0 400")
    assert raw(gw.port, f"{head}Content-Length: -5\r\n\r\n".encode()).startswith(b"HTTP/1.0 400")
    many = "".join(f"X-H{i}: v\r\n" for i in range(gateway.MAX_HEADERS + 1))
    assert raw(gw.port, f"{head}{many}\r\n".encode()).startswith(b"HTTP/1.0 431")
    big = f"X-Big: {'a' * 9000}\r\nX-Big2: {'b' * 9000}\r\n"
    assert raw(gw.port, f"{head}{big}\r\n".encode()).startswith(b"HTTP/1.0 431")
    assert up.requests == []


def test_a_client_that_stalls_is_dropped(start, up, monkeypatch):
    monkeypatch.setattr(gateway._Handler, "timeout", 0.3)
    gw = start()
    t = time.time()
    assert raw(gw.port, b"POST /Users/AuthenticateByName HTTP/1.0\r\nX-A: b") == b""  # headers never finished
    out = raw(gw.port, f"POST {SIGN_IN} HTTP/1.0\r\nContent-Length: 10\r\n\r\nabc".encode())  # body never finished
    assert out == b"" or out.startswith(b"HTTP/1.0 400")
    assert time.time() - t < 3
    assert up.requests == []


def test_whole_request_deadline_drops_a_slow_trickle(start, monkeypatch):
    monkeypatch.setattr(gateway, "REQUEST_SECONDS", 0.4)
    gw = start()
    s = socket.create_connection(("127.0.0.1", gw.port), timeout=5)
    s.sendall(b"POST /Users/AuthenticateByName HTTP/1.0\r\n")
    t = time.time()
    dropped = False
    for _ in range(30):  # a byte every 0.1 s would keep a plain read timeout alive for ever
        time.sleep(0.1)
        try:
            s.sendall(b"X")
        except OSError:
            dropped = True
            break
    s.close()
    assert dropped
    assert time.time() - t < 3


def test_connection_limit(start, monkeypatch):
    monkeypatch.setattr(gateway, "MAX_CONNECTIONS", 1)
    gw = start()
    hold = socket.create_connection(("127.0.0.1", gw.port))  # takes the only slot and says nothing
    try:
        time.sleep(0.2)
        assert raw(gw.port, b"").startswith(b"HTTP/1.0 503")
    finally:
        hold.close()
    time.sleep(0.3)
    assert gw.send(ip=HOME)[0] == 200  # the slot came back


# ---------------------------------------------------------------- nothing secret in any output
def test_no_secret_appears_in_logs_output_or_audit(start, up, env, capsys, tmp_path):
    give_window(env)
    up.script = lambda req: (200, [], qc_answer())
    gw = start()
    gw.send(ip=ABROAD)  # allowed by the window
    gw.send(body=json.dumps({"Username": "Nobody", "Pw": SECRETS[0]}).encode())  # refused
    gw.send(QUICK, body=b'{"Secret":"s"}', ip=HOME)
    gw.send(QUICK, body=b'{"Secret":"s"}', ip="11.0.1.9, 1.2.3.4")
    other = start(LOMBO_VISA_GATEWAY_MODE="log")
    other.send(ip=f"{ABROAD}, 1.2.3.4")
    broken = start(LOMBO_VISA_GATEWAY_UPSTREAM="http://127.0.0.1:1")
    broken.send(ip=HOME)  # upstream trouble
    out = capsys.readouterr()
    everything = "\n".join(gw.logs + other.logs + broken.logs) + out.out + out.err
    everything += (tmp_path / "audit.jsonl").read_text()
    for secret in [*SECRETS, "up-token", ABROAD, HOME, "1.2.3.4", "127.0.0.1"]:
        assert secret not in everything, secret
    assert "Pat" in everything  # names are logged, secrets are not


def test_credentials_go_only_to_the_configured_upstream(start, up, env):
    other = Upstream()
    try:
        give_window(env)
        gw = start(LOMBO_VISA_JELLYFIN_URL=other.url)  # the address of the API-key Jellyfin is a different server
        assert gw.send(ip=ABROAD)[0] == 200
        assert [r["body"] for r in up.requests] == [BODY]
        assert all(
            r["path"] == "/Users" and r["body"] == b"" for r in other.requests
        )  # only the name lookup went there
        assert all(SECRETS[1] not in str(r["headers"]) for r in other.requests)  # and none of the person's credentials
    finally:
        other.server.shutdown()


# ---------------------------------------------------------------- settings and doctor
def test_gateway_settings_defaults_and_values(env):
    cfg = from_env(env)
    assert cfg.gateway_mode == "log"
    assert cfg.gateway_bind == ("127.0.0.1", 8099)
    assert cfg.gateway_upstream == cfg.jellyfin_url
    assert cfg.proxy_networks == ()
    assert cfg.audit_file == ""
    cfg = from_env(
        {
            **env,
            "LOMBO_VISA_GATEWAY_MODE": "ENFORCE",
            "LOMBO_VISA_GATEWAY_BIND": "[::1]:9000",
            "LOMBO_VISA_GATEWAY_UPSTREAM": "https://jf.example.com:8920/",
            "LOMBO_VISA_TRUSTED_PROXIES": "172.18.0.0/16, 10.0.0.1, fd00::/8",
        }
    )
    assert cfg.gateway_mode == "enforce"
    assert cfg.gateway_bind == ("::1", 9000)
    assert cfg.gateway_upstream == "https://jf.example.com:8920"
    assert len(cfg.proxy_networks) == 3
    assert cfg.gateway_effective_mode == "log"  # DRY_RUN is still true
    live = from_env({**env, "LOMBO_VISA_DRY_RUN": "false", "LOMBO_VISA_GATEWAY_MODE": "enforce"})
    assert live.gateway_effective_mode == "enforce"


@pytest.mark.parametrize(
    ("name", "value"),
    [
        ("GATEWAY_MODE", "block"),
        ("GATEWAY_MODE", "true"),
        ("GATEWAY_BIND", "8099"),
        ("GATEWAY_BIND", "localhost:8099"),
        ("GATEWAY_BIND", "127.0.0.1:0"),
        ("GATEWAY_BIND", "127.0.0.1:99999"),
        ("GATEWAY_BIND", "127.0.0.1:http"),
        ("GATEWAY_UPSTREAM", "jellyfin:8096"),
        ("GATEWAY_UPSTREAM", "http://user:pw@jellyfin:8096"),
        ("GATEWAY_UPSTREAM", "http://jellyfin:8096/jf"),
        ("TRUSTED_PROXIES", "my-proxy"),
        ("TRUSTED_PROXIES", "10.0.0.0/33"),
    ],
)
def test_bad_gateway_settings_name_the_variable(env, name, value):
    with pytest.raises(ConfigError, match="LOMBO_VISA_" + name):
        from_env({**env, "LOMBO_VISA_" + name: value})


def doctor(env, capsys, **over):
    code = main(["doctor"], {**env, "LOMBO_VISA_GATEWAY_UPSTREAM": env["LOMBO_VISA_JELLYFIN_URL"], **over})
    return code, capsys.readouterr().out


def test_doctor_checks_the_gateway(env, capsys, tmp_path):
    code, out = doctor(env, capsys)
    assert code == 0
    assert "gateway: mode log on 127.0.0.1:8099" in out
    assert "LOMBO_VISA_TRUSTED_PROXIES is empty" in out
    assert "not a loopback" not in out
    assert "gateway upstream reachable" in out
    assert "audit log: off" in out

    _, out = doctor(
        env, capsys, LOMBO_VISA_GATEWAY_BIND="0.0.0.0:8099", LOMBO_VISA_TRUSTED_PROXIES="0.0.0.0/0,10.0.0.1"
    )
    assert "0.0.0.0:8099 is not a loopback" in out
    assert "0.0.0.0/0 is very wide" in out
    assert "10.0.0.1 is very wide" not in out
    assert "TRUSTED_PROXIES is empty" not in out

    _, out = doctor(env, capsys, LOMBO_VISA_GATEWAY_MODE="enforce")
    assert "act as log" in out

    code, out = doctor(env, capsys, LOMBO_VISA_GATEWAY_UPSTREAM="http://127.0.0.1:1")
    assert "[WARN] gateway upstream" in out

    _, out = doctor(env, capsys, LOMBO_VISA_GATEWAY_MODE="off")
    assert "gateway: mode off" in out
    assert "TRUSTED_PROXIES" not in out

    _, out = doctor(env, capsys, LOMBO_VISA_AUDIT_FILE=str(tmp_path / "a" / "audit.jsonl"))
    assert "audit log:" in out
    assert "[FAIL]" not in out
