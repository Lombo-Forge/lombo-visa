# Security notes: the gateway and the audit log

Short notes on the two parts that listen on a port or write a file. The wider threat model of the session mode is in
[security-review.md](security-review.md). **None of this has been run against a real Jellyfin or a real reverse proxy,
and none of it has had an independent review.** Every statement below is about what the code does, checked only
against a fake server in the tests.

## Contents

- [Gateway](#gateway)
- [Audit log](#audit-log)
- [What is not verified](#what-is-not-verified)

## Gateway

`lombo-visa gateway` is a small HTTP server (standard library only) that a reverse proxy puts in front of Jellyfin's
sign-in paths. It is optional: nothing starts it unless you run that command.

**What it trusts**

- The reverse proxy, but only an address you list in `LOMBO_VISA_TRUSTED_PROXIES`. The visitor's address is read from
  `X-Forwarded-For` only when the connection comes from such an address; then the rightmost hop that is not itself a
  trusted proxy is the visitor, so extra addresses a visitor writes on the left are ignored. From any other
  connection the header is ignored (said once in the log) and the connecting address is used.
- The country database and the travel windows in the state file, like the session mode.
- The upstream address you configured (`LOMBO_VISA_GATEWAY_UPSTREAM`, default your Jellyfin address). It is the only place
  a request, a password or a token is ever sent. Redirects are never followed.
- Jellyfin's answer to a Quick Connect sign-in, to learn who signed in.
- Everything from the visitor is untrusted: path, query, headers, body and the typed user name.

**What it refuses**

- Every path that is not exactly a sign-in path gets a 404 and goes nowhere: it is not a general proxy. So does every
  method but POST. The path is decoded once, compared in lower case, segment by segment; a second layer of encoding
  (`%25`), an encoded slash or backslash, control characters, non-ASCII, empty and dot segments, `;` parameters and a
  full URL in the request line are all "not a sign-in".
- A body over 64 KiB (413), a chunked body or a missing or bad `Content-Length` (400), more than 64 headers or 16 KiB of
  headers (431), a client that is slow (5 s per read, 30 s for the whole request), more than 64 connections at once (503),
  an answer from Jellyfin over 8 MiB (502), an upstream that does not answer in 10 s (502). One request per connection.
- With `LOMBO_VISA_GATEWAY_MODE=enforce` **and** `DRY_RUN=false`: a new sign-in from a known foreign country with no
  travel window gets a 403 and never reaches Jellyfin. A password sign-in from abroad whose body has no readable user
  name, or two different values under `username` keys that differ only in letter case (Jellyfin does not tell the keys
  apart by case, so this is a way to hide the name), also gets the 403, because nothing can then be matched to a window.
- Quick Connect names nobody in the request. From abroad it is forwarded, and when Jellyfin's answer names a person
  without a window, the new session is ended again (`POST /Sessions/Logout` to the upstream with the token Jellyfin
  just issued) and a 403 is returned instead of the token.

**Fail open.** No database, an unknown or unreadable address, an address that cannot be parsed, unreadable travel
windows, Jellyfin not answering the user lookup (the typed name is matched against the windows instead), mode `off`
or `log`, and `DRY_RUN=true` (which makes `enforce` act as `log`) all let the sign-in through. When Jellyfin itself
is unreachable the answer is a 502: there is nothing to fail open to.

**What it never logs or stores.** Request bodies, passwords, tokens, the `Authorization` and `X-Emby-Authorization`
headers, the path and query, and addresses. A log line has the decision, the kind of sign-in, the country and the user
name (cleaned of control characters and cut to 40 characters). Python's own request logging is switched off. The
audit log (below) holds a keyed hash of the address, never the address.

**Not defended**

- A proxy rule that misses a spelling of a sign-in path sends that request straight to Jellyfin. The examples send every path containing
  "authenticate" (any case) to the gateway, and the gateway refuses what is not a sign-in, to make that hard.
- A proxy that is not in `TRUSTED_PROXIES`: then every client is the proxy, which is your own network, so nothing is
  refused. `doctor` warns when the list is empty. Too wide a list (`0.0.0.0/0`) lets anyone claim any address; `doctor` warns.
- A gateway that anyone can reach directly on a non-loopback bind: they could present a forged `X-Forwarded-For` only if their
  own address is trusted, but they can reach Jellyfin's sign-in through it and fill its log. Keep the port private.
- Password guessing: the gateway decides by country only. Keep your proxy's rate limits and Jellyfin's lockout on.
- The gateway is a single point of failure for sign-ins when the proxy sends them there: if it stops, sign-ins fail
  (the proxy answers 502) until it is back. Session mode is unaffected. The `/Sessions/Logout` step of Quick Connect, and what
  Jellyfin's real answers look like, are unconfirmed.
- Whoever can write the state file or the data folder can grant windows or change the database.

## Audit log

`LOMBO_VISA_AUDIT_FILE` (empty = off). One JSON line per decision from the session mode and the gateway:
`ts`, `mode`, `decision`, `country`, `user`, `ip_hash`.

- **Stores:** the time (whole seconds), which mode, the verdict, the country code, the user name (cleaned, cut to 40
  characters) and the first 16 hex characters of an HMAC-SHA256 of the address.
- **Never stores:** the address itself, passwords, tokens, device names, request paths or bodies.
- **The key:** 32 random bytes in `audit.key` in the same folder as the log, made on first use with mode 0600. The log and its
  one old file `.1` are mode 0600 too. The same address always gives the same hash while the key stays, which is what makes "was
  this the same visitor?" answerable; without the key a hash cannot be turned back into an address except by trying every
  address (only 2^32 of them for IPv4: **anyone who has both the key and the log can recover the addresses**, so protect them
  as you protect the state file). Deleting the key and the log forgets everything.
- **Size:** at 5 MiB the file moves to `<file>.1` (replacing the old one); at most about 10 MiB is kept.
- **Fails quietly:** a log that cannot be written (unwritable folder, damaged key) prints one warning and never changes a decision.
- `lombo-visa audit` reads it; `doctor` checks the folder is writable and the key looks right, and makes no key itself.

## What is not verified

- No real Jellyfin: whether the three sign-in paths are all of them for every client and version, what a Quick Connect
  answer and `/Sessions/Logout` do in practice, and that the same Jellyfin treats headers and paths the way the
  gateway assumes.
- No real reverse proxy: the Caddy and nginx examples have not been run.
- No load test and no independent review.
