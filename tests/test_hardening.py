"""Regression tests for the findings of the independent audit (see docs/security-review.md, "Audit findings").

Each test reproduces the audit's case; every one failed on the code as audited.
"""

import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import pytest

from conftest import session
from lombo_visa import cli, core, notify, state
from lombo_visa.cli import main
from lombo_visa.config import ConfigError, from_env
from lombo_visa.jellyfin import Jellyfin, JellyfinError

BLOCK = {"LOMBO_VISA_DRY_RUN": "false", "LOMBO_VISA_BLOCK_SESSIONS": "true"}
LIVE = {"LOMBO_VISA_DRY_RUN": "false"}
HOME_IP = "12.0.1.5"  # US in the test database
ABROAD_IP = "11.0.1.9"  # DE in the test database


def baseline(env, fake, *existing):
    fake.sessions = list(existing)
    assert main(["run"], env) == 0


def state_path(env) -> Path:
    return Path(env["LOMBO_VISA_STATE_FILE"])


def serve(handler):
    httpd = HTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    return httpd, f"http://127.0.0.1:{httpd.server_port}"


# ---------------------------------------------------------------- 1: home country codes
def test_uk_is_refused_with_a_hint(env):
    with pytest.raises(ConfigError, match=r"LOMBO_VISA_HOME_COUNTRIES.*use GB"):
        from_env({**env, "LOMBO_VISA_HOME_COUNTRIES": "UK"})
    with pytest.raises(ConfigError, match="not an ISO 3166"):
        from_env({**env, "LOMBO_VISA_HOME_COUNTRIES": "US,QQ"})
    assert from_env({**env, "LOMBO_VISA_HOME_COUNTRIES": "gb, us"}).home_countries == {"GB", "US"}


def test_the_country_list_is_complete():
    assert len(core.COUNTRY_CODES) == 250  # 249 ISO codes and XK
    assert {"GB", "US", "CA", "DE", "FR", "AU", "NZ", "JP", "ZW"} <= core.COUNTRY_CODES
    assert not {"UK", "EU", "ZZ", "XX", "AP"} & core.COUNTRY_CODES


def test_travel_add_refuses_uk(env, fake, capsys):
    assert main(["travel", "add", "pat", "3", "--country", "uk"], env) == 1
    assert "United Kingdom is GB" in capsys.readouterr().err


def test_doctor_warns_when_no_home_country_is_in_the_database(env, fake, capsys):
    assert main(["doctor"], {**env, "LOMBO_VISA_HOME_COUNTRIES": "AQ"}) == 0
    assert "[WARN] home countries: none of AQ appears" in capsys.readouterr().out
    assert main(["doctor"], env) == 0
    assert "[WARN] home countries" not in capsys.readouterr().out


# ---------------------------------------------------------------- 2: nothing ends the loop
def test_a_truncated_answer_is_a_jellyfin_error():
    class Truncated(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def do_GET(self):
            self.send_response(200)
            self.send_header("Content-Length", "100")
            self.end_headers()
            self.wfile.write(b"[1,2")

    httpd, url = serve(Truncated)
    try:
        with pytest.raises(JellyfinError, match="IncompleteRead"):
            Jellyfin(url, "k").sessions()
    finally:
        httpd.shutdown()


def test_a_webhook_url_with_a_space_is_a_notify_error():
    with pytest.raises(notify.NotifyError, match="webhook failed"):
        notify.send("http://127.0.0.1:1/a b", "x", timeout=1)


def test_a_bad_webhook_url_does_not_break_the_round(env, fake, capsys):
    baseline(env, fake)
    fake.sessions = [session(device="n", ip=ABROAD_IP)]
    assert main(["run"], {**env, **BLOCK, "LOMBO_VISA_WEBHOOK_URL": "http://127.0.0.1:1/a b"}) == 0
    assert "webhook failed" in capsys.readouterr().err
    assert fake.deleted() == ["n"]


def test_an_unexpected_error_is_reported_and_the_loop_goes_on(env, monkeypatch, capsys):
    rounds = []

    def boom(cfg, now):
        rounds.append(now)
        raise RuntimeError("boom")

    sleeps = []

    def sleep(seconds):
        sleeps.append(seconds)
        if len(sleeps) == 2:
            raise KeyboardInterrupt

    monkeypatch.setattr(cli, "run_once", boom)
    monkeypatch.setattr(cli.time, "sleep", sleep)
    with pytest.raises(KeyboardInterrupt):
        main(["run", "--loop"], env)
    assert len(rounds) == 2
    assert "[FAIL] unexpected RuntimeError: boom" in capsys.readouterr().err
    assert main(["run"], env) == 1  # one-shot: reported, exit code 1, no traceback


def test_other_commands_do_not_end_with_a_traceback(env, monkeypatch, capsys):
    def boom(*a):
        raise RuntimeError("boom")

    monkeypatch.setattr(cli.geoip, "update", boom)
    assert main(["geoip", "update"], env) == 1
    assert "unexpected RuntimeError" in capsys.readouterr().err


# ---------------------------------------------------------------- 3: no lost updates
def test_travel_add_during_a_round_is_not_lost(env, fake, monkeypatch, capsys):
    baseline(env, fake)
    real = Jellyfin.sessions
    threads: list[threading.Thread] = []

    def slow(self):
        t = threading.Thread(target=lambda: main(["travel", "add", "pat", "5"], env))
        t.start()
        t.join(0.5)  # with the lock it is still waiting here; without it, it has already saved
        threads.append(t)
        return real(self)

    monkeypatch.setattr(Jellyfin, "sessions", slow)
    assert main(["run"], env) == 0
    threads[0].join(10)
    assert not threads[0].is_alive()
    capsys.readouterr()
    assert main(["travel", "list"], env) == 0
    assert "#1 Pat" in capsys.readouterr().out


def test_state_is_written_through_a_unique_temp_file(env, fake):
    baseline(env, fake)
    assert sorted(p.name for p in state_path(env).parent.glob("*") if p.name.startswith("state.json")) == [
        "state.json",
        "state.json.lock",
    ]
    assert not list(state_path(env).parent.glob(".state-*"))


# ---------------------------------------------------------------- 4: a damaged state file
def test_a_damaged_state_file_is_kept_aside(env, fake, capsys):
    baseline(env, fake)
    assert main(["travel", "add", "pat", "5"], env) == 0
    sp = state_path(env)
    damaged = sp.read_text()[:-20]
    sp.write_text(damaged)
    capsys.readouterr()
    fake.sessions = [session(device="cccc", ip=ABROAD_IP)]
    assert main(["run"], env) == 0
    kept = list(sp.parent.glob("state.json.bad-*"))
    assert [k.read_text() for k in kept] == [damaged]
    assert "damaged" in capsys.readouterr().err
    assert json.loads(sp.read_text())["windows"] == {}  # fresh, as the warning says


def test_an_empty_state_file_is_also_kept_aside(env, fake):
    sp = state_path(env)
    sp.write_text("")
    baseline(env, fake)
    assert len(list(sp.parent.glob("state.json.bad-*"))) == 1


def test_an_unreadable_state_file_skips_the_round(env, fake, capsys):
    state_path(env).mkdir()  # reading it fails with an I/O error, not "not found"
    fake.sessions = [session(device="n", ip=ABROAD_IP)]
    assert main(["run"], {**env, **BLOCK}) == 1
    assert "cannot read" in capsys.readouterr().err
    assert (fake.deleted(), fake.posts()) == ([], [])


def test_doctor_and_plan_never_touch_a_damaged_state_file(env, fake, capsys):
    sp = state_path(env)
    sp.write_text("{not json")
    assert main(["doctor"], env) == 1
    assert "[FAIL] state file" in capsys.readouterr().out
    assert main(["plan"], env) == 1
    assert main(["travel", "list"], env) == 1
    assert sp.read_text() == "{not json"
    assert list(sp.parent.glob("state.json.bad-*")) == []


def test_doctor_does_not_create_the_state_file(env, fake):
    assert main(["doctor"], env) == 0
    assert not state_path(env).exists()


# ---------------------------------------------------------------- 5: a shared device id
def test_window_expiry_leaves_a_shared_device_alone(env, fake):
    baseline(env, fake)
    assert main(["travel", "add", "pat", "5"], env) == 0
    fake.sessions = [
        session(user="Pat", device="SHARED", ip=ABROAD_IP),
        session(user="Bob", uid="cccc-dddd", device="SHARED", ip=HOME_IP),
    ]
    assert main(["run"], {**env, **BLOCK}) == 0
    assert main(["travel", "revoke", "1"], env) == 0
    fake.sessions = []
    assert main(["run"], {**env, **BLOCK}) == 0
    assert fake.deleted() == []
    devices = json.loads(state_path(env).read_text())["devices"]
    assert set(devices) == {"ccccdddd:SHARED"}  # the traveller's entry is forgotten
    assert "also used by another account" in fake.posts()[-1]["body"]["content"]


def test_two_new_sessions_with_one_device_id_in_one_round(env, fake):
    baseline(env, fake)
    fake.sessions = [
        session(user="Pat", device="SHARED", ip=ABROAD_IP),
        session(user="Bob", uid="cccc-dddd", device="SHARED", ip=HOME_IP),
    ]
    assert main(["run"], {**env, **BLOCK}) == 0
    assert fake.deleted() == []
    assert "Not ended: its device id also belongs to another account" in fake.posts()[0]["body"]["content"]


def _plan(sessions, lookup):
    return core.plan_round(sessions, {}, {}, [], frozenset({"US"}), (), frozenset(), lookup, 1000.0, 3)


def _sess(user, uid, ip, device="X"):
    return core.Session(uid, user, device, "dev", "client", ip)


def test_an_unknown_session_with_the_same_device_id_also_counts():
    lookup = {"11.0.1.9": "DE"}.get
    p = _plan([_sess("pat", "u1", "11.0.1.9"), _sess("bob", "u2", "8.8.8.8")], lookup)
    assert [(i.verdict, i.clash, i.revoke) for i in p.items] == [("deny", True, False), ("unknown", False, False)]
    alone = _plan([_sess("pat", "u1", "11.0.1.9")], lookup)
    assert alone.items[0].revoke
    # a second denied session with that id does not protect it: both are foreign
    both = _plan([_sess("pat", "u1", "11.0.1.9"), _sess("bob", "u2", "11.0.1.9")], lookup)
    assert all(i.revoke for i in both.items)


# ---------------------------------------------------------------- 6 (download side) is in test_geoip.py
# ---------------------------------------------------------------- 7: an unknown session is never judged later
def test_a_session_let_through_as_unknown_is_not_ended_later(env, fake, tmp_path, db_file):
    baseline(env, fake)
    fake.sessions = [session(device="d1", ip=ABROAD_IP)]
    no_db = {**env, **BLOCK, "LOMBO_VISA_GEOIP_DB": str(tmp_path / "later.mmdb")}
    assert main(["run"], no_db) == 0
    assert fake.deleted() == []
    assert "Could not tell" in fake.posts()[0]["body"]["content"]
    entry = json.loads(state_path(env).read_text())["devices"]["aaaabbbb:d1"]
    assert entry["unknown"] is True
    (tmp_path / "later.mmdb").write_bytes(db_file.read_bytes())  # the database arrives
    assert main(["run"], no_db) == 0
    assert fake.deleted() == []
    fake.sessions.append(session(device="d2", ip=ABROAD_IP))  # a genuinely new one is judged
    assert main(["run"], no_db) == 0
    assert fake.deleted() == ["d2"]


# ---------------------------------------------------------------- 8: pseudo countries
@pytest.mark.parametrize("code", ["ZZ", "XX", "EU", "AP", "A1", "A2", "O1", "zz"])
def test_pseudo_country_codes_are_unknown(code):
    assert core.country_of("11.0.1.9", lambda ip: code) == "unknown"
    assert core.decide("unknown", frozenset({"US"}), [], "u", 0)[0] == "unknown"


# ---------------------------------------------------------------- 10: chat markup
def test_alert_text_carries_no_chat_markup():
    hostile = "<!channel> @everyone [pay](http://evil.example) <http://x|click> `code`"
    s = core.Session("u", hostile, "d", hostile, hostile, "11.0.1.9")
    for verdict, clash in (("deny", False), ("deny", True), ("unknown", False)):
        item = core.Item(s, "u:d", "DE", verdict, None, clash, False, True)
        text = core.alert_text(item, ended=False)
        assert not set("<>@[]|`") & set(text)
        assert "channel" in text  # still readable, only defused


def test_log_lines_keep_ordinary_names():
    assert core.clean_text("j.smith@mail.example") == "j.smith@mail.example"


# ---------------------------------------------------------------- 11: misspelled setting names
def test_a_misspelled_setting_name_stops_the_app(env, capsys):
    with pytest.raises(ConfigError, match="Did you mean LOMBO_VISA_EXEMPT_USERS"):
        from_env({**env, "LOMBO_VISA_EXEMPT_USER": "pat"})
    with pytest.raises(ConfigError, match="LOMBO_VISA_DRY_RUN"):
        from_env({**env, "LOMBO_VISA_DRY_RUM": "false"})
    with pytest.raises(ConfigError, match="is not a setting"):
        from_env({**env, "LOMBO_VISA_QQQQQQQQ": "1"})
    assert main(["plan"], {**env, "LOMBO_VISA_EXEMPT_USER": "pat"}) == 2
    assert "EXEMPT_USERS" in capsys.readouterr().err
    from_env({**env, "SOMETHING_ELSE": "fine"})  # other programs' variables are none of our business


# ---------------------------------------------------------------- 12: no redirects with the key
def test_the_api_key_is_not_sent_on_a_redirect(fake):
    class Redirect(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def do_GET(self):
            self.send_response(302)
            self.send_header("Location", fake.url + "/Sessions")
            self.send_header("Content-Length", "0")
            self.end_headers()

        do_DELETE = do_GET

    httpd, url = serve(Redirect)
    try:
        with pytest.raises(JellyfinError, match=r"redirect to .*Sessions.*Set LOMBO_VISA_JELLYFIN_URL"):
            Jellyfin(url, "good-key").sessions()
        with pytest.raises(JellyfinError, match="redirect"):
            Jellyfin(url, "good-key").sign_out_device("d")
    finally:
        httpd.shutdown()
    assert fake.requests == []


# ---------------------------------------------------------------- 14: smaller points
@pytest.mark.parametrize("exempt", ["aaaa-bbbb", "aaaabbbb", "AAAA-BBBB"])
def test_exempt_account_ids_match_with_or_without_dashes(exempt):
    p = core.plan_round(
        [_sess("pat", "AAAA-BBBB", "11.0.1.9")],
        {},
        {},
        [],
        frozenset({"US"}),
        (),
        frozenset({exempt.lower()}),  # config lower-cases the text as typed
        {"11.0.1.9": "DE"}.get,
        1000.0,
        3,
    )
    assert p.items[0].verdict == "exempt"


def test_exempt_ids_with_dashes_work_through_the_settings(env, fake):
    baseline(env, fake)
    fake.sessions = [session(user="Pat", uid="AAAA-BBBB", device="n", ip=ABROAD_IP)]
    assert main(["run"], {**env, **BLOCK, "LOMBO_VISA_EXEMPT_USERS": "aaaa-bbbb"}) == 0
    assert fake.deleted() == []


def test_dry_run_records_sessions_but_sends_and_ends_nothing(env, fake):
    baseline(env, fake)
    fake.sessions = [session(device="h", ip=HOME_IP)]
    assert main(["run"], env) == 0
    assert "aaaabbbb:h" in json.loads(state_path(env).read_text())["devices"]  # what the docs now say
    assert (fake.deleted(), fake.posts()) == ([], [])


def test_state_load_survives_a_deeply_nested_file(tmp_path):
    p = tmp_path / "s.json"
    p.write_text("[" * 100_000)
    assert state.load(str(p)) == state.empty()
    assert len(list(tmp_path.glob("s.json.bad-*"))) == 1
