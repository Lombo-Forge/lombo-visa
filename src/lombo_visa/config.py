"""Settings, read once from environment variables (see .env.example).

Every setting has a LOMBO_VISA_ prefix so it never collides with another container's variables.
A bad value stops the app at start-up with a message that names the variable.
"""

import difflib
import ipaddress
import re
from dataclasses import dataclass
from urllib.parse import urlparse

from .core import COUNTRY_CODES, Network, clean_text

PREFIX = "LOMBO_VISA_"

# Every setting, so a test can keep .env.example in sync.
SETTINGS = (
    "JELLYFIN_URL",
    "JELLYFIN_API_KEY",
    "WEBHOOK_URL",
    "DRY_RUN",
    "BLOCK_SESSIONS",
    "HOME_COUNTRIES",
    "LAN_SUBNETS",
    "EXEMPT_USERS",
    "INTERVAL_SECONDS",
    "MAX_BLOCKS_PER_ROUND",
    "MAX_WINDOW_DAYS",
    "GEOIP_DB",
    "GEOIP_URL",
    "STATE_FILE",
)


class ConfigError(Exception):
    """A setting is missing or invalid. The message says which one and what to do."""


@dataclass(frozen=True)
class Config:
    jellyfin_url: str
    jellyfin_api_key: str
    webhook_url: str | None
    dry_run: bool
    block_sessions: bool
    home_countries: frozenset[str]
    lan_subnets: tuple[Network, ...]
    exempt_users: frozenset[str]
    interval_seconds: int
    max_blocks_per_round: int
    max_window_days: int
    geoip_db: str
    geoip_url: str | None
    state_file: str


def _truthy(value: str, name: str) -> bool:
    """Strict on/off: a typo such as DRY_RUN=treu is an error, never a silent 'off'."""
    word = value.strip().lower()
    if word in {"1", "true", "yes", "on"}:
        return True
    if word in {"0", "false", "no", "off"}:
        return False
    raise ConfigError(f"{PREFIX}{name} must be true or false (got {value!r})")


def _items(value: str) -> list[str]:
    return [x.strip() for x in value.split(",") if x.strip()]


def _number(raw: str, name: str, minimum: int) -> int:
    try:
        n = int(raw)
    except ValueError:
        raise ConfigError(f"{PREFIX}{name} must be a whole number") from None
    if n < minimum:
        raise ConfigError(f"{PREFIX}{name} must be at least {minimum}")
    return n


def _url(name: str, value: str, schemes: set[str], example: str) -> str:
    p = urlparse(value)
    if p.scheme not in schemes or not p.netloc:
        raise ConfigError(f"{PREFIX}{name} must be a full URL such as {example} (got {value!r})")
    return value


def _check_names(env: dict[str, str]) -> None:
    """A misspelled setting (EXEMPT_USER, DRY_RUM) would be ignored without a word: stop instead."""
    for name in sorted(env):
        if name.startswith(PREFIX) and name[len(PREFIX) :] not in SETTINGS:
            close = difflib.get_close_matches(name[len(PREFIX) :], SETTINGS, n=1)
            hint = f" Did you mean {PREFIX}{close[0]}?" if close else ""
            raise ConfigError(f"{clean_text(name, 60)} is not a setting.{hint}")


def from_env(env: dict[str, str]) -> Config:
    _check_names(env)

    def get(name: str, default: str = "") -> str:
        return (env.get(PREFIX + name) or default).strip()

    url = _url("JELLYFIN_URL", get("JELLYFIN_URL").rstrip("/"), {"http", "https"}, "http://jellyfin:8096")
    key = get("JELLYFIN_API_KEY")
    if not key or key == "changeme":
        raise ConfigError(f"{PREFIX}JELLYFIN_API_KEY is not set. Create one in Jellyfin: Dashboard > API Keys.")

    webhook = get("WEBHOOK_URL") or None
    if webhook and urlparse(webhook).scheme not in {"http", "https"}:
        raise ConfigError(f"{PREFIX}WEBHOOK_URL must start with http:// or https:// (or be empty)")

    home = [c.upper() for c in _items(get("HOME_COUNTRIES"))]
    if not home:
        raise ConfigError(
            f"{PREFIX}HOME_COUNTRIES is not set. List the two-letter codes of your countries, e.g. US,CA."
        )
    if any(not re.fullmatch(r"[A-Z]{2}", c) for c in home):
        raise ConfigError(f"{PREFIX}HOME_COUNTRIES must be two-letter country codes separated by commas")
    for c in home:
        if c not in COUNTRY_CODES:
            hint = " (the United Kingdom is GB: use GB)" if c == "UK" else ""
            raise ConfigError(f"{PREFIX}HOME_COUNTRIES has {c!r}, which is not an ISO 3166 country code{hint}")

    lan: list[Network] = []
    for part in _items(get("LAN_SUBNETS")):
        try:
            lan.append(ipaddress.ip_network(part, strict=False))
        except ValueError:
            raise ConfigError(
                f"{PREFIX}LAN_SUBNETS has {part!r}, which is not a network such as 100.64.0.0/10"
            ) from None

    geoip_url = get("GEOIP_URL") or None
    if geoip_url:
        _url("GEOIP_URL", geoip_url, {"https"}, "https://example.com/country.mmdb.gz")

    # Safe by default: nothing is sent or ended until DRY_RUN=false, and nothing is ended until BLOCK_SESSIONS=true.
    return Config(
        jellyfin_url=url,
        jellyfin_api_key=key,
        webhook_url=webhook,
        dry_run=_truthy(get("DRY_RUN", "true"), "DRY_RUN"),
        block_sessions=_truthy(get("BLOCK_SESSIONS", "false"), "BLOCK_SESSIONS"),
        home_countries=frozenset(home),
        lan_subnets=tuple(lan),
        exempt_users=frozenset(u.lower() for u in _items(get("EXEMPT_USERS"))),
        interval_seconds=_number(get("INTERVAL_SECONDS", "60"), "INTERVAL_SECONDS", 10),
        max_blocks_per_round=_number(get("MAX_BLOCKS_PER_ROUND", "3"), "MAX_BLOCKS_PER_ROUND", 1),
        max_window_days=_number(get("MAX_WINDOW_DAYS", "30"), "MAX_WINDOW_DAYS", 1),
        geoip_db=get("GEOIP_DB", "/data/dbip-country-lite.mmdb"),
        geoip_url=geoip_url,
        state_file=get("STATE_FILE", "/data/state.json"),
    )
