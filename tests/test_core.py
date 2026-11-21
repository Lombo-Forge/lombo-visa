import ipaddress

import pytest

from lombo_visa import core
from lombo_visa.core import Session, Window

NOW = 1_750_000_000.0
DAY = 86400
HOME = frozenset({"US"})
COUNTRIES = {"11.0.1.9": "DE", "12.0.1.5": "US", "13.0.1.7": "FR"}


def lookup(ip):
    return COUNTRIES.get(ip)


def sess(user="pat", device="d1", ip="11.0.1.9", uid="AAAA-bbbb"):
    return Session(uid, user, device, f"Phone {device}", "Web", ip)


def win(id=1, uid="aaaabbbb", country=None, start=NOW - DAY, end=NOW + DAY, revoked=None):
    return Window(id, uid, "pat", country, start, end, revoked)


def plan(sessions, known=None, noted=None, windows=(), exempt=frozenset(), max_blocks=3, lan=()):
    return core.plan_round(
        sessions, known or {}, noted or {}, list(windows), HOME, lan, exempt, lookup, NOW, max_blocks
    )


# ---------------------------------------------------------------- helpers
def test_clean_id_and_key():
    assert core.clean_id("AAAA-bbbb") == "aaaabbbb"
    assert core.clean_id(None) == ""
    assert core.device_key("AAAA-bbbb", "d1") == "aaaabbbb:d1"


def test_clean_text_neutralises_log_injection():
    assert core.clean_text("pat\n[ ok ] fake line\r\x1b[31m") == "pat?[ ok ] fake line??[31m"
    assert core.clean_text("a b") == "a?b"  # noqa: RUF001 (the separator is typed on purpose)
    assert core.clean_text("x" * 50) == "x" * 40 + "..."
    assert core.clean_text(None) == ""
    assert core.clean_text("  hi  ") == "hi"


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("11.0.1.9", "11.0.1.9"),
        ("11.0.1.9:8096", "11.0.1.9"),
        ("[2a00:1450::1]:443", "2a00:1450::1"),
        ("2a00:1450::1", "2a00:1450::1"),
        ("::ffff:11.0.1.9", "11.0.1.9"),
        ("fe80::1%eth0", "fe80::1"),
        (" 11.0.1.9 ", "11.0.1.9"),
        ("", None),
        ("junk", None),
        ("999.1.1.1", None),
        ("11.0.1.9, 10.0.0.1", None),  # a forwarded-for list is not one address
    ],
)
def test_normalise_ip(raw, expected):
    assert core.normalise_ip(raw) == expected


def test_country_of():
    extra = (ipaddress.ip_network("100.64.0.0/10"),)
    assert core.country_of("11.0.1.9", lookup) == "DE"
    assert core.country_of("10.1.1.5", lookup) == core.LAN
    assert core.country_of("127.0.0.1", lookup) == core.LAN
    assert core.country_of("fd00::1", lookup) == core.LAN
    assert core.country_of("100.64.1.1", lookup, extra) == core.LAN
    assert core.country_of("100.64.1.1", lookup) == "unknown"  # not private to the stdlib, not in the file
    assert core.country_of("2a00:1450::1", lookup, extra) == "unknown"  # an IPv6 address ignores IPv4 subnets
    assert core.country_of("8.8.8.8", lookup) == "unknown"
    assert core.country_of("junk", lookup) == "unknown"
    assert core.country_of("11.0.1.9", lambda ip: "de") == "DE"
    assert core.country_of("11.0.1.9", lambda ip: "DEU") == "unknown"
    assert core.country_of("11.0.1.9", lambda ip: 5) == "unknown"  # type: ignore[arg-type,return-value]


def test_a_lookup_that_raises_means_unknown():
    def boom(ip):
        raise RuntimeError("database exploded")

    assert core.country_of("11.0.1.9", boom) == "unknown"


# ---------------------------------------------------------------- decide
def test_decide():
    assert core.decide(core.LAN, HOME, [], "u", NOW) == ("allow-home", None)
    assert core.decide("US", HOME, [], "u", NOW) == ("allow-home", None)
    assert core.decide("unknown", HOME, [], "u", NOW) == ("unknown", None)
    assert core.decide("DE", HOME, [], "aaaabbbb", NOW) == ("deny", None)
    assert core.decide("DE", HOME, [win()], "AAAA-bbbb", NOW) == ("allow-window", 1)
    assert core.decide("DE", HOME, [win(country="DE")], "aaaabbbb", NOW) == ("allow-window", 1)
    assert core.decide("DE", HOME, [win(country="FR")], "aaaabbbb", NOW)[0] == "deny"
    assert core.decide("DE", HOME, [win(country="FR"), win(id=2, country="DE")], "aaaabbbb", NOW) == ("allow-window", 2)


def test_windows_follow_the_account_and_the_clock():
    assert core.decide("DE", HOME, [win(uid="other")], "aaaabbbb", NOW)[0] == "deny"
    assert core.decide("DE", HOME, [win(start=NOW + 1)], "aaaabbbb", NOW)[0] == "deny"  # not started
    assert core.decide("DE", HOME, [win(end=NOW)], "aaaabbbb", NOW)[0] == "deny"  # ended exactly now
    assert core.decide("DE", HOME, [win(revoked=NOW - 1)], "aaaabbbb", NOW)[0] == "deny"
    assert core.decide("DE", HOME, [win(start=NOW)], "aaaabbbb", NOW)[0] == "allow-window"


# ---------------------------------------------------------------- plan_round
def verdicts(p):
    return [(i.session.device_id, i.verdict) for i in p.items]


def test_new_sessions_are_judged_known_ones_are_not():
    known = {"aaaabbbb:d1": {}}
    p = plan(
        [sess(device="d1"), sess(device="d2"), sess(device="d3", ip="12.0.1.5"), sess(device="d4", ip="8.8.8.8")], known
    )
    assert verdicts(p) == [("d1", "known"), ("d2", "deny"), ("d3", "allow-home"), ("d4", "unknown")]
    assert [i.revoke for i in p.items] == [False, True, False, False]
    assert [i.remember for i in p.items] == [False, False, True, True]  # unknown is remembered too
    assert [i.first_note for i in p.items] == [False, True, False, True]


def test_a_window_allows_and_names_itself():
    p = plan([sess()], windows=[win(id=7)])
    assert p.items[0].verdict == "allow-window"
    assert p.items[0].window_id == 7
    assert p.items[0].remember
    assert not p.items[0].revoke


def test_noted_sessions_are_not_reported_again():
    p = plan([sess()], noted={"aaaabbbb:d1": NOW})
    assert p.items[0].verdict == "deny"
    assert p.items[0].first_note is False
    assert p.items[0].revoke  # still to be ended if an earlier attempt failed


def test_exempt_users_are_never_judged():
    p = plan([sess(user="Pat")], exempt=frozenset({"pat"}))
    assert p.items[0].verdict == "exempt"
    assert p.items[0].remember
    p = plan([sess()], exempt=frozenset({"aaaabbbb"}))
    assert p.items[0].verdict == "exempt"


def test_lan_subnets_count_as_home():
    lan = (ipaddress.ip_network("11.0.1.0/24"),)
    assert plan([sess()], lan=lan).items[0].verdict == "allow-home"


def test_no_device_id_cannot_be_ended():
    p = plan([sess(device="")])
    assert p.items[0].verdict == "deny"
    assert not p.items[0].revoke


def test_device_id_clash_is_not_ended():
    # d1 already belongs to another user's allowed device: removing the device would sign that person out.
    known = {"otheruser:d1": {}}
    p = plan([sess(device="d1")], known)
    assert p.items[0].verdict == "deny"
    assert p.items[0].clash
    assert not p.items[0].revoke
    assert "another account" in core.alert_text(p.items[0], ended=False)


def test_denials_for_too_many_accounts_in_one_round_end_nothing():
    many = [sess(user=f"u{n}", uid=f"id{n}", device=f"d{n}") for n in range(4)]
    p = plan(many, max_blocks=3)
    assert p.tripped
    assert not any(i.revoke for i in p.items)
    assert all(i.verdict == "deny" for i in p.items)
    ok = plan(many[:3], max_blocks=3)
    assert not ok.tripped
    assert all(i.revoke for i in ok.items)


def test_many_sessions_of_one_account_cannot_trip_the_stop():
    # Someone with one stolen password must not be able to switch the sign-outs off by opening many sessions.
    p = plan([sess(device=f"d{n}") for n in range(10)], max_blocks=3)
    assert not p.tripped
    assert all(i.revoke for i in p.items)


def test_no_database_fails_open():
    p = core.plan_round([sess()], {}, {}, [], HOME, (), frozenset(), lambda ip: None, NOW, 3)
    assert p.items[0].verdict == "unknown"
    assert not p.items[0].revoke


def test_an_unreadable_address_fails_open():
    assert plan([sess(ip="")]).items[0].verdict == "unknown"
    assert plan([sess(ip="1.2.3.4, 5.6.7.8")]).items[0].verdict == "unknown"


# ---------------------------------------------------------------- windows ending
def test_due_signouts():
    devices = {
        "aaaabbbb:a": {"window": 1},
        "aaaabbbb:b": {"window": 2},
        "aaaabbbb:home": {"window": None},
        "aaaabbbb:old": {},
        "zzzz:c": {"window": 3},
    }
    windows = [
        win(id=1, end=NOW - 1),  # ended
        win(id=2, end=NOW + DAY),  # still open
        win(id=3, uid="zzzz", revoked=NOW - 5),  # revoked
    ]
    # aaaabbbb:a is kept because that person has another open window (id 2); only the revoked one is due
    assert core.due_signouts(devices, windows, NOW) == ["zzzz:c"]


def test_a_person_with_another_open_window_keeps_their_devices():
    devices = {"aaaabbbb:a": {"window": 1}}
    assert core.due_signouts(devices, [win(id=1, end=NOW - 1), win(id=2)], NOW) == []
    assert core.due_signouts(devices, [win(id=1, end=NOW - 1)], NOW) == ["aaaabbbb:a"]
    assert core.due_signouts(devices, [], NOW) == ["aaaabbbb:a"]  # the window record is gone
    assert core.due_signouts(devices, [win(id=1)], NOW) == []


# ---------------------------------------------------------------- new_window
def test_new_window():
    w = core.new_window([], "AAAA-bbbb", "Pat", 14, " de ", NOW, 30)
    assert (w.id, w.user_id, w.country, w.starts_at, w.ends_at) == (1, "aaaabbbb", "DE", NOW, NOW + 14 * DAY)
    assert core.new_window([win(id=4)], "u", "Pat", 1, None, NOW, 30).id == 5
    assert core.new_window([], "u", "Pat", 1, "", NOW, 30).country is None


@pytest.mark.parametrize(("days", "country"), [(0, None), (-1, None), (31, None), (5, "DEU"), (5, "D1")])
def test_new_window_refuses(days, country):
    with pytest.raises(ValueError, match=r"days|country"):
        core.new_window([], "u", "Pat", days, country, NOW, 30)


# ---------------------------------------------------------------- words
def test_describe_and_alerts_are_clean():
    s = Session("u", "pat\n@everyone", "d1", "Phone\r\n", "Web", "11.0.1.9")
    i = plan([s]).items[0]
    assert "\n" not in core.describe(i)
    assert "\n" not in core.alert_text(i, ended=True)
    assert "ended" in core.alert_text(i, ended=True)
    assert "no travel window" in core.alert_text(i, ended=False)
    u = plan([sess(ip="8.8.8.8")]).items[0]
    assert "Could not tell" in core.alert_text(u, ended=False)
    nodev = plan([sess(device="")]).items[0]
    assert "no device id" in core.alert_text(nodev, ended=False)
