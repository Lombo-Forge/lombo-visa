import json
import re
from pathlib import Path

import pytest

from conftest import session
from lombo_visa import cli, core, notify, state
from lombo_visa.cli import main
from lombo_visa.config import PREFIX, SETTINGS, ConfigError, from_env
from lombo_visa.jellyfin import Jellyfin, JellyfinError

ROOT = Path(__file__).resolve().parent.parent
LIVE = {"LOMBO_VISA_DRY_RUN": "false"}
BLOCK = {"LOMBO_VISA_DRY_RUN": "false", "LOMBO_VISA_BLOCK_SESSIONS": "true"}
HOME_IP = "12.0.1.5"  # US in the test database
ABROAD_IP = "11.0.1.9"  # DE in the test database


def baseline(env, fake, *existing):
    fake.sessions = list(existing)
    assert main(["run"], env) == 0


# ---------------------------------------------------------------- settings
def test_config_defaults_are_safe(env):
    cfg = from_env(env)
    assert cfg.dry_run is True
    assert cfg.block_sessions is False
    assert cfg.interval_seconds == 60
    assert cfg.home_countries == {"US"}
    assert cfg.max_blocks_per_round == 3
    assert cfg.max_window_days == 30
    assert cfg.geoip_url is None


def test_config_reads_every_value(env):
    cfg = from_env(
        {
            **env,
            "LOMBO_VISA_HOME_COUNTRIES": "us, ca",
            "LOMBO_VISA_LAN_SUBNETS": "100.64.0.0/10, fd00::/8",
            "LOMBO_VISA_EXEMPT_USERS": "Boss, Pat",
            "LOMBO_VISA_GEOIP_URL": "https://example.com/c.mmdb.gz",
            "LOMBO_VISA_DRY_RUN": "no",
            "LOMBO_VISA_BLOCK_SESSIONS": "yes",
        }
    )
    assert cfg.home_countries == {"US", "CA"}
    assert len(cfg.lan_subnets) == 2
    assert cfg.exempt_users == {"boss", "pat"}
    assert cfg.geoip_url == "https://example.com/c.mmdb.gz"
    assert cfg.dry_run is False
    assert cfg.block_sessions is True


@pytest.mark.parametrize(
    ("name", "value"),
    [
        ("JELLYFIN_URL", "jellyfin:8096"),
        ("JELLYFIN_API_KEY", "changeme"),
        ("JELLYFIN_API_KEY", ""),
        ("WEBHOOK_URL", "ftp://x"),
        ("HOME_COUNTRIES", ""),
        ("HOME_COUNTRIES", "USA"),
        ("HOME_COUNTRIES", "U1"),
        ("LAN_SUBNETS", "not-a-network"),
        ("GEOIP_URL", "http://example.com/c.gz"),
        ("INTERVAL_SECONDS", "5"),
        ("INTERVAL_SECONDS", "soon"),
        ("MAX_BLOCKS_PER_ROUND", "0"),
        ("MAX_WINDOW_DAYS", "0"),
    ],
)
def test_config_rejects_bad_values_and_names_the_setting(env, name, value):
    with pytest.raises(ConfigError, match=PREFIX + name):
        from_env({**env, PREFIX + name: value})


def test_env_example_matches_config():
    text = (ROOT / ".env.example").read_text()
    listed = set(re.findall(rf"^#?\s*{PREFIX}([A-Z_]+)=", text, re.M))
    assert listed == set(SETTINGS)
    source = (ROOT / "src/lombo_visa/config.py").read_text()
    assert set(re.findall(r'get\("([A-Z_]+)"', source)) == set(SETTINGS)


def test_env_example_is_safe_to_copy():
    text = (ROOT / ".env.example").read_text()
    assert f"{PREFIX}DRY_RUN=true" in text
    assert f"{PREFIX}BLOCK_SESSIONS=false" in text


# ---------------------------------------------------------------- clients
def test_client_reads_sessions_and_sends_the_key(fake):
    fake.sessions = [session(), {"UserName": "nobody"}, "junk", session(user="x", ip="")]
    found = Jellyfin(fake.url, "good-key").sessions()
    assert [s.user_name for s in found] == ["pat", "x"]
    assert found[0].ip == "11.0.1.9"
    assert fake.requests[-1]["auth"] == 'MediaBrowser Token="good-key"'
    with pytest.raises(JellyfinError, match="HTTP 401"):
        Jellyfin(fake.url, "wrong").info()
    with pytest.raises(JellyfinError):
        Jellyfin("http://127.0.0.1:1", "k", timeout=1).public_info()


def test_client_rejects_odd_answers(fake):
    fake.sessions = {"not": "a list"}  # type: ignore[assignment]
    jf = Jellyfin(fake.url, "good-key")
    with pytest.raises(JellyfinError, match="not a list"):
        jf.sessions()
    fake.users = {"not": "a list"}  # type: ignore[assignment]
    with pytest.raises(JellyfinError, match="not a list"):
        jf.users()
    fake.sessions_status = 500
    with pytest.raises(JellyfinError, match="HTTP 500"):
        jf.sessions()


def test_client_answer_that_is_not_json(fake):
    fake.server.RequestHandlerClass.do_GET = lambda self: (
        self.send_response(200),
        self.send_header("Content-Length", "4"),
        self.end_headers(),
        self.wfile.write(b"nope"),
    )
    with pytest.raises(JellyfinError, match="not JSON"):
        Jellyfin(fake.url, "good-key").public_info()


def test_sign_out_device(fake):
    jf = Jellyfin(fake.url, "good-key")
    jf.sign_out_device("dev one&x=1")
    assert fake.deleted() == ["dev one&x=1"]  # sent as one encoded value
    fake.delete_status = 404
    jf.sign_out_device("gone")  # already gone is fine
    fake.delete_status = 500
    with pytest.raises(JellyfinError, match="HTTP 500"):
        jf.sign_out_device("x")


def test_notify_posts_json_with_mentions_off(fake):
    notify.send(fake.url + "/hook", "hello")
    assert fake.posts()[0]["body"] == {"content": "hello", "text": "hello", "allowed_mentions": {"parse": []}}
    with pytest.raises(notify.NotifyError):
        notify.send("http://127.0.0.1:1/hook", "x", timeout=1)


# ---------------------------------------------------------------- state
def test_state_survives_damage(tmp_path):
    p = tmp_path / "s.json"
    assert state.load(str(p)) == state.empty()
    p.write_text("{not json")
    assert state.load(str(p)) == state.empty()
    p.write_text("[1, 2]")
    assert state.load(str(p)) == state.empty()
    p.write_text(json.dumps({"baselined": "yes", "devices": [], "noted": {"a": 1}, "windows": "x"}))
    loaded = state.load(str(p))
    assert loaded["baselined"] is False
    assert loaded["devices"] == {}
    assert loaded["noted"] == {"a": 1}


def test_state_save_error_names_the_setting(tmp_path):
    blocker = tmp_path / "f"
    blocker.write_text("x")
    with pytest.raises(state.StateError, match="LOMBO_VISA_STATE_FILE"):
        state.save(str(blocker / "s.json"), state.empty())


def test_unreadable_windows_grant_nothing():
    st = state.empty()
    st["windows"] = {
        "1": {"user_id": "u", "user_name": "p", "starts_at": 1, "ends_at": 2},
        "2": {"user_id": "u"},
        "x": {"user_id": "u", "user_name": "p", "starts_at": 1, "ends_at": 2},
        "3": "junk",
    }
    assert [w.id for w in state.windows(st)] == [1]


def test_prune_forgets_old_things():
    st = state.empty()
    now = 10_000_000.0
    st["noted"] = {"new": now - 10, "old": now - 8 * 86400, "bad": "x"}
    st["windows"] = {
        "1": {"ends_at": now - 40 * 86400},
        "2": {"ends_at": now - 40 * 86400},
        "3": {"ends_at": now - 1},
        "4": "junk",
    }
    st["devices"] = {"u:d": {"window": 2}, "u:e": "junk"}
    state.prune(st, now)
    assert list(st["noted"]) == ["new"]
    assert sorted(st["windows"]) == ["2", "3", "4"]  # 2 still guards a device; 1 is gone


# ---------------------------------------------------------------- doctor, plan
def test_bad_settings_exit_2(capsys):
    assert main(["plan"], {}) == 2
    assert "LOMBO_VISA_JELLYFIN_URL" in capsys.readouterr().err


def test_doctor_passes(env, fake, capsys):
    fake.sessions = [session(ip=HOME_IP)]
    assert main(["doctor"], env) == 0
    out = capsys.readouterr().out
    assert "[FAIL]" not in out
    assert "1 signed-in session(s), 1 from a public address" in out
    assert "dry run" in out


def test_doctor_failures_and_warnings(env, fake, capsys, tmp_path):
    bad = {
        **env,
        "LOMBO_VISA_JELLYFIN_API_KEY": "wrong",
        "LOMBO_VISA_GEOIP_DB": str(tmp_path / "missing.mmdb"),
        "LOMBO_VISA_BLOCK_SESSIONS": "true",
        "LOMBO_VISA_STATE_FILE": str(tmp_path / "f" / "s.json"),
    }
    (tmp_path / "f").write_text("a file, not a folder")
    assert main(["doctor"], bad) == 1
    out = capsys.readouterr().out
    assert "API key accepted: /System/Info: HTTP 401" in out
    assert "[WARN] country database" in out
    assert "[FAIL] state file" in out
    assert "[WARN] BLOCK_SESSIONS is true but DRY_RUN" in out


def test_doctor_warns_when_everything_looks_local_or_the_database_is_old(env, fake, capsys, db_file):
    import os

    fake.sessions = [session(ip="10.1.1.20")]
    old = db_file.stat().st_mtime - 100 * 86400
    os.utime(db_file, (old, old))
    assert main(["doctor"], env) == 0
    out = capsys.readouterr().out
    assert "all are on your own network" in out
    assert "days old; run: lombo-visa geoip update" in out


def test_doctor_unreachable(env, capsys):
    assert main(["doctor"], {**env, "LOMBO_VISA_JELLYFIN_URL": "http://127.0.0.1:1"}) == 1
    assert "[FAIL] Jellyfin reachable" in capsys.readouterr().out


def test_plan_before_the_first_run_explains_the_baseline(env, fake, capsys):
    fake.sessions = [session()]
    assert main(["plan"], env) == 0
    assert "No baseline yet" in capsys.readouterr().out
    assert not Path(env["LOMBO_VISA_STATE_FILE"]).exists()  # plan never writes


def test_plan_shows_decisions_and_changes_nothing(env, fake, capsys):
    baseline(env, fake, session(device="old"))
    before = Path(env["LOMBO_VISA_STATE_FILE"]).read_text()
    fake.sessions = [session(device="old"), session(device="new", ip=ABROAD_IP), session(device="home", ip=HOME_IP)]
    assert main(["plan"], {**env, **BLOCK}) == 0
    out = capsys.readouterr().out
    assert "from DE: deny -> would end it" in out
    assert "from US: allow-home" in out
    assert "from ?" not in out
    assert (fake.deleted(), fake.posts()) == ([], [])
    assert Path(env["LOMBO_VISA_STATE_FILE"]).read_text() == before


def test_plan_with_nothing_new_and_notes(env, fake, capsys, tmp_path):
    baseline(env, fake, session())
    assert main(["plan"], env) == 0
    assert "No new sign-ins" in capsys.readouterr().out
    fake.sessions = [session(user=f"u{n}", uid=f"id{n}", device=f"n{n}") for n in range(5)]
    missing = {**env, "LOMBO_VISA_GEOIP_DB": str(tmp_path / "none.mmdb")}
    assert main(["plan"], missing) == 0
    assert "no country database" in capsys.readouterr().out
    assert main(["plan"], env) == 0
    assert "nothing would be ended" in capsys.readouterr().out


# ---------------------------------------------------------------- run
def test_first_run_records_existing_sessions_and_judges_nothing(env, fake, capsys):
    fake.sessions = [session(device="a", ip=ABROAD_IP)]
    assert main(["run"], {**env, **BLOCK}) == 0
    assert "First run: recorded 1" in capsys.readouterr().out
    assert (fake.deleted(), fake.posts()) == ([], [])
    assert "aaaabbbb:a" in json.loads(Path(env["LOMBO_VISA_STATE_FILE"]).read_text())["devices"]


def test_dry_run_prints_and_changes_nothing(env, fake, capsys):
    baseline(env, fake)
    fake.sessions = [session(device="n", ip=ABROAD_IP)]
    assert main(["run"], {**env, "LOMBO_VISA_BLOCK_SESSIONS": "true"}) == 0
    out = capsys.readouterr().out
    assert "deny -> would end it" in out
    assert "alert(s) not sent: dry run" in out
    assert (fake.deleted(), fake.posts()) == ([], [])


def test_going_live_reports_what_dry_run_already_showed(env, fake):
    baseline(env, fake)
    fake.sessions = [session(device="n", ip=ABROAD_IP)]
    assert main(["run"], env) == 0
    assert fake.posts() == []
    assert main(["run"], {**env, **LIVE}) == 0
    assert len(fake.posts()) == 1


def test_alerts_only_mode_does_not_end_sessions(env, fake):
    baseline(env, fake)
    fake.sessions = [session(device="n", ip=ABROAD_IP)]
    assert main(["run"], {**env, **LIVE}) == 0
    assert fake.deleted() == []
    text = fake.posts()[0]["body"]["content"]
    assert "from DE (no travel window)" in text
    assert main(["run"], {**env, **LIVE}) == 0  # same session again: not reported twice
    assert len(fake.posts()) == 1


def test_live_blocking_ends_the_new_session_and_says_so_once(env, fake):
    baseline(env, fake, session(device="old", ip=HOME_IP))
    fake.sessions = [session(device="old", ip=HOME_IP), session(device="n", ip=ABROAD_IP)]
    assert main(["run"], {**env, **BLOCK}) == 0
    assert fake.deleted() == ["n"]  # the existing session is untouched
    assert "ended" in fake.posts()[0]["body"]["content"]
    fake.requests.clear()
    assert (
        main(["run"], {**env, **BLOCK}) == 0
    )  # the session is still listed (not yet gone): ended again, not re-reported
    assert fake.posts() == []


def test_home_and_lan_sign_ins_are_remembered_and_allowed(env, fake):
    baseline(env, fake)
    fake.sessions = [session(device="h", ip=HOME_IP), session(device="l", ip="10.1.1.9")]
    assert main(["run"], {**env, **BLOCK}) == 0
    assert (fake.deleted(), fake.posts()) == ([], [])
    devices = json.loads(Path(env["LOMBO_VISA_STATE_FILE"]).read_text())["devices"]
    assert {"aaaabbbb:h", "aaaabbbb:l"} <= set(devices)
    # once remembered, the same device from abroad later is a continuing session, not a new sign-in
    fake.sessions = [session(device="h", ip=ABROAD_IP)]
    assert main(["run"], {**env, **BLOCK}) == 0
    assert fake.deleted() == []


def test_unknown_address_fails_open(env, fake, tmp_path, capsys):
    baseline(env, fake)
    fake.sessions = [session(device="n", ip=ABROAD_IP)]
    missing = {**env, **BLOCK, "LOMBO_VISA_GEOIP_DB": str(tmp_path / "none.mmdb")}
    assert main(["run"], missing) == 0
    assert fake.deleted() == []
    assert "Could not tell" in fake.posts()[0]["body"]["content"]
    assert "no country database" in capsys.readouterr().err


def test_garbage_database_fails_open(env, fake, db_file):
    baseline(env, fake)
    db_file.write_bytes(b"this is not a database")
    fake.sessions = [session(device="n", ip=ABROAD_IP)]
    assert main(["run"], {**env, **BLOCK}) == 0
    assert fake.deleted() == []


def test_jellyfin_failure_skips_the_round(env, fake, capsys):
    baseline(env, fake)
    fake.sessions = [session(device="n", ip=ABROAD_IP)]
    fake.sessions_status = 500
    assert main(["run"], {**env, **BLOCK}) == 1
    assert "[FAIL]" in capsys.readouterr().err
    assert (fake.deleted(), fake.posts()) == ([], [])


def test_failed_sign_out_is_reported_and_retried(env, fake, capsys):
    baseline(env, fake)
    fake.sessions = [session(device="n", ip=ABROAD_IP)]
    fake.delete_status = 500
    assert main(["run"], {**env, **BLOCK}) == 0
    assert "could not end the session" in capsys.readouterr().err
    assert "no travel window" in fake.posts()[0]["body"]["content"]  # it did not claim success
    fake.delete_status = 204
    assert main(["run"], {**env, **BLOCK}) == 0
    assert fake.deleted() == ["n", "n"]
    assert len(fake.posts()) == 1


def test_webhook_failure_does_not_break_the_round(env, fake, capsys):
    baseline(env, fake)
    fake.sessions = [session(device="n", ip=ABROAD_IP)]
    fake.hook_status = 500
    assert main(["run"], {**env, **BLOCK}) == 0
    assert "webhook failed" in capsys.readouterr().err
    assert fake.deleted() == ["n"]


def test_no_webhook_means_a_printed_note(env, fake, capsys):
    baseline(env, fake)
    fake.sessions = [session(device="n", ip=ABROAD_IP)]
    assert main(["run"], {**env, **LIVE, "LOMBO_VISA_WEBHOOK_URL": ""}) == 0
    assert "no webhook set" in capsys.readouterr().out


def test_a_wave_of_denials_ends_nothing(env, fake):
    baseline(env, fake)
    fake.sessions = [session(user=f"u{n}", uid=f"id{n}", device=f"n{n}", ip=ABROAD_IP) for n in range(5)]
    assert main(["run"], {**env, **BLOCK}) == 0
    assert fake.deleted() == []
    text = fake.posts()[0]["body"]["content"]
    assert "Nothing was ended" in text
    fake.requests.clear()
    assert main(["run"], {**env, **BLOCK}) == 0  # the same wave again: no new message
    assert fake.posts() == []


def test_a_long_alert_is_cut_to_ten_lines(env, fake):
    baseline(env, fake)
    fake.sessions = [session(device=f"n{n}", ip=ABROAD_IP) for n in range(12)]
    assert main(["run"], {**env, **BLOCK, "LOMBO_VISA_MAX_BLOCKS_PER_ROUND": "20"}) == 0
    assert len(fake.deleted()) == 12
    text = fake.posts()[0]["body"]["content"]
    assert text.count("\n") == cli.MAX_ALERT_LINES
    assert text.endswith("...and 2 more")


def test_exempt_user_is_never_ended(env, fake):
    baseline(env, fake)
    fake.sessions = [session(user="Pat", device="n", ip=ABROAD_IP)]
    assert main(["run"], {**env, **BLOCK, "LOMBO_VISA_EXEMPT_USERS": "pat"}) == 0
    assert fake.deleted() == []


def test_hostile_names_cannot_forge_log_or_alert_lines(env, fake, capsys):
    baseline(env, fake)
    fake.sessions = [session(user="pat\n[ ok ] all fine @everyone", device="n", name="x\r\ny", ip=ABROAD_IP)]
    assert main(["run"], {**env, **LIVE}) == 0
    out = capsys.readouterr().out
    assert "\n[ ok ] all fine" not in out
    body = fake.posts()[0]["body"]
    assert "\n[ ok ]" not in body["content"]
    assert body["allowed_mentions"] == {"parse": []}


def test_loop_survives_a_bad_round(env, fake, monkeypatch, capsys):
    baseline(env, fake)
    calls = []

    def fake_sleep(seconds):
        calls.append(seconds)
        fake.sessions_status = 200 if len(calls) == 1 else 500
        if len(calls) == 3:
            raise KeyboardInterrupt

    monkeypatch.setattr(cli.time, "sleep", fake_sleep)
    fake.sessions_status = 500
    with pytest.raises(KeyboardInterrupt):
        main(["run", "--loop"], env)
    assert calls == [60, 60, 60]
    assert "[FAIL]" in capsys.readouterr().err


def test_a_state_file_that_cannot_be_written_stops_the_round(env, fake, tmp_path, capsys):
    (tmp_path / "f").write_text("x")
    assert main(["run"], {**env, "LOMBO_VISA_STATE_FILE": str(tmp_path / "f" / "s.json")}) == 1
    assert "LOMBO_VISA_STATE_FILE" in capsys.readouterr().err


# ---------------------------------------------------------------- travel windows
def test_travel_window_allows_then_signs_out_when_it_ends(env, fake, capsys):
    baseline(env, fake)
    assert main(["travel", "add", "pat", "14", "--country", "de"], env) == 0
    assert "may sign in from DE until" in capsys.readouterr().out
    assert main(["travel", "list"], env) == 0
    assert "#1 Pat: DE, until" in capsys.readouterr().out
    fake.sessions = [session(device="trip", ip=ABROAD_IP, uid="AAAA-BBBB")]
    assert main(["run"], {**env, **BLOCK}) == 0
    assert fake.deleted() == []
    dev = json.loads(Path(env["LOMBO_VISA_STATE_FILE"]).read_text())["devices"]["aaaabbbb:trip"]
    assert dev["window"] == 1
    # another country is still refused
    fake.sessions.append(session(device="fr", ip="2a00:1450::5"))
    assert main(["run"], {**env, **BLOCK}) == 0
    assert fake.deleted() == ["fr"]
    fake.sessions = [s for s in fake.sessions if s["DeviceId"] != "fr"]  # Jellyfin drops it once it is signed out
    # dry run: the end of the window is only announced
    fake.requests.clear()
    assert main(["travel", "revoke", "1"], env) == 0
    assert main(["run"], env) == 0
    assert "travel window over: would sign out" in capsys.readouterr().out
    assert main(["plan"], env) == 0
    assert "travel window over: would sign out" in capsys.readouterr().out
    assert fake.deleted() == []
    # live: signed out, once
    assert main(["run"], {**env, **BLOCK}) == 0
    assert fake.deleted() == ["trip"]
    assert "signed out 1 device(s)" in fake.posts()[-1]["body"]["content"]
    assert "aaaabbbb:trip" not in json.loads(Path(env["LOMBO_VISA_STATE_FILE"]).read_text())["devices"]
    fake.sessions = []  # Jellyfin no longer lists it
    assert main(["run"], {**env, **BLOCK}) == 0
    assert fake.deleted() == ["trip"]


def test_ended_trips_can_be_signed_out_without_ending_anything_else(env, fake):
    only_trips = {**env, "LOMBO_VISA_DRY_RUN": "false", "LOMBO_VISA_SIGN_OUT_ENDED_TRIPS": "true"}
    baseline(env, fake)
    assert main(["travel", "add", "pat", "14", "--country", "de"], env) == 0
    fake.sessions = [session(device="trip", ip=ABROAD_IP, uid="AAAA-BBBB"), session(device="fr", ip="2a00:1450::5")]
    assert main(["run"], only_trips) == 0
    assert fake.deleted() == []  # the sign-in with no window is left alone
    assert main(["travel", "revoke", "1"], env) == 0
    assert main(["run"], only_trips) == 0
    assert fake.deleted() == ["trip"]


def test_failed_window_sign_out_is_retried(env, fake, capsys):
    baseline(env, fake)
    assert main(["travel", "add", "pat", "1"], env) == 0
    fake.sessions = [session(device="trip", ip=ABROAD_IP)]
    assert main(["run"], {**env, **BLOCK}) == 0
    assert main(["travel", "revoke", "1"], env) == 0
    fake.delete_status = 500
    assert main(["run"], {**env, **BLOCK}) == 0
    assert "could not sign out a device" in capsys.readouterr().err
    fake.delete_status = 204
    assert main(["run"], {**env, **BLOCK}) == 0
    assert fake.deleted() == ["trip", "trip"]


@pytest.mark.parametrize(
    ("args", "message"),
    [
        (["travel", "add", "nobody", "5"], "no account named"),
        (["travel", "add", "boss", "5"], "administrator"),
        (["travel", "add", "pat", "99"], "at most 30"),
        (["travel", "add", "pat", "5", "--country", "Germany"], "two letters"),
        (["travel", "revoke", "42"], "no active window #42"),
    ],
)
def test_travel_refusals(env, fake, capsys, args, message):
    assert main(args, env) == 1
    assert message in capsys.readouterr().err


def test_travel_list_when_empty_and_double_revoke(env, fake, capsys):
    assert main(["travel", "list"], env) == 0
    assert "No active or upcoming" in capsys.readouterr().out
    assert main(["travel", "add", "pat", "3"], env) == 0
    assert main(["travel", "revoke", "1"], env) == 0
    assert main(["travel", "revoke", "1"], env) == 1


def test_travel_add_with_jellyfin_down(env, capsys):
    assert main(["travel", "add", "pat", "3"], {**env, "LOMBO_VISA_JELLYFIN_URL": "http://127.0.0.1:1"}) == 1
    assert "[FAIL]" in capsys.readouterr().err


# ---------------------------------------------------------------- geoip update command
def test_geoip_update_command(env, monkeypatch, capsys):
    monkeypatch.setattr(cli.geoip, "update", lambda path, url, now, force: f"updated {path} (force={force})")
    assert main(["geoip", "update", "--force"], env) == 0
    assert "force=True" in capsys.readouterr().out

    def refuse(path, url, now, force):
        raise cli.geoip.GeoIpError("download failed: nope")

    monkeypatch.setattr(cli.geoip, "update", refuse)
    assert main(["geoip", "update"], env) == 1
    assert "The old file, if any, was kept." in capsys.readouterr().err


def test_core_clock_is_passed_in():
    import inspect

    source = inspect.getsource(core)
    assert "import time" not in source
    assert "datetime" not in source
    assert "urllib" not in source
