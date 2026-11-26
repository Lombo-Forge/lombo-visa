"""The gateway's optional "ask to be let in" page. Fake Jellyfin and fake webhook only."""

# ruff: noqa: F811  (the imported fixtures are named again as test arguments)

import http.client
import json
import threading

from lombo_visa import ask, notify
from lombo_visa.config import from_env
from test_gateway import ABROAD, start, up  # noqa: F401

T0 = 1_800_000_000.0


# ---------------------------------------------------------------- ask page
def get(gw, path):
    c = http.client.HTTPConnection("127.0.0.1", gw.port, timeout=10)
    c.request("GET", path)
    r = c.getresponse()
    data = r.read()
    c.close()
    return r.status, data


def form(gw, name, ip=ABROAD):
    headers = {"Content-Type": "application/x-www-form-urlencoded"}
    return gw.send(ask.PATH, f"name={name}".encode(), ip=ip, headers=headers)


def test_the_ask_page_is_off_by_default(start, up):
    gw = start()
    assert get(gw, ask.PATH)[0] == 404
    assert form(gw, "Pat")[0] == 404
    assert "away" not in json.loads(gw.send()[2])
    assert up.requests == []


def test_the_ask_page_sends_one_cleaned_alert_without_the_address(start, up, monkeypatch):
    sent, done = [], threading.Event()
    monkeypatch.setattr(notify, "send", lambda url, text: (sent.append(text), done.set()))
    gw = start(LOMBO_VISA_ASK_PAGE="true", LOMBO_VISA_WEBHOOK_URL="http://hook.invalid/x")
    assert get(gw, ask.PATH) == (200, ask.PAGE.encode())
    assert json.loads(gw.send()[2])["away"] == ask.PATH
    status, _, body = form(gw, "Pat+%40everyone")
    assert (status, body.decode()) == (200, ask.ANSWER)
    assert done.wait(5)
    assert form(gw, "Pat+%40everyone")[0] == 200  # same name: no second alert
    (text,) = sent
    assert "Pat" in text
    assert "@" not in text
    assert ABROAD not in text
    assert form(gw, "")[0] == 422
    assert up.requests == []  # nothing of this ever reaches Jellyfin


def test_asking_is_limited_per_address_and_in_all(env):
    now = [T0]
    asks = ask.Asks(from_env(env), lambda _: None, lambda: now[0])
    assert [asks.ask(f"n{i}", "1.1.1.1", "DE")[0] for i in range(4)] == [200, 200, 200, 429]
    results = [asks.ask(f"m{i}", f"10.0.{i}.1", "DE")[0] for i in range(ask.IN_ALL)]
    assert results.count(200) == ask.IN_ALL - ask.PER_ADDRESS
    now[0] += ask.HOUR
    assert asks.ask("later", "1.1.1.1", "DE")[0] == 200


def test_your_own_ask_page_and_a_json_post(start, tmp_path):
    page = tmp_path / "away.html"
    page.write_text("<h1>Ask Sam</h1>")
    gw = start(LOMBO_VISA_ASK_PAGE="true", LOMBO_VISA_ASK_PAGE_FILE=str(page))
    assert get(gw, ask.PATH) == (200, b"<h1>Ask Sam</h1>")
    assert gw.send(ask.PATH, b'{"name":"Pat"}')[0] == 200
    assert ask.name_from("application/json", b"[1]") == ""
