"""The audit log: fields, no raw address anywhere, rotation, the key file, the `audit` command, session mode."""

import json
import os
import stat
from pathlib import Path

import pytest

from conftest import session
from lombo_visa import audit
from lombo_visa.cli import main

IP = "11.0.1.9"
LIVE = {"LOMBO_VISA_DRY_RUN": "false"}


def lines(path):
    with open(path) as f:
        return [json.loads(x) for x in f.read().splitlines()]


def test_a_line_has_exactly_the_documented_fields(tmp_path):
    path = str(tmp_path / "audit.jsonl")
    audit.record(path, "gateway", "deny", "DE", "pat", IP, 1_700_000_000.9)
    (row,) = lines(path)
    assert set(row) == {"ts", "mode", "decision", "country", "user", "ip_hash"}
    assert row["ts"] == 1_700_000_000
    assert (row["mode"], row["decision"], row["country"], row["user"]) == ("gateway", "deny", "DE", "pat")
    assert len(row["ip_hash"]) == audit.HASH_CHARS
    int(row["ip_hash"], 16)


def test_no_raw_address_is_stored_anywhere(tmp_path):
    path = str(tmp_path / "audit.jsonl")
    audit.record(path, "session", "allow-home", "US", "pat", "12.0.1.5", 1.0)
    audit.record(path, "session", "deny", "DE", "pat", IP, 2.0)
    for name in os.listdir(tmp_path):
        content = (tmp_path / name).read_bytes()
        assert b"12.0.1.5" not in content
        assert IP.encode() not in content


def test_the_hash_is_keyed_and_stable(tmp_path):
    a, b = str(tmp_path / "a" / "audit.jsonl"), str(tmp_path / "b" / "audit.jsonl")
    for p in (a, a, b):
        audit.record(p, "gateway", "deny", "DE", "pat", IP, 1.0)
    first, second = lines(a)
    assert first["ip_hash"] == second["ip_hash"]  # the same address can be recognised again
    assert lines(b)[0]["ip_hash"] != first["ip_hash"]  # but a different key gives a different hash
    audit.record(a, "gateway", "deny", "DE", "pat", "11.0.1.10", 1.0)
    assert lines(a)[-1]["ip_hash"] != first["ip_hash"]


def test_a_missing_address_gives_an_empty_hash(tmp_path):
    path = str(tmp_path / "audit.jsonl")
    audit.record(path, "gateway", "unknown", "unknown", "", None, 1.0)
    assert lines(path)[0]["ip_hash"] == ""


def test_the_key_and_the_log_are_private_to_the_owner(tmp_path):
    path = str(tmp_path / "data" / "audit.jsonl")
    audit.record(path, "gateway", "deny", "DE", "pat", IP, 1.0)
    key = tmp_path / "data" / "audit.key"
    assert stat.S_IMODE(key.stat().st_mode) == 0o600
    assert len(key.read_bytes()) == audit.KEY_BYTES
    assert stat.S_IMODE(os.stat(path).st_mode) == 0o600
    before = key.read_bytes()
    audit.record(path, "gateway", "deny", "DE", "pat", IP, 2.0)
    assert key.read_bytes() == before  # made once, never replaced


def test_a_damaged_key_stops_the_log_but_never_raises(tmp_path, capsys):
    path = str(tmp_path / "audit.jsonl")
    (tmp_path / "audit.key").write_bytes(b"short")
    audit.record(path, "gateway", "deny", "DE", "pat", IP, 1.0)
    assert not os.path.exists(path)
    assert "not a key file" in capsys.readouterr().err
    audit.record(path, "gateway", "deny", "DE", "pat", IP, 1.0)
    assert capsys.readouterr().err == ""  # said once


def test_an_unwritable_place_never_raises(tmp_path, capsys):
    blocker = tmp_path / "file"
    blocker.write_text("x")
    audit.record(str(blocker / "audit.jsonl"), "gateway", "deny", "DE", "pat", IP, 1.0)
    assert "[WARN] audit log" in capsys.readouterr().err


def test_off_writes_nothing(tmp_path):
    audit.record("", "gateway", "deny", "DE", "pat", IP, 1.0)
    assert list(tmp_path.iterdir()) == []


def test_hostile_user_names_stay_on_one_line(tmp_path):
    path = str(tmp_path / "audit.jsonl")
    audit.record(path, "gateway", "deny", "DE", 'pat\n{"fake": 1} x' + "y" * 200, IP, 1.0)  # noqa: RUF001
    (row,) = lines(path)
    assert "\n" not in row["user"]
    assert len(row["user"]) <= 44


def test_rotation_keeps_one_old_file(tmp_path, monkeypatch):
    monkeypatch.setattr(audit, "MAX_BYTES", 400)
    path = str(tmp_path / "audit.jsonl")
    for i in range(40):
        audit.record(path, "gateway", "allow-home", "US", f"user{i}", IP, float(i))
    assert os.path.exists(path + ".1")
    assert not os.path.exists(path + ".2")
    assert os.path.getsize(path) < 400 + 200
    users = [r["user"] for r in audit.read(path, 1000, None)]
    assert users[-1] == "user39"
    assert users == sorted(users, key=lambda u: int(u[4:]))  # old file first, then the current one
    assert stat.S_IMODE(os.stat(path + ".1").st_mode) == 0o600


def test_read_filters_and_skips_damaged_lines(tmp_path):
    path = str(tmp_path / "audit.jsonl")
    for i, who in enumerate(["Pat", "bo", "pat"]):
        audit.record(path, "session", "deny", "DE", who, IP, float(i))
    with open(path, "a") as f:
        f.write("not json\n[1]\n{}\n")
    assert [r["user"] for r in audit.read(path, 10, "PAT")] == ["Pat", "pat"]
    assert [r["ts"] for r in audit.read(path, 2, None)] == [1, 2]
    assert audit.read(path, 0, None) == []
    assert audit.read(str(tmp_path / "missing"), 5, None) == []


def test_audit_command(env, tmp_path, capsys):
    path = str(tmp_path / "audit.jsonl")
    cfg = {**env, "LOMBO_VISA_AUDIT_FILE": path}
    assert main(["audit"], env) == 1
    assert "audit log is off" in capsys.readouterr().err
    assert main(["audit"], cfg) == 0
    assert "No matching entries" in capsys.readouterr().out
    audit.record(path, "gateway", "deny", "DE", "Pat", IP, 1_700_000_000)
    audit.record(path, "session", "allow-home", "US", "Bo", "12.0.1.5", 1_700_000_100)
    assert main(["audit"], cfg) == 0
    out = capsys.readouterr().out
    assert "gateway" in out
    assert "deny" in out
    assert "Pat" in out
    assert "Bo" in out
    assert "ip#" in out
    assert IP not in out
    assert main(["audit", "--last", "1"], cfg) == 0
    out = capsys.readouterr().out
    assert "Bo" in out
    assert "Pat" not in out
    assert main(["audit", "--user", "pat"], cfg) == 0
    out = capsys.readouterr().out
    assert "Pat" in out
    assert "Bo" not in out


# ---------------------------------------------------------------- session mode writes it too
def test_session_mode_records_each_judged_sign_in_once(env, fake, tmp_path):
    path = str(tmp_path / "audit.jsonl")
    cfg = {**env, **LIVE, "LOMBO_VISA_AUDIT_FILE": path}
    fake.sessions = [session()]
    assert main(["run"], cfg) == 0  # the first run only records what exists
    assert not os.path.exists(path)
    fake.sessions = [
        session(),
        session(user="away", device="d2", ip=IP, uid="u2"),
        session(user="home", device="d3", ip="12.0.1.5", uid="u3"),
        session(user="lost", device="d4", ip="not-an-ip", uid="u4"),
    ]
    for _ in range(3):  # later rounds do not repeat the lines
        assert main(["run"], cfg) == 0
    rows = lines(path)
    got = {r["user"]: (r["mode"], r["decision"], r["country"]) for r in rows}
    assert got == {
        "away": ("session", "deny", "DE"),
        "home": ("session", "allow-home", "US"),
        "lost": ("session", "unknown", "unknown"),
    }
    assert len(rows) == 3
    raw = Path(path).read_text()
    assert IP not in raw
    assert "12.0.1.5" not in raw


def test_session_mode_records_in_dry_run_too(env, fake, tmp_path):
    path = str(tmp_path / "audit.jsonl")
    cfg = {**env, "LOMBO_VISA_AUDIT_FILE": path}
    fake.sessions = []
    main(["run"], cfg)
    fake.sessions = [session(user="away", device="d2", ip=IP, uid="u2")]
    main(["run"], cfg)
    assert [(r["decision"], r["user"]) for r in lines(path)] == [("deny", "away")]


def test_a_broken_audit_file_never_changes_a_decision(env, fake, tmp_path):
    blocker = tmp_path / "file"
    blocker.write_text("x")
    cfg = {**env, **LIVE, "LOMBO_VISA_BLOCK_SESSIONS": "true", "LOMBO_VISA_AUDIT_FILE": str(blocker / "a.jsonl")}
    fake.sessions = []
    main(["run"], cfg)
    fake.sessions = [session(user="away", device="d2", ip=IP, uid="u2")]
    assert main(["run"], cfg) == 0
    assert fake.deleted() == ["d2"]


def test_doctor_checks_the_audit_file(env, capsys, tmp_path):
    ok = tmp_path / "ok" / "audit.jsonl"
    assert main(["doctor"], {**env, "LOMBO_VISA_AUDIT_FILE": str(ok)}) == 0
    assert "audit log:" in capsys.readouterr().out
    assert not (tmp_path / "ok" / "audit.key").exists()  # doctor makes no key
    (tmp_path / "bad").mkdir()
    (tmp_path / "bad" / "audit.key").write_bytes(b"x")
    assert main(["doctor"], {**env, "LOMBO_VISA_AUDIT_FILE": str(tmp_path / "bad" / "audit.jsonl")}) == 1
    assert "[FAIL] audit log" in capsys.readouterr().out


@pytest.mark.parametrize("last", ["x", "1.5"])
def test_audit_last_must_be_a_number(env, last):
    with pytest.raises(SystemExit):
        main(["audit", "--last", last], env)
