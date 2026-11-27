# How it works

## Contents

- [One round](#one-round)
- [Verdicts](#verdicts)
- [What is remembered](#what-is-remembered)
- [Travel windows](#travel-windows) (and [self-service windows](#self-service-windows-optional))
- [What happens on a denial](#what-happens-on-a-denial)
- [Gateway mode (optional)](#gateway-mode-optional)
- [What it never does](#what-it-never-does)
- [The country database](#the-country-database)

## One round

1. Ask Jellyfin for its sessions (`GET /Sessions`). If that fails, the round ends and nothing is done.
2. First run ever: record every existing session as allowed and stop. Later runs go on.
3. (`run` in dry run still records allowed sessions in the state file; only `plan` writes nothing.) For each session whose `user:device` pair is not in the remembered list, find the country of the address Jellyfin recorded and decide.
4. Print one line per new session. Remember the allowed ones. Alert on the others (once). End the denied ones if blocking is on.
5. Sign out devices whose travel window ended (blocking on). Save the state file.

## Verdicts

| Verdict | Meaning | Remembered | Can be ended |
|---|---|---|---|
| `known` | this user and device were allowed before | already | no |
| `exempt` | the account is in `EXEMPT_USERS` | yes | no |
| `allow-home` | own network, or a country in `HOME_COUNTRIES` | yes | no |
| `allow-window` | foreign country, but the person has an open window for it | yes (with the window id) | no |
| `unknown` | no country could be found for the address (a code such as `ZZ`, `EU` or `A1` counts as no country) | yes, marked `unknown`, so it is never judged later | no |
| `deny` | foreign country, no window | no | yes, when `DRY_RUN=false` and `BLOCK_SESSIONS=true` |

An `unknown` session is remembered so that a database installed (or updated) later cannot turn it into a `deny` weeks afterwards;
only a genuinely new sign-in is judged. `unknown` happens when the database is missing or unreadable, the address is not in it, or Jellyfin gave an address that is not a
single IP address (for example a list). The first line of the alert says so.

## What is remembered

The state file (`LOMBO_VISA_STATE_FILE`, JSON) holds: whether the first run happened, the allowed `user:device` pairs, the denied or unknown
sessions already reported (kept for 7 days so the same one is not reported every minute), and the travel windows. Deleting it makes the
next run record the then-current sessions as allowed again and forget all windows. A damaged file is not deleted: it is moved to
`state.json.bad-<time>` next to it with a warning, and the run starts fresh. A file that cannot be read at all skips the round. Commands
that change the file (`run`, `travel add`, `travel revoke`, and a self-service request) take a lock first, so a `travel add` made during a round waits for it. It contains user ids, user names and device ids; no
addresses, passwords or tokens.

## Travel windows

`lombo-visa travel add NAME DAYS [--country CC]` looks the account up in Jellyfin (administrator accounts are refused), and stores a window
that starts now and lasts at most `MAX_WINDOW_DAYS`. A window with a country allows only that country; without one, any country.
It follows the account id, not the name. A new sign-in judged `allow-window` is remembered with the window's id. When the window has ended
or was revoked, and the person has no other open window, those devices are signed out (only with blocking on; otherwise `run` and `plan`
print "would sign out"). Devices that signed in from home are never touched.
The gateway also notes the device each time a window lets a sign-in through (not in dry run), so this works even when
Jellyfin records your proxy's address instead of the visitor's.

### Self-service windows (optional)

With `LOMBO_VISA_API_TOKEN` set, `lombo-visa api` lets a page service (for example Shelf) open a window **for the person who is
signed in there**. It makes the same kind of window as `travel add` (starts now, any country), with three differences:

- It is stored with `"self": true`, so the rules below can tell it from one an admin made.
- It is at most `LOMBO_VISA_SELF_MAX_DAYS` long (default 14, never more than `MAX_WINDOW_DAYS`).
- After a self-opened window **ends** (or is revoked, whichever comes first) the same person must wait
  `LOMBO_VISA_SELF_COOLDOWN_DAYS` (default 30, 0 = no wait) before opening another. Windows an admin made do not start a
  cool-down. Self-opened windows are kept in the state file for 400 days after they end (others for 30) so a long cool-down is not forgotten.

A request is refused when the person is not known to Jellyfin, is an administrator, already has an open or upcoming window (of any
kind), or is inside a cool-down. It changes the state file under the same lock as `run` and `travel add`, so it never overwrites a
round's work. Everything else about windows (sign-outs when one ends, revoke with `travel revoke`) is unchanged. The exact calls are in
[contracts.md](contracts.md); the safety notes are in [security.md](security.md). With `DRY_RUN=true` the API answers and applies
every rule but writes nothing.

## What happens on a denial

1. A line is printed: `<user> on <device> (<client>) from <country>: deny`.
2. With `DRY_RUN=false` one webhook message is sent (all of a round's messages together, at most ten lines).
3. With `BLOCK_SESSIONS=true` as well, `DELETE /Devices?id=<device id>` is sent. A 404 counts as success. Any other failure is printed and
   retried quietly each round while the session is still listed.
4. Not ended, only reported: the device id is empty, the device id also belongs to an allowed device of **another** account (ending it would
   sign that person out), or more than `MAX_BLOCKS_PER_ROUND` **different accounts** were denied in the same round (a message says so). Accounts, not sessions:
   one account opening many sessions cannot switch the sign-outs off. A wave across many accounts can, and that is reported loudly.

## Gateway mode (optional)

`lombo-visa gateway` decides **before** Jellyfin sees a sign-in. Your reverse proxy sends only the sign-in paths to it.

1. The path is classified: password sign-in, Quick Connect sign-in or the older `/Users/{id}/Authenticate`. Anything else, and any
   method but POST, gets a 404. The path is decoded once; double encoding and other ambiguous spellings are "not a sign-in".
2. The client address is the connecting address, or, if that is in `TRUSTED_PROXIES`, the rightmost `X-Forwarded-For` hop that is
   not itself trusted. The country comes from the same database as session mode; private ranges and `LAN_SUBNETS` are `LAN`.
3. For a password sign-in the typed user name is read from the body (the legacy path carries the account id). An account in
   `EXEMPT_USERS` is allowed. For a foreign country the travel windows decide, through `core.decide`, exactly as in session mode.
4. Allowed (or mode `off`/`log`, or `DRY_RUN=true`): the request is forwarded as it came and Jellyfin's answer returned untouched.
   Refused (`enforce` and `DRY_RUN=false` and verdict `deny`): a 403 with a short JSON message.
5. Quick Connect does not name anyone in its request. From abroad it is forwarded; if Jellyfin's answer names a person without a
   window (and the gateway is enforcing), the new session is ended with `POST /Sessions/Logout` and the 403 is returned instead.
6. One line goes to the audit log if `AUDIT_FILE` is set. Every doubt along the way means "allow".

Both modes can run side by side; they share the country database, the state file's windows and the settings.

## What it never does

- Delete or change users, passwords, policies, libraries or media.
- End a session that was already allowed or that existed before the first run.
- End anything for an `unknown` country, with no database, or for an account in `EXEMPT_USERS`. A session first seen as `unknown` stays allowed.
- Sign out a device id that another account also uses (window expiry included): it is only reported.
- Session mode: send a sign-in attempt, a password or a token anywhere. It sees sessions after the fact. (The gateway passes
  them on, to the configured upstream only, and never logs or stores them.)
- Session mode: sit in the path of traffic. If it stops, Jellyfin carries on exactly as before. The gateway is on the path of
  the sign-in paths you send it; everything else never touches it.
- Download anything except the country database, from one https address, with size limits.

## The country database

`lombo-visa geoip update` downloads `dbip-country-lite-YYYY-MM.mmdb.gz` from DB-IP (this month's, else last month's), unzips it with a
size limit (64 MB unpacked, five minutes in all), checks it opens, answers lookups and is a country database, then moves it into place with one rename. It does nothing if the
file in place is under 20 days old (unless `--force`). Any failure keeps the old file. `doctor` warns after 75 days.
`LOMBO_VISA_GEOIP_URL` can point to another https source of the same kind of file.
