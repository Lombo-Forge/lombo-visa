"""The first run records signed-in devices as well as sessions, and older installs catch up once."""

import json
from pathlib import Path

import pytest

from conftest import session
from lombo_visa import cli, state
from lombo_visa.config import from_env

DAY = 86400
FIRST = 1_795_000_000.0  # when the install first ran


def device(device_id, last="2026-01-01T00:00:00.1234567Z", user="Pat", uid="aaaa-bbbb"):
    return {"Id": device_id, "LastUserId": uid, "LastUserName": user, "DateLastActivity": last}


def keys(env):
    return {k.partition(":")[2] for k in state.load(env["LOMBO_VISA_STATE_FILE"])["devices"]}


def live(env):
    return from_env({**env, "LOMBO_VISA_DRY_RUN": "false", "LOMBO_VISA_BLOCK_SESSIONS": "true"})


def test_a_device_switched_off_at_the_first_run_is_never_signed_out(env, fake):
    fake.sessions = [session(device="phone", ip="12.0.1.9")]
    fake.devices = [device("phone"), device("tv")]
    assert cli.run_once(live(env), FIRST) == 0
    assert keys(env) == {"phone", "tv"}
    fake.sessions = [session(device="tv", ip="11.0.1.9")]  # later, from abroad
    assert cli.run_once(live(env), FIRST + DAY) == 0
    assert fake.deleted() == []


def test_a_first_run_that_cannot_read_the_devices_records_nothing(env, fake):
    fake.devices_status = 500
    with pytest.raises(cli.JellyfinError):
        cli.run_once(live(env), FIRST)
    assert not Path(env["LOMBO_VISA_STATE_FILE"]).exists()


def old_state(env, **devices):
    Path(env["LOMBO_VISA_STATE_FILE"]).write_text(
        json.dumps({"baselined": True, "devices": devices, "noted": {}, "windows": {}})
    )


def test_an_older_install_catches_up_once_with_only_devices_idle_since_its_first_run(env, fake, capsys):
    old_state(
        env,
        **{
            "aaaabbbb:phone": {"user": "Pat", "first": FIRST},
            "aaaabbbb:trip": {"user": "Pat", "first": FIRST - DAY, "window": 1},  # noted by the gateway before
        },
    )
    fake.devices = [
        device("tv", "2026-01-01T00:00:00.0000000Z"),
        device("stranger", "2099-01-01T00:00:00.0000000Z"),  # used since the first run: stays judged
        device("garbled", "yesterday"),
    ]
    assert cli.run_once(live(env), FIRST + DAY) == 0
    assert keys(env) == {"phone", "tv"}  # the trip device was signed out: it has no window
    assert fake.deleted() == ["trip"]
    assert "Recorded 1 signed-in device(s)" in capsys.readouterr().out
    fake.devices.append(device("later"))
    assert cli.run_once(live(env), FIRST + 2 * DAY) == 0
    assert "later" not in keys(env)  # once only
    fake.sessions = [session(device="stranger", ip="11.0.1.9")]
    assert cli.run_once(live(env), FIRST + 3 * DAY) == 0
    assert fake.deleted() == ["trip", "stranger"]


def test_the_catch_up_waits_when_the_device_list_cannot_be_read(env, fake, capsys):
    old_state(env, **{"aaaabbbb:phone": {"user": "Pat", "first": FIRST}})
    fake.devices_status = 500
    assert cli.run_once(live(env), FIRST + DAY) == 0  # the round itself still runs
    assert "trying again next round" in capsys.readouterr().err
    fake.devices_status, fake.devices = 200, [device("tv")]
    assert cli.run_once(live(env), FIRST + 2 * DAY) == 0
    assert keys(env) == {"phone", "tv"}


def test_nothing_is_added_when_the_first_run_left_no_entry(env, fake):
    old_state(env)
    fake.devices = [device("tv")]
    assert cli.run_once(live(env), FIRST) == 0
    assert keys(env) == set()
