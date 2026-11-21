"""Command line. Talks to the outside world; core.py decides.

lombo-visa doctor                        check the settings, Jellyfin, the country database and the state file
lombo-visa plan                          show what the next round would do; changes nothing, ever
lombo-visa run [--loop]                  do it. Alerts and sign-outs only when DRY_RUN=false (sign-outs also
                                         need BLOCK_SESSIONS=true); --loop repeats every INTERVAL_SECONDS
lombo-visa travel add NAME DAYS [--country CC]   allow a person to sign in from abroad, starting now
lombo-visa travel list                   active and upcoming windows
lombo-visa travel revoke ID              end a window; its devices are signed out on the next round
lombo-visa geoip update [--force]        download the country database (monthly is enough)
"""

import argparse
import os
import sys
import time
from collections.abc import Callable
from typing import Any

from . import __version__, core, geoip, notify, state
from .config import Config, ConfigError, from_env
from .jellyfin import Jellyfin, JellyfinError

MAX_ALERT_CHARS = 1800
MAX_ALERT_LINES = 10
MAX_DEVICES = 5000


def _jf(cfg: Config) -> Jellyfin:
    return Jellyfin(cfg.jellyfin_url, cfg.jellyfin_api_key)


def _when(t: float) -> str:
    return time.strftime("%Y-%m-%d %H:%M UTC", time.gmtime(t))


def _lookup(cfg: Config) -> tuple[Callable[[str], str | None], bool]:
    """(country function, whether the database could be opened). Without it every answer is "unknown"."""
    lk = geoip.open_lookup(cfg.geoip_db)
    if lk is None:
        return (lambda _ip: None), False
    return lk.country, True


def _mode(cfg: Config) -> str:
    if cfg.dry_run:
        return "dry run (decisions are printed; nothing is sent or ended)"
    if cfg.block_sessions:
        return "LIVE: alerts are sent and new sign-ins from abroad without a window are ended"
    return "alerts only (sign-outs are off; set LOMBO_VISA_BLOCK_SESSIONS=true to end sessions)"


def _noted(st: dict[str, Any], live: bool) -> dict[str, Any]:
    """Dry-run notes are kept apart, so going live reports everything again once."""
    return (
        {k[4:]: v for k, v in st["noted"].items() if k.startswith("dry:")}
        if not live
        else {k: v for k, v in st["noted"].items() if not k.startswith("dry:")}
    )


def _evaluate(cfg: Config, st: dict[str, Any], sessions: list[core.Session], now: float) -> tuple[core.RoundPlan, bool]:
    lookup, have_db = _lookup(cfg)
    plan = core.plan_round(
        sessions,
        st["devices"],
        _noted(st, not cfg.dry_run),
        state.windows(st),
        cfg.home_countries,
        cfg.lan_subnets,
        cfg.exempt_users,
        lookup,
        now,
        cfg.max_blocks_per_round,
    )
    return plan, have_db


# ---------------------------------------------------------------- doctor
def doctor(cfg: Config, now: float) -> int:
    jf = _jf(cfg)
    ok = True

    def reachable() -> str:
        info = jf.public_info()
        return f"{core.clean_text(info.get('ServerName'))} {core.clean_text(info.get('Version'))}"

    def key_accepted() -> str:
        jf.info()
        return "yes"

    def sessions() -> str:
        found = jf.sessions()
        public = sum(1 for s in found if core.country_of(s.ip, lambda _ip: "QQ", cfg.lan_subnets) == "QQ")
        text = f"{len(found)} signed-in session(s), {public} from a public address"
        if found and not public:
            text += (
                " (all are on your own network: if people sign in through a reverse proxy, Jellyfin may only see the "
                "proxy; see docs/setup.md, 'Real addresses')"
            )
        return text

    checks: list[tuple[str, Callable[[], str]]] = [
        ("Jellyfin reachable", reachable),
        ("API key accepted", key_accepted),
        ("sessions readable", sessions),
    ]
    for label, check in checks:
        try:
            print(f"[ ok ] {label}: {check()}")
        except JellyfinError as e:
            print(f"[FAIL] {label}: {e}")
            ok = False
    age = geoip.age_days(cfg.geoip_db, now)
    lk = geoip.open_lookup(cfg.geoip_db)
    if lk is None:
        print(
            f"[WARN] country database: {cfg.geoip_db} is missing or unusable. Every address is 'unknown' and "
            "is let through. Run: lombo-visa geoip update"
        )
    elif age is not None and age > geoip.STALE_DAYS:
        print(f"[WARN] country database: {age:.0f} days old; run: lombo-visa geoip update")
    else:
        print(f"[ ok ] country database: {cfg.geoip_db} ({age or 0:.0f} days old)")
    if lk is not None:
        try:
            codes = lk.codes()
        except Exception:  # noqa: BLE001 (a check that cannot run is skipped, not a failure)
            codes = set()
        if codes and not cfg.home_countries & codes:
            print(
                f"[WARN] home countries: none of {', '.join(sorted(cfg.home_countries))} appears in the database, so "
                "every address from outside your own network would be judged foreign. "
                "Check the codes (the United Kingdom is GB)."
            )
    print(f"[ ok ] home countries: {', '.join(sorted(cfg.home_countries))} (and your own network)")
    print(f"[ ok ] webhook: {'set' if cfg.webhook_url else 'not set (alerts are skipped)'}")
    try:
        state.load(cfg.state_file, quarantine=False)  # doctor never moves or writes the state file
        state.check_writable(cfg.state_file)
        print(f"[ ok ] state file: {cfg.state_file} is readable and its folder is writable")
    except state.StateError as e:
        print(f"[FAIL] state file: {e}")
        ok = False
    print(f"[ ok ] mode: {_mode(cfg)}")
    if cfg.block_sessions and cfg.dry_run:
        print("[WARN] BLOCK_SESSIONS is true but DRY_RUN is still true: nothing will be ended")
    return 0 if ok else 1


# ---------------------------------------------------------------- plan and run
def plan_cmd(cfg: Config, now: float) -> int:
    st = state.load(cfg.state_file, quarantine=False)
    sessions = _jf(cfg).sessions()
    if not st["baselined"]:
        print(
            f"No baseline yet: the first `run` records the {len(sessions)} session(s) that exist now as allowed "
            "(sessions that already exist keep working anywhere) and judges only later sign-ins."
        )
        return 0
    result, have_db = _evaluate(cfg, st, sessions, now)
    shown = [i for i in result.items if i.verdict != core.KNOWN]
    for i in shown:
        print(core.describe(i) + (" -> would end it" if i.revoke else ""))
    if not shown:
        print("No new sign-ins to judge.")
    if result.tripped:
        print(f"note: more than {cfg.max_blocks_per_round} accounts denied in one round; nothing would be ended.")
    if not have_db:
        print("note: no country database, so every address is 'unknown' and allowed.")
    for key in core.due_signouts(st["devices"], state.windows(st), now):
        print(f"travel window over: would sign out device {key.partition(':')[2][:40]!r}")
    return 0


def run_once(cfg: Config, now: float) -> int:
    with state.locked(cfg.state_file):  # `travel add` and friends wait for the round to finish, then add to its result
        return _round(cfg, now)


def _round(cfg: Config, now: float) -> int:
    jf = _jf(cfg)
    st = state.load(cfg.state_file)
    sessions = jf.sessions()  # a failure here ends the round with nothing done (fail open)
    live = not cfg.dry_run
    if not st["baselined"]:
        for s in sessions:
            st["devices"][core.device_key(s.user_id, s.device_id)] = {
                "user": core.clean_text(s.user_name),
                "first": now,
            }
        st["baselined"] = True
        state.save(cfg.state_file, st)
        print(f"First run: recorded {len(sessions)} existing session(s) as allowed. Later sign-ins are judged.")
        return 0

    result, have_db = _evaluate(cfg, st, sessions, now)
    if not have_db:
        print("[WARN] no country database; every new sign-in is 'unknown' and allowed", file=sys.stderr)
    messages: list[str] = []
    prefix = "" if live else "dry:"
    for i in result.items:
        if i.verdict == core.KNOWN:
            continue
        if i.remember:
            if len(st["devices"]) < MAX_DEVICES:
                entry: dict[str, Any] = {
                    "user": core.clean_text(i.session.user_name),
                    "first": now,
                    "window": i.window_id,
                }
                if i.verdict == core.UNKNOWN:
                    entry["unknown"] = True  # let through for lack of a country; never judged later
                st["devices"][i.key] = entry
            if i.verdict != core.UNKNOWN:
                print(core.describe(i))
                continue
        if not i.first_note:
            if i.revoke and live and cfg.block_sessions:  # an earlier attempt failed: try again quietly
                _end(jf, i)
            continue
        print(core.describe(i) + (" -> would end it" if i.revoke else ""))
        ended = bool(i.revoke and live and cfg.block_sessions and _end(jf, i))
        messages.append(core.alert_text(i, ended))
        st["noted"][prefix + i.key] = now
    if result.tripped and any(i.first_note and i.verdict == core.DENY for i in result.items):
        messages.append(
            f"Sign-ins from abroad for more than {cfg.max_blocks_per_round} different accounts in one round. "
            "Nothing was ended: this looks like a setup problem (wrong address seen, bad database) "
            "or a wave of stolen passwords. Check `lombo-visa plan`."
        )

    messages += _expire_windows(cfg, jf, st, now, live, sessions)
    state.prune(st, now)
    state.save(cfg.state_file, st)

    if messages and live and cfg.webhook_url:
        text = "\n".join(messages[:MAX_ALERT_LINES])
        if len(messages) > MAX_ALERT_LINES:
            text += f"\n...and {len(messages) - MAX_ALERT_LINES} more"
        try:
            notify.send(cfg.webhook_url, text[:MAX_ALERT_CHARS])
        except notify.NotifyError as e:
            print(f"[FAIL] {e}", file=sys.stderr)
    elif messages:
        print(f"({len(messages)} alert(s) not sent: {'dry run' if cfg.dry_run else 'no webhook set'})")
    return 0


def _end(jf: Jellyfin, i: core.Item) -> bool:
    try:
        jf.sign_out_device(i.session.device_id)
    except JellyfinError as e:
        print(f"[FAIL] could not end the session of {core.clean_text(i.session.user_name)}: {e}", file=sys.stderr)
        return False
    return True


def _expire_windows(
    cfg: Config, jf: Jellyfin, st: dict[str, Any], now: float, live: bool, sessions: list[core.Session]
) -> list[str]:
    due = core.due_signouts(st["devices"], state.windows(st), now)
    if not due:
        return []
    if not (live and cfg.block_sessions):
        for key in due:
            print(f"travel window over: would sign out device {key.partition(':')[2][:40]!r}")
        return []
    done = shared = 0
    for key in due:
        user, _, device = key.partition(":")
        # Removing a device removes it for everyone who uses that id: leave a shared one alone.
        others = {k.partition(":")[0] for k in st["devices"] if k not in due and k.partition(":")[2] == device}
        others |= {core.clean_id(s.user_id) for s in sessions if s.device_id == device}
        if others - {user}:
            print(f"travel window over: device {device[:40]!r} is also used by another account; not signed out")
            del st["devices"][key]
            shared += 1
            continue
        try:
            jf.sign_out_device(device)
        except JellyfinError as e:
            print(f"[FAIL] could not sign out a device: {e}", file=sys.stderr)
            continue
        del st["devices"][key]
        done += 1
    out = [f"Travel window over: signed out {done} device(s) that signed in abroad."] if done else []
    if shared:
        out.append(f"Travel window over: left {shared} shared device(s) signed in (also used by another account).")
    return out


# ---------------------------------------------------------------- travel and geoip
def travel(cfg: Config, args: argparse.Namespace, now: float) -> int:
    if args.action == "list":
        st = state.load(cfg.state_file, quarantine=False)
        rows = [w for w in state.windows(st) if w.revoked_at is None and w.ends_at > now]
        for w in sorted(rows, key=lambda w: w.ends_at):
            where = w.country or "any country"
            print(f"#{w.id} {core.clean_text(w.user_name)}: {where}, until {_when(w.ends_at)}")
        if not rows:
            print("No active or upcoming travel windows.")
        return 0
    if args.action == "revoke":
        with state.locked(cfg.state_file):
            st = state.load(cfg.state_file)
            old = next((x for x in state.windows(st) if x.id == args.id and x.revoked_at is None), None)
            if old is None:
                print(f"[FAIL] no active window #{args.id}", file=sys.stderr)
                return 1
            state.put_window(
                st, core.Window(old.id, old.user_id, old.user_name, old.country, old.starts_at, old.ends_at, now)
            )
            state.save(cfg.state_file, st)
        print(f"Window #{old.id} revoked. Its devices are signed out on the next round (if sign-outs are on).")
        return 0
    users = _jf(cfg).users()
    user = next((u for u in users if str(u.get("Name", "")).lower() == args.name.lower()), None)
    if user is None:
        print(f"[FAIL] Jellyfin has no account named {core.clean_text(args.name)!r}", file=sys.stderr)
        return 1
    if (user.get("Policy") or {}).get("IsAdministrator"):
        print("[FAIL] administrator accounts do not get travel windows; use a normal viewing account", file=sys.stderr)
        return 1
    with state.locked(cfg.state_file):
        st = state.load(cfg.state_file)
        try:
            w = core.new_window(
                state.windows(st),
                str(user.get("Id", "")),
                str(user.get("Name", "")),
                args.days,
                args.country,
                now,
                cfg.max_window_days,
            )
        except ValueError as e:
            print(f"[FAIL] {e}", file=sys.stderr)
            return 1
        state.put_window(st, w)
        state.save(cfg.state_file, st)
    who, where = core.clean_text(w.user_name), w.country or "any country"
    print(f"Window #{w.id}: {who} may sign in from {where} until {_when(w.ends_at)}.")
    return 0


def geoip_update(cfg: Config, force: bool, now: float) -> int:
    try:
        print("geoip:", geoip.update(cfg.geoip_db, cfg.geoip_url, now, force))
    except geoip.GeoIpError as e:
        print(f"[FAIL] geoip: {e}. The old file, if any, was kept.", file=sys.stderr)
        return 1
    return 0


def _describe_failure(e: Exception) -> str:
    if isinstance(e, JellyfinError | state.StateError):
        return str(e)
    return (
        f"unexpected {type(e).__name__}: {core.clean_text(e, 200)}. This round was skipped; the next one runs as usual."
    )


def main(argv: list[str] | None = None, env: dict[str, str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="lombo-visa", description=__doc__, formatter_class=argparse.RawTextHelpFormatter)
    p.add_argument("--version", action="version", version=__version__)
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("doctor")
    sub.add_parser("plan")
    run = sub.add_parser("run")
    run.add_argument("--loop", action="store_true")
    tr = sub.add_parser("travel").add_subparsers(dest="action", required=True)
    add = tr.add_parser("add")
    add.add_argument("name")
    add.add_argument("days", type=float)
    add.add_argument("--country", default="")
    tr.add_parser("list")
    tr.add_parser("revoke").add_argument("id", type=int)
    gi = sub.add_parser("geoip").add_subparsers(dest="action", required=True)
    gi.add_parser("update").add_argument("--force", action="store_true")
    args = p.parse_args(argv)

    try:
        cfg = from_env(dict(os.environ) if env is None else env)
    except ConfigError as e:
        print(f"[FAIL] settings: {e}", file=sys.stderr)
        return 2

    try:
        if args.cmd == "doctor":
            return doctor(cfg, time.time())
        if args.cmd == "plan":
            return plan_cmd(cfg, time.time())
        if args.cmd == "travel":
            return travel(cfg, args, time.time())
        if args.cmd == "geoip":
            return geoip_update(cfg, args.force, time.time())
        while True:
            try:
                run_once(cfg, time.time())
            except Exception as e:  # noqa: BLE001 (one bad round must never end the loop: say what happened, go on)
                print(f"[FAIL] {_describe_failure(e)}", file=sys.stderr)  # nothing was changed by the failed step
                if not args.loop:
                    return 1
            if not args.loop:
                return 0
            time.sleep(cfg.interval_seconds)
    except Exception as e:  # noqa: BLE001 (a command that fails says why instead of printing a traceback)
        print(f"[FAIL] {_describe_failure(e)}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
