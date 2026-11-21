# Setup

## Contents

- [What you need](#what-you-need)
- [Steps](#steps)
- [Real addresses](#real-addresses)
- [Going live in stages](#going-live-in-stages)
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
