# Security review

> **This is the author's own review.** LomboVisa has **not had an independent review** and has **not been run on a live server**; it has run against a
> fake Jellyfin and a throwaway one ([the run below](#real-server-run-2026-11-25)). Treat other statements about how Jellyfin behaves as unconfirmed until the
> checklist at the end is done. Written 2026-11-20. An unattended audit on 2026-11-21 (read-only, reproductions against the fake server only) found the
> issues listed under [Audit findings](#audit-findings-2026-11-21); all are fixed and each has a regression test. That audit never saw a
> real Jellyfin either, and it was not a person reading the code, so the review items in the checklist stay open.

## Contents

- [What is being protected, and what is not](#what-is-being-protected-and-what-is-not)
- [Where the data comes from, and who is trusted](#where-the-data-comes-from-and-who-is-trusted)
- [Threats](#threats)
- [Audit findings (2026-11-21)](#audit-findings-2026-11-21)
- [Summary: what is not defended](#summary-what-is-not-defended)
- [Powers and secrets the app holds](#powers-and-secrets-the-app-holds)
- [Checklist before the first release](#checklist-before-the-first-release)

## What is being protected, and what is not

**Goal.** A person who has a correct Jellyfin password but is in a country you did not list should not get a lasting session, unless
you allowed that trip. In plain terms: stolen or guessed passwords are much less useful from abroad.

**Not a goal.** Stopping password guessing (the guesses reach Jellyfin; the tool only sees the successful sign-ins), stopping people
inside your countries, stopping a stolen token used from abroad (an existing session is never judged), protecting administrator
accounts from a determined attacker, or anything on the media or file side of Jellyfin.

**Shape that drives everything below.** LomboVisa sits beside Jellyfin and polls `GET /Sessions`. It never sees the password and
never sees the request. A new session therefore exists for up to one round (default 60 s) before it can be ended. This is a deliberate
trade for failing safe: if LomboVisa is down or wrong, Jellyfin still works for everyone. The optional gateway mode (`lombo-visa gateway`)
closes the gap by refusing before the sign-in exists, but becomes a single point of failure for the sign-in paths and needs
proxy-specific setup. **This file covers the session mode; the gateway and the audit log are in [security.md](security.md).**

## Where the data comes from, and who is trusted

| Input | Source | Trust |
|---|---|---|
| Session list (user, device id, device name, client, address) | Jellyfin, answering to the API key | The *structure* is trusted. The *values* (names, device ids) are chosen by clients and are untrusted text. The address is only as good as Jellyfin's proxy settings. |
| Country database | Downloaded over https from one site | Untrusted file until it passes the checks; after that, trusted for its answers (see tampered database). |
| Settings, state file, travel windows | The operator, local disk | Trusted. Whoever can write the state file can create windows. |
| Webhook URL, Jellyfin URL, database URL | The operator | Trusted; not restricted to public addresses on purpose (a webhook or Jellyfin on your LAN is normal). |

## Threats

Each: what an attacker can do, what the tool does about it, and what remains.

### 1. Spoofed `X-Forwarded-For` (or any forwarded address)

*Attack.* Jellyfin records an address from the forwarded header if the connection comes from a proxy it trusts. If Jellyfin trusts too
much, or the proxy forwards a client-supplied header, an attacker abroad writes a home address and is judged `allow-home`.
*Defence.* The tool can do nothing about a lie that Jellyfin already believes. `doctor` warns when every session looks local (the
opposite mistake, which makes the fence silently open), and [setup.md](setup.md#real-addresses) says how to configure it and how to
test with a phone on mobile data. A header holding several addresses is not accepted as one address (judged `unknown`, allowed, and
reported once).
*Not defended.* Misconfiguration. This is the most likely way the tool silently does nothing.

### 2. VPN users

*Attack/effect.* Someone abroad using an exit in a home country is indistinguishable from a home user. Someone at home using an exit abroad is denied.
*Defence.* None technical. The sign-out message does not explain; the owner is told by the alert and can add a window or exempt the account.
*Not defended.* A foreign attacker using a VPN exit in a home country. Do not rely on this tool against a determined attacker.

### 3. Mobile and roaming users

*Effect.* A phone on roaming data often exits in the home country (fine) but sometimes resolves elsewhere; carrier-grade NAT and mobile
ranges are the least accurate part of any free database. Travellers who forget to ask for a window get signed out on a **new** sign-in.
*Defence.* Dry run first (a week or two shows who would be hit); `travel add` for planned trips; existing sessions and known devices are never ended;
`EXEMPT_USERS` as a way back in; the safety stop when many accounts are denied at once.
*Not defended.* Honest false positives. Expect a few.

### 4. Lookup failure

*Effect.* No file, unreadable file, address not in the file, a garbage address, the lookup raising.
*Defence.* Every one of these ends as `unknown`: allowed, not remembered, mentioned once. `core.country_of` catches any exception from the
lookup. A failed Jellyfin read ends the round with nothing done, and so does any other error: the loop reports it and tries again next round.
A state file that is missing starts fresh (the current sessions are recorded as allowed). A damaged one is moved aside as
`state.json.bad-<time>` with a loud warning and then also starts fresh; travel windows in it must be added again. One that cannot be read
at all skips the round. A session let through as `unknown` is remembered and never judged later. `doctor` warns about a missing or old (over 75 days) database.
*Not defended.* An attacker cannot be stopped while the database is missing; that is the design.

### 5. Time-window abuse

*Attack.* Getting a longer or earlier window than intended, or opening one for yourself.
*Defence.* Windows are created only by someone with shell access (`lombo-visa travel add`); there is no web page, no self-service and no
API, so a stolen Jellyfin token cannot open one (the private original had self-service guarded by a PIN; that was left out on purpose).
Windows start now (no back-dating), end at most `MAX_WINDOW_DAYS` later, are per account id (a renamed account keeps it, a new account
with the same name does not inherit it), can be limited to a country, and are refused for administrator accounts. The clock is read once
per command and passed in, never taken from a client. Revoking a window signs out the devices that signed in under it.
*Not defended.* Whoever can write the state file or run the CLI can grant anything. A window allows every sign-in from that account in
that time, including an attacker who has the password. Clock changes on the host move windows.

### 6. Log and alert injection

*Attack.* A user name, device name or client string with line breaks, control characters or `@everyone` is stored by Jellyfin and appears in a
log line or a webhook message, forging lines or pinging people.
*Defence.* `core.clean_text` replaces anything not printable (including line breaks, escape characters and Unicode line separators) with `?` and
cuts values to 40 characters before they reach any output; webhook bodies are built with `json.dumps`; `allowed_mentions` is empty so chat
mentions do nothing. Tests include a hostile name and device name.
Alert text (not log lines) also loses `< > @ [ ] | \`` so `<!channel>`, `@everyone` and `[text](link)` do nothing in Slack-style services.
*Not defended.* Visual tricks with printable characters (look-alike letters), and plain URLs that a chat service turns into links.

### 7. SSRF through the database download

*Attack.* Making the downloader fetch an internal address (metadata service, LAN device) or something huge.
*Defence.* The default URL is fixed (`download.db-ip.com`); no user, client or Jellyfin data ever enters a URL. `GEOIP_URL`, if set, is
operator-controlled and must start with `https://`. Redirects are followed only to https on the **same host and port**, at most three. Compressed and
decompressed sizes are capped (decompressed 64 MB, checked while the data streams in, so a gzip bomb is cut off long before it fills
memory), the whole download has a five-minute limit, and the result must be a real, usable country database
before it replaces anything. The URL for the Jellyfin and webhook calls is also operator-set.
*Not defended.* An operator who points `GEOIP_URL` (or the webhook) at an internal host gets exactly that. DNS rebinding and a hostile
resolver are not handled (the tool does not check which address the name resolves to). The code reading the response is the standard library's.

### 8. Tampered database file

*Attack.* Someone replaces the downloaded file (compromised download site, someone who can write the data folder, a man in the middle if https were broken).
*Effects.* A file that maps an attacker's range to a home country opens the fence for them; a file that maps everything to one foreign country denies everyone.
*Defence.* https with certificate checks (standard library defaults); size, structure and type checks before the file is moved in with one rename
(the old file stays on any failure); the reader treats the file as hostile (every read bounds-checked, no pointer chains, limits on depth, number of values and text bytes per
lookup, bytes values never returned, nothing but `MmdbError` allowed out; tests feed it malformed bytes, including a seeded fuzz run); the safety stop when many accounts are denied; the data folder is the only writable place in the container.
*Not defended.* There is **no signature or checksum check** of the download; this tool does not know of one that DB-IP publishes. Valid-looking
but wrong data is accepted. File permissions on the data folder are the operator's job.

### 9. Race between sign-in and sign-out

*Effects.* (a) A denied session works until the next round. (b) A session that signs in and out between two polls is never seen. (c) A
session Jellyfin has already dropped could be listed once more and be judged again (harmless: the sign-out is repeated, a 404 counts as success).
*Defence.* Short default interval (60 s, minimum 10 s), sessions that are still listed are retried each round if the sign-out failed.
*Not defended.* (a) and (b) are inherent to polling. Anything an attacker does in that minute (read the library, change that account's
settings, create an API key if the account is an administrator) is not undone.

### 10. Wrong sign-out and evasion through device ids

*Attack/effect.* `DELETE /Devices?id=` removes the device and its tokens. Device ids are chosen by clients. (a) A client that uses an id that already belongs to a
**different** account's allowed device would, if ended, sign that other person out. (b) The same rule lets someone with their own account and a
known device id sign in as another account with that id and escape the sign-out.
*Defence.* (a) A denied session whose device id belongs to another account's allowed device is **not** ended; it is reported ("not ended: its
device id also belongs to another account"). "Allowed" counts remembered devices **and** every other session in the same list that is not denied
(also `unknown` ones). When a travel window ends, a device id that another account also holds is forgotten, not signed out. A session with no
device id is reported, not ended.
*Not defended.* (b): the cost of (a). The alert is still sent, so someone must act on it. Whether Jellyfin really removes the tokens of every
user of a device id, and whether it signs out a Quick Connect session this way, is unconfirmed.

### 11. Avoiding the fence with a device that is already known, and a stolen token

A device that was allowed once (home, or under a window) is never judged again; an existing session keeps working anywhere. A stolen
token or a stolen device therefore works from anywhere. *Not defended*, by design (the owner's rule: roaming must stay easy).

### 12. Making the tool switch itself off

*Attack.* Opening many sessions from abroad so that the safety stop (see [how-it-works.md](how-it-works.md#what-happens-on-a-denial)) turns the sign-outs off.
*Defence.* The stop counts **accounts**, not sessions, so one stolen password cannot trip it. Someone with stolen passwords for more than
`MAX_BLOCKS_PER_ROUND` accounts can, and then the alert says so loudly.
*Not defended.* That case (it is the price of the stop).

### 13. Alert flooding and state growth

At most ten lines per round are sent; the same session is reported once (kept 7 days); remembered devices are capped at 5000.
A very large number of unknown or denied sessions still costs one lookup each per round.

## Audit findings (2026-11-21)

An unattended audit reproduced the cases below. Status after the fixes; each is covered by a test in `tests/test_hardening.py`,
`tests/test_geoip.py` or `tests/test_mmdb_fuzz.py` that fails on the audited code. "Fixed" means fixed and tested against the fake server and
generated files; nothing here has been seen against a real Jellyfin.

| # | Finding | Status |
|---|---|---|
| 1 | `HOME_COUNTRIES=UK` accepted, every home sign-in denied | **Fixed.** Codes are checked against an ISO 3166 list in `core.COUNTRY_CODES` (plus XK); `UK` is refused with "use GB". `travel add --country` uses the same list. `doctor` warns when none of your codes appears in the database. |
| 2 | Unexpected exceptions ended the process | **Fixed.** Each round of `run` (and every other command) catches everything, prints `[FAIL] unexpected ...` and goes on. The Jellyfin client and the webhook sender also catch `HTTPException` and `ValueError`; the reader catches `OverflowError`; `open_lookup` and `check_database` never raise. Deeply nested JSON is a handled error too. |
| 3 | Lost updates between `run`, `travel` and `doctor` | **Fixed.** `run`, `travel add` and `travel revoke` hold an exclusive `flock` (`state.json.lock`) for the whole load-change-save; `doctor` no longer saves; the temp file has a unique name and is synced before the rename. |
| 4 | A damaged state file was silently replaced | **Fixed.** A damaged or empty file is moved to `state.json.bad-<time>` with a warning on stderr; a read error skips the round; `doctor`, `plan` and `travel list` report a damaged file and never move it. |
| 5 | Sign-out by device id could end another person's session | **Fixed.** The clash check counts remembered devices and every non-denied session in the current list (including `unknown` ones); window expiry forgets a shared device instead of signing it out. Whether Jellyfin really removes other users' tokens for that id is still unconfirmed (checklist). |
| 6 | Hostile database or download could use unbounded memory | **Fixed.** File cap 64 MB; per-lookup budget of 10,000 values and 128 KB of text (pointers followed again count again); bytes values are skipped and never returned; the download is read in 64 KB chunks with the size checked as it streams. |
| 7 | An `unknown` session could be ended weeks later | **Fixed.** It is remembered (marked `unknown`) and never judged again. |
| 8 | `ZZ`, `XX`, `EU`, `AP`, `A1`, `A2`, `O1` treated as countries | **Fixed.** They are `unknown`. The codes in the real file are still to be checked (checklist). |
| 9 | Download details | **Fixed.** Five-minute overall deadline; a future file date counts as "unknown, update"; redirects compare host **and port**. |
| 10 | Chat markup in alerts | **Fixed** for alert text (see threat 6). |
| 11 | Misspelled setting names ignored | **Fixed.** An unknown `LOMBO_VISA_*` name stops the app at start-up with a close-name suggestion. |
| 12 | Jellyfin client followed redirects with the API key | **Fixed.** Redirects are refused with a message to set `LOMBO_VISA_JELLYFIN_URL` to the final address (this also makes it visible that sign-outs cannot work through a redirecting proxy). |
| 13 | Device cap (5000) makes later sessions judged again | **Not changed** (needs 5000 devices); written down here. |
| 14 | Smaller points | **Fixed:** README and docs now say that `run` in dry run still writes the state file and only `plan` writes nothing; exempt account ids match with or without dashes. The state file is synced before the rename. |

## Real-server run (2026-11-25)

`test/real-jellyfin/run.sh`: a throwaway Jellyfin 12.4, Caddy 2 with `examples/Caddyfile`, the real country database, a
visitor's address simulated through `X-Forwarded-For`. Passed: home and LAN sign-ins; abroad refused for password,
legacy, Quick Connect and 20 odd spellings of the path; no device or session left by a refusal; Jellyfin recorded the
visitor's address with the gateway in Known proxies; a travel window let one account in and its revoke signed the device
out (with `run` going); gateway stopped = sign-ins still work. It found two bugs, both fixed: the database download got
403, and the Caddy example did not fail open. Not covered: phone and TV apps, nginx, an independent reviewer.

**Not defended: swamping a fail-open proxy.** With 80 and 400 half-sent sign-ins hanging, foreign attempts got 502 or
503 and no token. That is two runs, not a proof: an attacker who can make the proxy fail to reach the gateway gets the
fail-open path. Keep a rate limit on the sign-in paths, or delete `handle_errors` to refuse instead.

## Summary: what is not defended

- Password guessing, and the first minute of any successful sign-in from abroad.
- Wrong forwarded addresses (Jellyfin settings), VPN exits in a home country.
- Any existing session or already-allowed device, stolen tokens.
- A tampered or poisoned database that still passes the structure checks.
- Someone who can edit the state file, run the CLI, or write the data folder.
- Evasion through another account's known device id (reported, not ended).
- Administrator accounts used from abroad are judged like everyone else and cannot get windows; if you exempt one, the tool does not protect it.

## Powers and secrets the app holds

- The Jellyfin API key (`LOMBO_VISA_JELLYFIN_API_KEY`). Jellyfin keys are not scoped: this key can do anything an administrator can. The app only
  calls `GET /System/Info/Public`, `GET /System/Info`, `GET /Sessions`, `GET /Devices`, `GET /Users` and `DELETE /Devices`. A compromise of the app (or of its `.env`) is a compromise of
  Jellyfin administration. Keep the file readable only by you; use a key only for this app so it can be revoked.
- The webhook URL (a secret in itself for most chat services).
- Session mode never sees passwords or tokens. The gateway sees them in passing (it forwards them to the configured upstream only) and never logs or stores them: [security.md](security.md).
- No passwords or tokens of users are ever read, stored or logged by session mode. The state file holds user ids and names, device ids and window dates, not addresses.
- The container runs as an unprivileged user (uid 999), with a read-only root file system and 128 MB of memory in the example compose file.
  The reader holds the whole database (a few MB, at most 64 MB) in memory and answers each lookup with a fixed value and text budget.
- The Jellyfin client never follows a redirect, so the API key is not sent to any other address.

## Checklist before the first release

The owner ticks these, in order. Nothing here has been done yet: every box needs a real server, the real download site or a person.

**Behaviour against a real server (throwaway Jellyfin first, then yours)**
- [ ] Run a throwaway Jellyfin (container named `lf-`, own port). Sign in from a browser; confirm `lombo-visa plan` shows the session with the right user, device and address fields (`UserId`, `UserName`, `DeviceId`, `DeviceName`, `Client`, `RemoteEndPoint` in `GET /Sessions`).
- [ ] Confirm what `RemoteEndPoint` looks like with a port, with IPv6, and behind a proxy with and without Known proxies set. Adjust `normalise_ip` if there is a format the tests do not cover.
- [ ] Confirm `GET /Sessions` lists idle sessions or how long they stay (affects how often a "new" session can reappear).
- [ ] Confirm `DELETE /Devices?id=` ends the session in a real client (web, Android, TV app), and whether it removes other users' tokens on the same device id (the clash rules assume it does).
- [ ] Confirm Jellyfin does not answer the API calls with a redirect on your setup (the client now refuses redirects and says so).
- [ ] Confirm Quick Connect and SSO sign-ins appear as sessions and are ended the same way.
- [ ] Confirm the API key's permissions are enough and nothing broader is needed.

**Database and download**
- [ ] Run `lombo-visa geoip update` against the real site; confirm the checks accept the real file (size, type name containing "country") and that lookups for a few known addresses give the right country (`1.1.1.1`, `8.8.8.8`, an IPv6 address).
- [ ] Confirm the real download involves no redirect to another host (if it does, the same-host rule will refuse it and needs a decision).
- [ ] Decide whether to add a checksum or signature check if DB-IP offers one.
- [ ] After `geoip update`, run `lombo-visa doctor`: no "home countries" warning. List the codes in the real file and confirm none besides the pseudo codes (`ZZ`, `XX`, `EU`, `AP`, `A1`, `A2`, `O1`) is not an ISO country.
- [ ] Check the reader against a real MaxMind-format file from another source (an official test file) to catch format cases the generated fixtures do not cover.

**Deployment**
- [ ] Phone on mobile data, no wifi: sign in, confirm the country in the log is the real one. Then with a VPN exit abroad: confirm `deny`.
- [ ] Reverse proxy: trusted proxies set to the proxy only; the proxy replaces `X-Forwarded-For`.
- [ ] Data folder and `.env` readable only by the owner; container read-only except `/data`.
- [ ] Dry run for at least a week on the real server; every `deny` and `unknown` line explained.
- [ ] Alerts-only stage for a week; confirm the webhook messages are readable and nobody else can post to the channel.
- [ ] Run `travel add` while the loop is running; confirm `travel list` shows the window afterwards (the lock).
- [ ] Start with no database, sign in, then run `geoip update`; confirm that session is not ended (`unknown` is remembered).
- [ ] A way back in: your own account has a window or is in `EXEMPT_USERS`; you have tested `BLOCK_SESSIONS=false` and `docker stop`.
- [ ] Decide what to do with administrator accounts (local network or VPN only; exempting them removes protection).

**Review**
- [ ] Someone other than the author reads `core.py`, `mmdb.py`, `geoip.py`, `jellyfin.py`, `state.py` and this file, and tries to break the reader with hostile files. (The 2026-11-21 audit was one pass of this by an unattended tool; its two reader findings are fixed and its fuzz run is now `tests/test_mmdb_fuzz.py`. Re-run the audit after these changes, and have a person read the diff.)
- [ ] CodeQL and the leak check pass on the published repository.

**Gateway mode and the audit log (see [security.md](security.md))**
- [ ] Behind a throwaway Jellyfin and a real Caddy and a real nginx: the example rules send the sign-in paths to the gateway and nothing else; a sign-in from the web client, a phone app and Quick Connect works; a refused one shows the refusal in the client.
- [ ] Confirm what Jellyfin's real Quick Connect answer holds (`User.Name`, `User.Id`, `AccessToken`) and that `POST /Sessions/Logout` with that token ends the session.
- [ ] Confirm the three sign-in paths are all that Jellyfin's clients use (list them from a few clients and versions).
- [ ] Someone other than the author reads `gateway.py` and `audit.py`, and tries odd paths, headers and slow clients against it.
- [ ] Confirm `X-Forwarded-For` as your proxy sets it ends with the visitor's address, and that Known proxies in Jellyfin lists the gateway.
- [ ] README banner about "not run against a real Jellyfin / no independent review" is removed or reworded only after the boxes above are ticked, and this file is updated with the results.
