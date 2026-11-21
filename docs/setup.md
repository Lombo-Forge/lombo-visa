# Setup

## Contents

- [What you need](#what-you-need)
- [Steps](#steps)
- [Real addresses](#real-addresses)
- [Going live in stages](#going-live-in-stages)
- [Gateway mode](#gateway-mode)
- [Audit log](#audit-log)
- [Keeping the database fresh](#keeping-the-database-fresh)
- [Webhooks](#webhooks)
- [Running without Docker](#running-without-docker)

## What you need

- A Jellyfin server and an API key (Dashboard > API Keys). The key is admin-level: it can list sessions, list users and sign devices out.
  Give LomboVisa its own key so you can revoke it alone.
- Internet access from the container to `download.db-ip.com` (once a month) and to Jellyfin.
- Optional: a webhook URL (Discord, Slack, or a bridge such as Apprise for ntfy, Gotify or email).

## Steps

1. Copy `.env.example` to `.env`. Set the Jellyfin URL and key and `LOMBO_VISA_HOME_COUNTRIES`.
2. `mkdir data && chown 999 data`, then `docker compose -f compose.example.yaml up -d`.
3. `... exec lombo-visa lombo-visa geoip update`, then `... exec lombo-visa lombo-visa doctor`. Fix every `[FAIL]` and read every `[WARN]`.
4. Let it run in dry-run mode for a week or two. `docker compose ... logs lombo-visa` shows one line per new sign-in with its verdict.
   Anything you did not expect (a friend on a VPN, a phone that resolves to the wrong country) is cheaper to find now.

## Real addresses

LomboVisa trusts the address Jellyfin records for a session (`RemoteEndPoint`). If your users reach Jellyfin through a reverse proxy,
Jellyfin only sees the proxy unless you tell it which proxies to trust (Dashboard > Networking > "Known proxies"; check the Jellyfin
documentation for your version) and the proxy sets `X-Forwarded-For`. Two ways to get this wrong:

- **Proxy not trusted:** every session shows the proxy's address, which is private, so everything counts as your own network and
  nothing is ever denied. `doctor` prints "all are on your own network" when every current session looks local. If you really have no
  remote users yet, ignore it.
- **Too many proxies trusted, or the proxy passes on the client's own `X-Forwarded-For`:** a visitor can write any address they like.
  Trust only the proxy's own address, and make the proxy replace the header rather than append to it.

Test with a phone on mobile data (no wifi): sign in, then run `lombo-visa plan` or read the log. The country shown should be the real one.

## Going live in stages

| Stage | Settings | What happens |
|---|---|---|
| 1. Look | `DRY_RUN=true` (default) | Lines are printed; nothing is sent or ended |
| 2. Tell me | `DRY_RUN=false`, `BLOCK_SESSIONS=false` | Same, plus a webhook message for each denied or unknown sign-in |
| 3. Block | `DRY_RUN=false`, `BLOCK_SESSIONS=true` | Denied sign-ins are signed out |

Make yourself a way back in before stage 3: a travel window for your own viewing account (`lombo-visa travel add`), or your account name in
`LOMBO_VISA_EXEMPT_USERS`, or simply set `LOMBO_VISA_BLOCK_SESSIONS=false` and restart. The emergency stop is always `docker stop lombo-visa`:
nothing else depends on it.

Keep administrator accounts to the local network or a VPN; they cannot get windows.

## Gateway mode

Optional. Session mode (everything above) reacts after Jellyfin accepted a sign-in; gateway mode refuses a sign-in from abroad before Jellyfin
sees it, but only for the sign-in paths your reverse proxy sends to it. Read [security.md](security.md) first.

1. Run it beside Jellyfin on a private network: [../examples/compose.gateway.yaml](../examples/compose.gateway.yaml). Do not publish its port.
2. Set `LOMBO_VISA_TRUSTED_PROXIES` to your proxy's address or network, and `LOMBO_VISA_GATEWAY_BIND=0.0.0.0:8099` inside the container network
   (the default `127.0.0.1:8099` is for running the gateway on the same host as the proxy, without Docker).
3. Make the proxy send every path that mentions "authenticate" to the gateway and everything else to Jellyfin
   ([../examples/Caddyfile](../examples/Caddyfile), [../examples/nginx.conf](../examples/nginx.conf)), and set `X-Forwarded-For` to the visitor's address
   (replace it, never append to what the visitor sent).
4. Add the gateway's address to Jellyfin's Known proxies, as for your proxy, so the sessions it records show real addresses.
5. `lombo-visa doctor` shows the mode, warns about a bind that is not loopback, an empty or very wide trusted-proxy list, and an
   upstream that does not answer.
6. Stages: `GATEWAY_MODE=log` (the default) decides and records and always forwards. Read the log (or the audit log) for a week. Then
   `LOMBO_VISA_DRY_RUN=false` **and** `LOMBO_VISA_GATEWAY_MODE=enforce`. With dry run on, `enforce` only logs, and the gateway says so at start.
7. Keep your proxy's rate limit on the sign-in paths and Jellyfin's lockout on. A way back in is the same as for session mode: a travel window for
   your own account, `EXEMPT_USERS`, or `GATEWAY_MODE=log`/`off` and a restart of the gateway.

**Check it (needs a real server; not done by the author).** From a phone on mobile data, sign in: it should work and the log should show your
country. Then from a VPN exit abroad with `enforce`: the app should show a refusal and the audit log a `deny`. `curl -i -X POST
https://jellyfin.example.com/Users/AuthenticateByName` should reach the gateway (a 400 or 401 from Jellyfin or a 403 from the gateway), and
`curl -i https://jellyfin.example.com/Users/Authenticate%252542yName` should reach Jellyfin directly or the gateway's 404, never a sign-in.

## Audit log

Set `LOMBO_VISA_AUDIT_FILE=/data/audit.jsonl` to keep one line per decision (session and gateway mode). `lombo-visa audit --last 20 --user alice` prints
them. The addresses are stored only as keyed hashes; the key (`audit.key`) is made beside the file, mode 0600.

## Keeping the database fresh

DB-IP publishes a new file each month. Run `lombo-visa geoip update` monthly, for example from cron on the host:
`0 4 2 * * docker exec lombo-visa lombo-visa geoip update`. It does nothing if the file is under 20 days old. The running loop reopens the
file every round, so no restart is needed. After 75 days `doctor` warns; with no file every address is unknown and allowed. Anyone who signs in before the first download stays allowed (they are remembered as unknown, never judged later), so run `geoip update` before the first `run` if you can.

## Webhooks

The message is posted as JSON with the text under both `content` (Discord) and `text` (Slack and compatible), with mentions switched off.
Send to ntfy, Gotify or email through a bridge. No service is built in.

## Running without Docker

```sh
pip install .
export LOMBO_VISA_JELLYFIN_URL=http://localhost:8096 ...   # see .env.example
export LOMBO_VISA_STATE_FILE=$HOME/.lombo-visa/state.json LOMBO_VISA_GEOIP_DB=$HOME/.lombo-visa/country.mmdb
lombo-visa geoip update
lombo-visa doctor
lombo-visa run --loop
```
