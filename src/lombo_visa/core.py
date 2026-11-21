"""The decisions. Pure: no network, no disk, no clock (the time is passed in), so tests need no fakes.

Given the sessions Jellyfin lists now, what the app remembers, the travel windows and a function that
turns an address into a country, `plan_round()` says what each new session is and whether it should be
ended. Every doubt (no country, no address, a lookup that raises) comes out as "unknown", which is
allowed. Only a session that is new, from a known foreign country, with no matching window is "deny".
"""

import ipaddress
import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any

LAN = "LAN"
UNKNOWN = "unknown"
DAY = 86400.0

# Verdicts. Only DENY can ever lead to a session being ended.
KNOWN = "known"  # this user and device were allowed before; never judged again
EXEMPT = "exempt"
ALLOW_HOME = "allow-home"
ALLOW_WINDOW = "allow-window"
DENY = "deny"
# UNKNOWN (above) is the sixth: allowed, and remembered too, so a session let through because the country
# was not known is never judged (and ended) later, when a database appears or an address moves.

REMEMBERED = {EXEMPT, ALLOW_HOME, ALLOW_WINDOW, UNKNOWN}

# Codes a country database uses for "no country" or a region. They are not countries: treated as unknown.
PSEUDO_COUNTRIES = frozenset({"ZZ", "XX", "EU", "AP", "A1", "A2", "O1"})

# ISO 3166-1 alpha-2 (the 249 assigned codes) plus XK (Kosovo, not ISO but used by the databases).
COUNTRY_CODES = frozenset(
    [
        "AD",
        "AE",
        "AF",
        "AG",
        "AI",
        "AL",
        "AM",
        "AO",
        "AQ",
        "AR",
        "AS",
        "AT",
        "AU",
        "AW",
        "AX",
        "AZ",
        "BA",
        "BB",
        "BD",
        "BE",
        "BF",
        "BG",
        "BH",
        "BI",
        "BJ",
        "BL",
        "BM",
        "BN",
        "BO",
        "BQ",
        "BR",
        "BS",
        "BT",
        "BV",
        "BW",
        "BY",
        "BZ",
        "CA",
        "CC",
        "CD",
        "CF",
        "CG",
        "CH",
        "CI",
        "CK",
        "CL",
        "CM",
        "CN",
        "CO",
        "CR",
        "CU",
        "CV",
        "CW",
        "CX",
        "CY",
        "CZ",
        "DE",
        "DJ",
        "DK",
        "DM",
        "DO",
        "DZ",
        "EC",
        "EE",
        "EG",
        "EH",
        "ER",
        "ES",
        "ET",
        "FI",
        "FJ",
        "FK",
        "FM",
        "FO",
        "FR",
        "GA",
        "GB",
        "GD",
        "GE",
        "GF",
        "GG",
        "GH",
        "GI",
        "GL",
        "GM",
        "GN",
        "GP",
        "GQ",
        "GR",
        "GS",
        "GT",
        "GU",
        "GW",
        "GY",
        "HK",
        "HM",
        "HN",
        "HR",
        "HT",
        "HU",
        "ID",
        "IE",
        "IL",
        "IM",
        "IN",
        "IO",
        "IQ",
        "IR",
        "IS",
        "IT",
        "JE",
        "JM",
        "JO",
        "JP",
        "KE",
        "KG",
        "KH",
        "KI",
        "KM",
        "KN",
        "KP",
        "KR",
        "KW",
        "KY",
        "KZ",
        "LA",
        "LB",
        "LC",
        "LI",
        "LK",
        "LR",
        "LS",
        "LT",
        "LU",
        "LV",
        "LY",
        "MA",
        "MC",
        "MD",
        "ME",
        "MF",
        "MG",
        "MH",
        "MK",
        "ML",
        "MM",
        "MN",
        "MO",
        "MP",
        "MQ",
        "MR",
        "MS",
        "MT",
        "MU",
        "MV",
        "MW",
        "MX",
        "MY",
        "MZ",
        "NA",
        "NC",
        "NE",
        "NF",
        "NG",
        "NI",
        "NL",
        "NO",
        "NP",
        "NR",
        "NU",
        "NZ",
        "OM",
        "PA",
        "PE",
        "PF",
        "PG",
        "PH",
        "PK",
        "PL",
        "PM",
        "PN",
        "PR",
        "PS",
        "PT",
        "PW",
        "PY",
        "QA",
        "RE",
        "RO",
        "RS",
        "RU",
        "RW",
        "SA",
        "SB",
        "SC",
        "SD",
        "SE",
        "SG",
        "SH",
        "SI",
        "SJ",
        "SK",
        "SL",
        "SM",
        "SN",
        "SO",
        "SR",
        "SS",
        "ST",
        "SV",
        "SX",
        "SY",
        "SZ",
        "TC",
        "TD",
        "TF",
        "TG",
        "TH",
        "TJ",
        "TK",
        "TL",
        "TM",
        "TN",
        "TO",
        "TR",
        "TT",
        "TV",
        "TW",
        "TZ",
        "UA",
        "UG",
        "UM",
        "US",
        "UY",
        "UZ",
        "VA",
        "VC",
        "VE",
        "VG",
        "VI",
        "VN",
        "VU",
        "WF",
        "WS",
        "YE",
        "YT",
        "ZA",
        "ZM",
        "ZW",
        "XK",
    ]
)

Network = ipaddress.IPv4Network | ipaddress.IPv6Network


@dataclass(frozen=True)
class Session:
    user_id: str
    user_name: str
    device_id: str
    device_name: str
    client: str
    ip: str


@dataclass(frozen=True)
class Window:
    id: int
    user_id: str
    user_name: str
    country: str | None  # None = any country
    starts_at: float
    ends_at: float
    revoked_at: float | None = None

    def active(self, now: float) -> bool:
        return self.revoked_at is None and self.starts_at <= now < self.ends_at


@dataclass(frozen=True)
class Item:
    session: Session
    key: str
    country: str
    verdict: str
    window_id: int | None
    clash: bool  # the device id also belongs to another user's allowed device
    revoke: bool  # end this session (still subject to dry run and the BLOCK_SESSIONS setting)
    first_note: bool  # not reported before, so log and alert once

    @property
    def remember(self) -> bool:
        return self.verdict in REMEMBERED


@dataclass(frozen=True)
class RoundPlan:
    items: list[Item]
    tripped: bool  # too many denials in one round: nothing is ended, the person is told


# ---------------------------------------------------------------- small helpers
def clean_id(raw: object) -> str:
    """A Jellyfin user id the same way everywhere: no dashes, lower case."""
    return str(raw or "").replace("-", "").lower()


def device_key(user_id: str, device_id: str) -> str:
    return f"{clean_id(user_id)}:{device_id}"


def clean_text(raw: object, limit: int = 40) -> str:
    """Text that came from a client (names, device names), made safe to print or send.

    Control characters, line breaks and anything outside plain printable characters become "?", so a
    crafted user name cannot forge a log line or an alert. Long values are cut.
    """
    text = "".join(ch if ch.isprintable() and ch not in "  " else "?" for ch in str(raw or ""))  # noqa: RUF001 (the two separators are listed on purpose)
    text = text.strip()
    return text[:limit] + ("..." if len(text) > limit else "")


def chat_text(raw: object, limit: int = 40) -> str:
    """clean_text, plus no chat markup: `<!channel>`, `@everyone`, `[words](link)`, `<url|words>` and code quotes
    lose the characters that make them work, because an alert may go to a service that is not Discord."""
    return "".join("?" if ch in "<>@[]|`" else ch for ch in clean_text(raw, limit))


def normalise_ip(raw: str) -> str | None:
    """A bare address from what Jellyfin shows ("1.2.3.4", "1.2.3.4:5", "[::1]:5", "::ffff:1.2.3.4"), or None."""
    text = (raw or "").strip()
    if text.startswith("["):
        text = text[1:].split("]", 1)[0]
    elif text.count(":") == 1:  # IPv4 with a port
        text = text.split(":", 1)[0]
    text = text.split("%", 1)[0]
    try:
        ip = ipaddress.ip_address(text)
    except ValueError:
        return None
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped is not None:
        ip = ip.ipv4_mapped
    return str(ip)


def is_lan(ip: str, extra: tuple[Network, ...] = ()) -> bool:
    a = ipaddress.ip_address(ip)
    return a.is_private or a.is_loopback or a.is_link_local or any(a in n for n in extra if n.version == a.version)


def country_of(raw_ip: str, lookup: Callable[[str], str | None], lan: tuple[Network, ...] = ()) -> str:
    """LAN, an upper-case country code, or UNKNOWN. Never raises: a broken lookup is UNKNOWN."""
    ip = normalise_ip(raw_ip)
    if ip is None:
        return UNKNOWN
    if is_lan(ip, lan):
        return LAN
    try:
        code = lookup(ip)
    except Exception:  # noqa: BLE001 (fail open: whatever went wrong inside the lookup, the answer is "unknown")
        return UNKNOWN
    if not isinstance(code, str) or not re.fullmatch(r"[A-Za-z]{2}", code) or code.upper() in PSEUDO_COUNTRIES:
        return UNKNOWN
    return code.upper()


# ---------------------------------------------------------------- the rule
def decide(
    country: str, home: frozenset[str], windows: list[Window], user_id: str, now: float
) -> tuple[str, int | None]:
    """(verdict, window id) for a NEW session. Home or LAN is allowed; unknown is allowed; abroad needs a window."""
    if country == LAN or country in home:
        return ALLOW_HOME, None
    if country == UNKNOWN:
        return UNKNOWN, None
    uid = clean_id(user_id)
    for w in windows:
        if clean_id(w.user_id) == uid and w.active(now) and w.country in (None, country):
            return ALLOW_WINDOW, w.id
    return DENY, None


def plan_round(
    sessions: list[Session],
    known: Mapping[str, Any],
    noted: Mapping[str, Any],
    windows: list[Window],
    home: frozenset[str],
    lan: tuple[Network, ...],
    exempt: frozenset[str],
    lookup: Callable[[str], str | None],
    now: float,
    max_blocks: int,
) -> RoundPlan:
    # Who holds each device id: every remembered key, plus (below) every session in this list that is not denied.
    holders: dict[str, set[str]] = {}
    for k in known:
        user, _, dev = k.partition(":")
        holders.setdefault(dev, set()).add(user)
    exempt_ids = {clean_id(x) for x in exempt}  # an id pasted with dashes still matches
    judged: list[tuple[Session, str, str, str, int | None]] = []  # session, key, country, verdict, window id
    for s in sessions:
        key = device_key(s.user_id, s.device_id)
        if key in known:
            judged.append((s, key, "", KNOWN, None))
        elif s.user_name.lower() in exempt or clean_id(s.user_id) in exempt_ids:
            judged.append((s, key, "", EXEMPT, None))
        else:
            country = country_of(s.ip, lookup, lan)
            verdict, wid = decide(country, home, windows, s.user_id, now)
            judged.append((s, key, country, verdict, wid))
    for s, _, _, verdict, _ in judged:
        if verdict != DENY:
            holders.setdefault(s.device_id, set()).add(clean_id(s.user_id))
    items: list[Item] = []
    for s, key, country, verdict, wid in judged:
        if verdict in (KNOWN, EXEMPT):
            items.append(Item(s, key, country, verdict, None, False, False, False))
            continue
        clash = bool(holders.get(s.device_id, set()) - {clean_id(s.user_id)})
        revoke = verdict == DENY and not clash and bool(s.device_id)
        items.append(
            Item(s, key, country, verdict, wid, clash, revoke, verdict in (DENY, UNKNOWN) and key not in noted)
        )
    # Count accounts, not sessions: one account with many sessions (someone who has that password) must not be able
    # to switch the sign-outs off. Many different accounts denied at once points at a setup problem instead.
    denied_accounts = {clean_id(i.session.user_id) for i in items if i.verdict == DENY}
    tripped = len(denied_accounts) > max_blocks
    if tripped:
        items = [Item(i.session, i.key, i.country, i.verdict, i.window_id, i.clash, False, i.first_note) for i in items]
    return RoundPlan(items, tripped)


def due_signouts(devices: Mapping[str, Mapping[str, Any]], windows: list[Window], now: float) -> list[str]:
    """Device keys that were allowed only by a window that has ended or been revoked.

    A person with another open window keeps their devices.
    """
    by_id = {w.id: w for w in windows}
    out: list[str] = []
    for key, info in devices.items():
        wid = info.get("window")
        if not isinstance(wid, int):
            continue
        w = by_id.get(wid)
        if w is not None and w.active(now):
            continue
        user = key.partition(":")[0]
        if any(clean_id(x.user_id) == user and x.active(now) for x in windows):
            continue
        out.append(key)
    return out


def new_window(
    existing: list[Window], user_id: str, user_name: str, days: float, country: str | None, now: float, max_days: int
) -> Window:
    """A window that starts now. Raises ValueError with a plain message when the request is not allowed."""
    if not 0 < days <= max_days:
        raise ValueError(f"days must be more than 0 and at most {max_days}")
    code = (country or "").strip().upper() or None
    if code is not None and not re.fullmatch(r"[A-Z]{2}", code):
        raise ValueError("country must be two letters such as DE, or left out for any country")
    if code is not None and code not in COUNTRY_CODES:
        raise ValueError(f"{code} is not a country code" + (" (the United Kingdom is GB)" if code == "UK" else ""))
    return Window(
        max((w.id for w in existing), default=0) + 1, clean_id(user_id), user_name, code, now, now + days * DAY
    )


# ---------------------------------------------------------------- words
def describe(i: Item) -> str:
    s = i.session
    where = i.country or "?"
    return (
        f"{clean_text(s.user_name)} on {clean_text(s.device_name)} ({clean_text(s.client)}) from {where}: {i.verdict}"
    )


def alert_text(i: Item, ended: bool) -> str:
    s = i.session
    who = f"{chat_text(s.user_name)} on {chat_text(s.device_name)} ({chat_text(s.client)})"
    if i.verdict == UNKNOWN:
        return f"Could not tell where {who} signed in from (no country for that address). Let through."
    if i.clash:
        return f"New sign-in from {i.country}: {who}. Not ended: its device id also belongs to another account."
    if ended:
        return f"New sign-in from {i.country} ended: {who}."
    if not s.device_id:
        return f"New sign-in from {i.country}: {who}. Cannot end it: the session has no device id."
    return f"New sign-in from {i.country} (no travel window): {who}."
