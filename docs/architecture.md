# How it is built

## Contents

- [The shape](#the-shape)
- [Module contracts](#module-contracts)
- [Decisions made while porting](#decisions-made-while-porting)
- [Kept, cut and generalised](#kept-cut-and-generalised)
- [The country lookup and why there is a reader of our own](#the-country-lookup-and-why-there-is-a-reader-of-our-own)
- [Safe by default](#safe-by-default)
- [Settings](#settings)
- [Alerts](#alerts)

## The shape

```
Jellyfin  <--  jellyfin.py  -->  cli.py  -->  core.py (decides)
 /Sessions                          |  \
 /Users                             |   +--> geoip.py --> mmdb.py --> country database file (local)
 DELETE /Devices                    |
                                    +--> state.py   (JSON file: allowed devices, windows, notes)
                                    +--> notify.py  --> your webhook
```

`cli.py` fetches the session list, hands it to `core.plan_round()`, then acts on the answer. `core.py` never touches the
network, the disk or the clock; the time and the country function are passed in.

## Module contracts

| Module | Takes | Returns / does |
|---|---|---|
| `core.plan_round(sessions, known, noted, windows, home, lan, exempt, lookup, now, max_blocks)` | plain data | `RoundPlan(items, tripped)`; each `Item` has a verdict (`known`, `exempt`, `allow-home`, `allow-window`, `unknown`, `deny`), `revoke`, `clash`, `first_note` |
| `core.decide(country, home, windows, user_id, now)` | a country code (or `LAN` / `unknown`) | `(verdict, window id)` |
| `core.due_signouts(devices, windows, now)` | allowed devices and windows | device keys whose window has ended |
| `core.country_of(raw_ip, lookup, lan)` | an address string, a function `ip -> code or None` | `LAN`, a code or `unknown`; never raises |
| `geoip.Lookup.country(ip)` | an address | upper-case code or `None` (None means "do not know") |
| `mmdb.Reader(bytes).get(ip)` | an address | the record or `None`; raises `MmdbError` on a bad file |
| `geoip.update(path, url, now, force)` | where to put the file | one line; raises `GeoIpError`, old file untouched |
| `state.load/save` | a path | the JSON state; a damaged file means "start fresh" |
| `gateway.classify(target)` | a request target | `(kind, id)` for the sign-in paths, else `None` (the path is decoded once; ambiguous forms are `None`) |
| `gateway.client_address(peer, forwarded, trusted)` | the connecting address, `X-Forwarded-For` values, trusted networks | `(address or None, header ignored)` |
| `gateway.Gateway.process(...)` | one classified request | a `Reply` (Jellyfin's answer untouched, or a 403) |
| `audit.record(file, mode, decision, country, user, ip, now)` | one decision | appends one JSON line; never raises |

## Decisions made while porting

1. **Polling by default, a gateway as an option.** The private original put a gateway in front of Jellyfin's sign-in endpoints (the reverse proxy
   sent them to the gateway, which looked up the country, then forwarded to Jellyfin or refused with a 403). That needs
   reverse-proxy configuration for every setup and puts the app on the path of every login. A public tool cannot assume a
   particular proxy, and a crash would have to be survived by proxy settings. So the zero-setup default reads `GET /Sessions`
   and ends new sessions after the fact. Costs: a window of up to one round in which the new session is usable, and no
   protection against password guessing. Benefit: if LomboVisa stops, Jellyfin is unaffected. People who want the refusal
   before the sign-in can also run `lombo-visa gateway` (`gateway.py`), behind a proxy rule they set up; it reuses `core.decide`,
   the country lookup and the state file's windows, and fails open the same way.
2. **No admin panel, no self-service.** Travel windows are made with `lombo-visa travel add`. The original's
   friend-facing "I am travelling" form (it needed the PIN system of the private stack) and its "notify the owner" page were cut.
3. **Windows live in the state file** (JSON) instead of a database.
4. **Sign-out uses `DELETE /Devices?id=`**, as the original did for expiring windows. That removes the device and its tokens, for
   every user of that device id. See the device-id clash rule in the security review.
5. **Two switches, three stages.** `DRY_RUN` (default true) then `BLOCK_SESSIONS` (default false). The original had off/log/enforce.
6. **A circuit breaker** (`MAX_BLOCKS_PER_ROUND`) is new: a wrong address source or a bad database would otherwise end many accounts' sessions at once.
   It counts distinct accounts, not sessions, so that someone holding one stolen password cannot trip it by opening many sessions.
7. **Existing sessions are recorded on the first run** and never judged, matching "sessions that already exist keep working anywhere".

## Kept, cut and generalised

| Original | Here |
|---|---|
| Fail open on every doubt (no database, unknown address, broken tables, mode off/log) | Kept: every path ends in "allow" and the tests cover each |
| Private/loopback/link-local addresses count as home | Kept, plus `LAN_SUBNETS` for ranges such as a VPN |
| Home country list | Kept, now a required setting (no built-in default) |
| Windows per account (id), optional country, admin accounts excluded | Kept |
| Max window length, cooldown for self-service | Max length kept (`MAX_WINDOW_DAYS`); cooldown cut with self-service |
| Devices signed in under a window are signed out when it ends | Kept |
| Keyed hash of addresses in a database of events | Kept as an opt-in file: `AUDIT_FILE`, JSON lines with a keyed hash of the address (`audit.py`) |
| Discord posts | Generic webhook, mentions switched off |
| Update script (`curl`, shell) | `lombo-visa geoip update` in Python, with extra checks (https only, same-host redirects, size caps, structure check) |
| GeoIP reading with the `maxminddb` package | A small reader of our own, see below |
| Caddy block, `/away` page, People-tab UI, login message | Cut: tied to the private stack |

## The country lookup and why there is a reader of our own

The app stays standard-library only. The original used the `maxminddb` package. Adding a dependency to read one field of one file
seemed worse than about 150 lines of reader (`mmdb.py`) that can be read in one sitting, because the file is **downloaded input**: the
reader checks every offset, forbids pointer chains, caps nesting, the number of values and the text bytes per lookup, never returns raw bytes values, and the tests feed it hand-made
hostile bytes. The tests use a tiny database generated in memory (`tests/mmdb_builder.py`); no real database is in the repository.
`geoip.Lookup` is a one-method interface, so anyone who prefers `maxminddb` can add a class next to `MmdbLookup` and use that in `cli._lookup`.

## Safe by default

- `DRY_RUN` is true until the person sets it to false; `BLOCK_SESSIONS` is false until set to true.
- `plan` never writes anything (state, Jellyfin or webhook).
- `doctor` separates "Jellyfin is unreachable" from "the key was refused", and warns about a missing or old database.
- A bad setting, or a setting name that does not exist, stops the app at start-up and names the variable.
- A failed round (Jellyfin unreachable, state not readable or writable, or any unexpected error) does nothing and the loop tries again.
- `doctor` and `plan` never write the state file; `run`, `travel add` and `travel revoke` hold a lock while they change it.
- Dry-run notes are kept apart from live notes, so going live reports everything once.

## Settings

Every setting is an environment variable with the `LOMBO_VISA_` prefix, validated in `config.py`. To add one: read it in `config.py`
with a clear error, add it to `SETTINGS`, add a line to `.env.example` (a test keeps the three in sync), and add a test.

## Alerts

`notify.send(url, message)` posts JSON with the text under `content` and `text` and `allowed_mentions: {"parse": []}`, which Discord,
Slack and many bridges accept. Names that come from clients are cleaned (`core.clean_text`) before they reach a log line or an alert.
