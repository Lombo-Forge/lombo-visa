"""The admin routes and the password check, for building Visa into your own app. Fake Jellyfin only."""

# ruff: noqa: F811  (the imported fixtures are named again as test arguments)

import pytest

from lombo_visa import api
from lombo_visa.config import ConfigError, from_env
from lombo_visa.jellyfin import Jellyfin, JellyfinError
from test_api import ADMIN, LIVE, T0, TOKEN, UID, Clock, start, windows  # noqa: F401
from test_gateway import up  # noqa: F401

KEY = "adm-" + "ZyXwVuTsRq9876543210" * 2
BOTH = {**LIVE, "LOMBO_VISA_ADMIN_TOKEN": KEY}


# ---------------------------------------------------------------- admin routes
def test_admin_routes_need_the_admin_token_and_it_opens_nothing_else(start):
    a = start(BOTH)
    assert a.call("GET", "/v1/admin/windows")[0] == 401  # the self-service token is not enough
    assert a.call("GET", "/v1/admin/windows", token=None)[0] == 401
    assert a.call("GET", f"/v1/travel/{UID}", token=KEY)[0] == 401
    assert a.call("GET", "/v1/admin/windows", token=KEY)[0] == 200


def test_admin_routes_are_off_without_a_token_even_for_an_empty_bearer(start):
    a = start(LIVE)
    for token in (None, "", TOKEN):
        assert a.call("GET", "/v1/admin/windows", token=token)[0] == 401


def test_with_only_the_admin_token_the_self_routes_stay_shut(start):
    a = start({**BOTH, "LOMBO_VISA_API_TOKEN": ""})
    for headers in ({"Authorization": "Bearer "}, {"Authorization": "Bearer"}, {}):
        assert a.call("GET", f"/v1/travel/{UID}", token=None, headers=headers)[0] == 401


def test_admin_add_list_and_revoke(start, env):
    a = start(BOTH, clock=Clock())
    code, body, _ = a.call("POST", "/v1/admin/windows", {"user_id": UID, "days": 5, "country": "JP"}, token=KEY)
    assert code == 200
    (row,) = a.call("GET", "/v1/admin/windows", token=KEY)[1]["windows"]
    assert (row["id"], row["name"], row["country"], row["self"]) == (body["id"], "Pat", "JP", False)
    assert row["ends"] == int(T0 + 5 * 86400)
    assert a.call("DELETE", f"/v1/admin/windows/{body['id']}", token=KEY)[0] == 200
    assert a.call("GET", "/v1/admin/windows", token=KEY)[1]["windows"] == []
    assert a.call("DELETE", f"/v1/admin/windows/{body['id']}", token=KEY)[0] == 404


def test_admin_add_refuses_administrators_and_bad_input(start, env):
    a = start(BOTH)
    for body in (
        {"user_id": ADMIN, "days": 3},
        {"user_id": UID, "days": 0},
        {"user_id": UID, "days": 999},
        {"user_id": UID, "days": 3, "country": "UK"},
        {"user_id": "0" * 32, "days": 3},
    ):
        assert a.call("POST", "/v1/admin/windows", body, token=KEY)[0] == 422, body
    for body in ({"user_id": UID}, {"user_id": UID, "days": 3, "extra": 1}, {"user_id": UID, "days": True}):
        assert a.call("POST", "/v1/admin/windows", body, token=KEY)[0] == 400, body
    assert a.call("DELETE", "/v1/admin/windows/x", token=KEY)[0] == 404
    assert a.call("PUT", "/v1/admin/windows", token=KEY)[0] == 405


def test_the_two_tokens_must_differ_and_be_long(env):
    for admin in (TOKEN, "short"):
        with pytest.raises(ConfigError, match="ADMIN_TOKEN"):
            from_env({**env, "LOMBO_VISA_API_TOKEN": TOKEN, "LOMBO_VISA_ADMIN_TOKEN": admin})


# ---------------------------------------------------------------- password check
def test_a_self_window_needs_the_password_and_wrong_ones_are_limited(start, env, monkeypatch):
    seen = []
    monkeypatch.setattr(Jellyfin, "check_password", lambda self, n, p: seen.append((n, p)) or p == "right")
    clock = Clock()
    a = start({**LIVE, "LOMBO_VISA_SELF_REQUIRE_PASSWORD": "true"}, clock=clock)
    ask_for = lambda pw: a.call("POST", "/v1/travel", {"user_id": UID, "days": 3, "password": pw})[0]  # noqa: E731
    assert a.post()[0] == 400  # no password at all
    assert [ask_for("nope"), ask_for(""), ask_for("right")] == [422, 422, 429]
    assert seen == [("Pat", "nope")]  # an empty one is never sent; nothing is asked once the limit is hit
    clock.now += api.PASSWORD_WINDOW
    assert ask_for("right") == 200


def test_the_password_is_not_asked_for_by_default(start):
    a = start(LIVE)
    assert a.call("POST", "/v1/travel", {"user_id": UID, "days": 3, "password": "x"})[0] == 400
    assert a.post()[0] == 200


def test_check_password_signs_in_and_straight_out(up):
    up.script = lambda r: (200, [], b'{"AccessToken":"t1"}' if "Authenticate" in r["path"] else b"")
    assert Jellyfin(up.url, "key").check_password("Pat", "secret") is True
    assert [r["path"] for r in up.requests] == ["/Users/AuthenticateByName", "/Sessions/Logout"]
    assert 'Token="t1"' in dict(up.requests[1]["headers"])["Authorization"]
    assert all("key" not in dict(r["headers"])["Authorization"] for r in up.requests)  # never the API key
    up.script = lambda r: (401, [], b"")
    assert Jellyfin(up.url, "key").check_password("Pat", "bad") is False
    up.script = lambda r: (500, [], b"")
    with pytest.raises(JellyfinError, match="HTTP 500"):
        Jellyfin(up.url, "key").check_password("Pat", "bad")
