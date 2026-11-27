"""Setup and checks for run.sh: a throwaway Jellyfin, a real Caddy and the gateway in front of the sign-in paths."""

# ruff: noqa: S603, S607  (the only commands run are docker, with fixed names)

import http.client
import json
import os
import socket
import subprocess
import sys
import time
from typing import Any

S = os.environ["LF_SCRATCH"]
JF, CADDY = ("172.31.250.10", 8096), ("127.0.0.1", 18180)
HOME_IP, AWAY_IP = "99.224.0.1", "133.11.0.1"  # a Canadian and a Japanese network
PASSWORD = "lf-test-pass"  # noqa: S105  (throwaway server)
failures: list[str] = []


def call(
    where: tuple[str, int],
    method: str,
    path: str,
    body: Any = None,
    token: str = "",
    ip: str = "",
    device: str = "lf",
    encoding: str = "",
) -> tuple[int, Any]:
    auth = f'MediaBrowser Client="lf", Device="{device}", DeviceId="{device}", Version="1"'
    headers = {"Content-Type": "application/json", "Authorization": auth + (f', Token="{token}"' if token else "")}
    if ip:
        headers["X-Forwarded-For"] = ip
    if encoding:
        headers["Accept-Encoding"] = encoding
    c = http.client.HTTPConnection(*where, timeout=60)
    try:
        data = body if isinstance(body, bytes) else None if body is None else json.dumps(body).encode()
        c.request(method, path, data, headers)  # the path goes out exactly as written
        r = c.getresponse()
        raw = r.read().decode(errors="replace")
        try:
            return r.status, json.loads(raw)
        except ValueError:
            return r.status, raw[:200]
    except (OSError, http.client.HTTPException) as e:
        return 0, type(e).__name__
    finally:
        c.close()


def check(name: str, ok: bool, detail: object = "") -> None:
    print(("[ ok ] " if ok else "[FAIL] ") + name + ("" if ok or detail == "" else f"  -> {str(detail)[:160]}"))
    if not ok:
        failures.append(name)


def login(user: str, ip: str, device: str = "lf", path: str = "/Users/AuthenticateByName", body: Any = None):
    return call(CADDY, "POST", path, body or {"Username": user, "Pw": PASSWORD}, ip=ip, device=device)


def got_token(reply: tuple[int, Any]) -> bool:
    return isinstance(reply[1], dict) and bool(reply[1].get("AccessToken"))


def wait_for(where: tuple[str, int]) -> None:
    for _ in range(90):
        if call(where, "GET", "/System/Info/Public")[0] == 200:
            return
        time.sleep(2)
    sys.exit(f"[FAIL] nothing answers at {where}")


def setup() -> None:
    wait_for(JF)
    for _ in range(30):  # the wizard routes come up a moment after the public info
        if call(JF, "GET", "/Startup/Configuration")[0] == 200:
            break
        time.sleep(2)
    lang = {"UICulture": "en-US", "MetadataCountryCode": "US", "PreferredMetadataLanguage": "en"}
    call(JF, "POST", "/Startup/Configuration", lang)
    call(JF, "GET", "/Startup/User")
    call(JF, "POST", "/Startup/User", {"Name": "admin", "Password": PASSWORD})
    call(JF, "POST", "/Startup/Complete")
    token = call(JF, "POST", "/Users/AuthenticateByName", {"Username": "admin", "Pw": PASSWORD})[1]["AccessToken"]
    call(JF, "POST", "/Users/New", {"Name": "viewer", "Password": PASSWORD}, token)
    call(JF, "POST", "/Auth/Keys?app=lf-visa", token=token)
    key = next(k["AccessToken"] for k in call(JF, "GET", "/Auth/Keys", token=token)[1]["Items"])
    net = call(JF, "GET", "/System/Configuration/network", token=token)[1]
    net["KnownProxies"] = ["172.31.250.20", "172.31.250.30"]  # the gateway and Caddy
    call(JF, "POST", "/System/Configuration/network", net, token)
    with open(os.path.join(S, "key"), "w") as f:
        f.write(key)
    subprocess.run(["docker", "restart", "lf-visa-jf"], check=True, capture_output=True)
    wait_for(JF)
    print("[ ok ] throwaway Jellyfin ready")


def visa(*args: str) -> str:
    cmd = ["docker", "run", "--rm", "--network", "lf-visa-net", "--user", f"{os.getuid()}:{os.getgid()}"]
    cmd += ["--env-file", f"{S}/env", "-v", f"{S}/data:/data", "lf-visa:test", *args]
    r = subprocess.run(cmd, capture_output=True, text=True)
    return r.stdout + r.stderr


def run() -> None:
    wait_for(CADDY)
    time.sleep(2)
    admin = call(JF, "POST", "/Users/AuthenticateByName", {"Username": "admin", "Pw": PASSWORD})[1]["AccessToken"]

    r = login("viewer", HOME_IP, "home-dev")
    check("home country: password sign-in works", r[0] == 200 and got_token(r), r)
    r = login("viewer", "10.20.1.5", "lan-dev")
    check("home network: password sign-in works", r[0] == 200, r)
    r = login("viewer", AWAY_IP, "away-1")
    check("abroad: password sign-in refused with 403", r[0] == 403 and not got_token(r), r)

    user_id = call(JF, "GET", "/Users", token=admin)[1]
    viewer_id = next(u["Id"] for u in user_id if u["Name"] == "viewer")
    spellings = [
        "/users/authenticatebyname", "/USERS/AUTHENTICATEBYNAME", "/Users/AuthenticateByName/",
        "/Users/AuthenticateByName//", "//Users/AuthenticateByName", "/Users//AuthenticateByName",
        "/Users/%41uthenticateByName", "/Users/%2541uthenticateByName", "/Users%2FAuthenticateByName",
        "/Users/AuthenticateByName%2F", "/emby/Users/AuthenticateByName", "/mediabrowser/Users/AuthenticateByName",
        "/Users/./AuthenticateByName", "/x/../Users/AuthenticateByName", "/Users/AuthenticateByName;a",
        "/Users/AuthenticateByName.json", "/Users/AuthenticateByName%00", "/jellyfin/Users/AuthenticateByName",
        "/Users/AuthenticateByName?x=1", "/Users/Authenticate%C5%BFByName",
    ]  # fmt: skip
    for path in spellings:
        r = login("viewer", AWAY_IP, "away-2", path)
        check(f"abroad, odd spelling gets no token: {path}", not got_token(r), r)
    r = call(CADDY, "POST", f"/Users/{viewer_id}/Authenticate?pw={PASSWORD}", ip=AWAY_IP, device="away-3")
    check("abroad: legacy per-user sign-in gets no token", not got_token(r), r)
    for label, body in {
        "lower-case key": b'{"username":"viewer","pw":"lf-test-pass"}',
        "two names": b'{"Username":"viewer","userName":"admin","Pw":"lf-test-pass"}',
        "not json": b"Username=viewer&Pw=lf-test-pass",
    }.items():
        r = login("viewer", AWAY_IP, "away-4", body=body)
        check(f"abroad, {label}: no token", not got_token(r), r)
    r = login("viewer", f"{HOME_IP}, {AWAY_IP}", "away-5")
    check("a visitor adding a home address in front does not get in", not got_token(r) or r[0] != 200, r)

    def quick(ip: str, device: str, who: str, encoding: str = "") -> tuple[int, Any]:
        start = call(CADDY, "POST", "/QuickConnect/Initiate", ip=ip, device=device)
        if start[0] != 200:
            return start
        call(JF, "POST", f"/QuickConnect/Authorize?code={start[1]['Code']}", token=who)
        secret = {"Secret": start[1]["Secret"]}
        return call(
            CADDY, "POST", "/Users/AuthenticateWithQuickConnect", secret, ip=ip, device=device, encoding=encoding
        )

    mine = call(JF, "POST", "/Users/AuthenticateByName", {"Username": "viewer", "Pw": PASSWORD}, device="authorizer")[1]
    r = quick(HOME_IP, "qc-home", mine["AccessToken"])
    check("home country: Quick Connect works", r[0] == 200 and got_token(r), r)
    r = quick(AWAY_IP, "qc-away", mine["AccessToken"])
    check("abroad: Quick Connect refused", r[0] == 403 and not got_token(r), r)
    r = quick(AWAY_IP, "qc-away-gzip", mine["AccessToken"], encoding="gzip, br")
    check("abroad: Quick Connect refused when the app accepts compression", r[0] == 403, r)
    devices = [s.get("DeviceId") for s in call(JF, "GET", "/Sessions", token=admin)[1]]
    keys = call(JF, "GET", "/Devices", token=admin)[1].get("Items", [])
    check("abroad: refused Quick Connect left no session", "qc-away" not in devices, devices)
    print(f"       devices Jellyfin still lists: {sorted(d.get('Id', '') for d in keys)}")
    check("abroad: no refused attempt left a device", not [d for d in keys if d.get("Id", "").startswith("away")], keys)

    seen = {s.get("DeviceId"): s.get("RemoteEndPoint") for s in call(JF, "GET", "/Sessions", token=admin)[1]}
    check("Jellyfin records the visitor's address behind the gateway", seen.get("home-dev") == HOME_IP, seen)

    visa("run")  # the first round only notes who is already signed in
    print("       " + visa("travel", "add", "viewer", "1").strip())
    r = login("viewer", AWAY_IP, "trip-dev")
    check("abroad with a travel window: sign-in works", r[0] == 200 and got_token(r), r)
    with open(os.path.join(S, "data", "state.json")) as f:  # before any `run`: only the gateway can have written it
        noted = {k.partition(":")[2]: v.get("window") for k, v in json.load(f)["devices"].items()}
    check("the gateway noted the trip device from Jellyfin's own answer", bool(noted.get("trip-dev")), noted)
    trip = r[1].get("AccessToken") if isinstance(r[1], dict) else ""
    print("       " + visa("run").strip().replace("\n", "\n       "))
    r = login("admin", AWAY_IP, "away-6")
    check("abroad: a window for one person does not let another in", not got_token(r), r)
    listed = visa("travel", "list")
    visa("travel", "revoke", listed.split("#")[1].split()[0])
    print("       " + visa("run").strip().replace("\n", "\n       "))
    check("after revoke: the trip device is signed out", call(JF, "GET", "/Users/Me", token=trip)[0] == 401)
    r = login("viewer", AWAY_IP, "away-7")
    check("after revoke: abroad refused again", r[0] == 403, r)

    with open(os.path.join(S, "env")) as f:
        env = dict(line.strip().split("=", 1) for line in f)

    def api(method: str, path: str, token: str, body: Any = None) -> tuple[int, Any]:
        c = http.client.HTTPConnection("172.31.250.40", 8097, timeout=30)
        headers = {"Authorization": f"Bearer {env[token]}", "Content-Type": "application/json"}
        c.request(method, path, None if body is None else json.dumps(body).encode(), headers)
        r = c.getresponse()
        out = r.status, json.loads(r.read())
        c.close()
        return out

    admin_key, self_key = "LOMBO_VISA_ADMIN_TOKEN", "LOMBO_VISA_API_TOKEN"
    uid = viewer_id.replace("-", "")
    code, made = api("POST", "/v1/admin/windows", admin_key, {"user_id": uid, "days": 2})
    check("admin API: add a window", code == 200, made)
    r = login("viewer", AWAY_IP, "admin-trip")
    check("admin API: that window lets the sign-in through", r[0] == 200, r)
    listed = api("GET", "/v1/admin/windows", admin_key)[1]["windows"]
    check("admin API: it is listed", [w["id"] for w in listed] == [made.get("id")], listed)
    check("admin API: revoke", api("DELETE", f"/v1/admin/windows/{made.get('id')}", admin_key)[0] == 200)
    check("admin API: refused again after revoke", login("viewer", AWAY_IP, "away-8")[0] == 403)

    wrong = api("POST", "/v1/travel", self_key, {"user_id": uid, "days": 2, "password": "not-it"})
    check("password check: a wrong password opens nothing", wrong[0] == 422, wrong)
    right = api("POST", "/v1/travel", self_key, {"user_id": uid, "days": 2, "password": PASSWORD})
    check("password check: the right password opens a window", right[0] == 200 and right[1].get("active"), right)
    left = [s.get("DeviceId") for s in call(JF, "GET", "/Sessions", token=admin)[1]]
    check("password check: leaves no session behind", "lombo-visa-check" not in left, left)
    keys = call(JF, "GET", "/Devices", token=admin)[1].get("Items", [])
    check("password check: leaves no device behind", "lombo-visa-check" not in [d.get("Id") for d in keys], keys)

    c = http.client.HTTPConnection(*CADDY, timeout=20)
    c.request("GET", "/visa/ask", headers={"X-Forwarded-For": AWAY_IP})
    page = c.getresponse()
    check("ask page: served through the proxy", page.status == 200 and b'action="/visa/ask"' in page.read())
    c.request("POST", "/visa/ask", b"name=viewer", {"Content-Type": "application/x-www-form-urlencoded"})
    sent = c.getresponse()
    check("ask page: a form post is answered", sent.status == 200 and b"has been told" in sent.read())
    c.close()

    # Swamp the gateway with half-sent sign-ins: running out of its slots must not turn into "fail open".
    head = b"POST /Users/AuthenticateByName HTTP/1.1\r\nHost: x\r\nContent-Length: 500\r\n\r\n{"
    for amount in (80, 400):
        held = [socket.create_connection(CADDY, timeout=10) for _ in range(amount)]
        for sock in held:
            sock.sendall(head)
        time.sleep(1)
        replies = [login("viewer", AWAY_IP, f"flood-{amount}") for _ in range(10)]
        check(f"abroad while {amount} slow sign-ins hang: no token", not any(got_token(x) for x in replies), replies[0])
        print(f"       statuses seen: {sorted({x[0] for x in replies})}")
        for sock in held:
            sock.close()
        time.sleep(8)

    subprocess.run(["docker", "stop", "-t", "2", "lf-visa-gw"], check=True, capture_output=True)
    r = login("viewer", HOME_IP, "down-dev")
    check("gateway stopped: people can still sign in (fail open)", r[0] == 200 and got_token(r), r)

    print(f"\n{len(failures)} failed" if failures else "\nall passed")
    sys.exit(1 if failures else 0)


setup() if sys.argv[1] == "setup" else run()
