<p align="center"><img src="assets/banner.svg" alt="LomboVisa" width="100%"></p>

<p align="center">
  <a href="LICENSE"><img alt="License: GPL-3.0" src="https://img.shields.io/github/license/Lombo-Forge/lombo-visa"></a>
  <a href="https://github.com/Lombo-Forge/lombo-visa/actions/workflows/ci.yml"><img alt="CI" src="https://img.shields.io/github/actions/workflow/status/Lombo-Forge/lombo-visa/ci.yml?branch=main&label=CI"></a>
  <a href="https://github.com/Lombo-Forge/lombo-visa/actions/workflows/codeql.yml"><img alt="CodeQL" src="https://img.shields.io/github/actions/workflow/status/Lombo-Forge/lombo-visa/codeql.yml?branch=main&label=CodeQL"></a>
  <a href="https://github.com/Lombo-Forge/lombo-visa/releases"><img alt="Release" src="https://img.shields.io/github/v/release/Lombo-Forge/lombo-visa?include_prereleases"></a>
  <img alt="Python 3.12+" src="https://img.shields.io/badge/python-3.12%2B-blue">
  <a href="https://ko-fi.com/datlombardo"><img alt="Support on Ko-fi" src="https://img.shields.io/badge/Ko--fi-support-FF5E5B?logo=ko-fi&logoColor=white"></a>
</p>

# LomboVisa

Watches a Jellyfin server for new sign-ins and, if one comes from a country you did not list, tells you and (when you turn that on) signs that device out again. People who are travelling get a "travel window" first. If anything is unclear, it lets the sign-in through.

> **Status: not release-ready.** LomboVisa has **not been run against a real Jellyfin server** (only against a fake one in the tests) and has **not had an independent security review**. [docs/security-review.md](docs/security-review.md) is the author's own threat model and a checklist to tick before trusting it. Until then keep it in dry-run mode.

```text
$ lombo-visa plan
alice on Pixel 8 (Jellyfin Android) from DE: deny -> would end it
bob on Living room TV (Jellyfin Android TV) from US: allow-home
```

## Contents

- [What it does and does not do](#what-it-does-and-does-not-do)
- [Install in a minute](#install-in-a-minute)
- [Commands](#commands)
- [Settings](#settings)
- [Travel windows](#travel-windows)
- [FAQ](#faq)
- [Credits](#credits)
- [Contributing and licence](#contributing-and-licence)

## What it does and does not do

It polls Jellyfin's list of signed-in sessions (`GET /Sessions`) every minute. A session it has not seen before is a **new sign-in**. For each one it looks up the country of the address Jellyfin recorded:

| Where it is from | Result |
|---|---|
| Your own network, or a country in `HOME_COUNTRIES` | allowed, remembered |
| A foreign country, and the person has a travel window for it | allowed, remembered |
| A foreign country, no window | **denied**: logged; with `DRY_RUN=false` an alert is sent; with `BLOCK_SESSIONS=true` the device is signed out |
| Cannot tell (no database, address not in it, odd address, or a code such as `ZZ` that is not a country) | allowed, remembered (so it is never ended later), and mentioned |

Three stages, so you can trust it step by step: **dry run** (default: prints what it would do; sends and ends nothing, but `run` still records sessions in its state file, only `plan` writes nothing), **alerts only** (`DRY_RUN=false`), **blocking** (`DRY_RUN=false` and `BLOCK_SESSIONS=true`).

**Important limits, stated plainly:**

- It reacts **after** Jellyfin accepted the sign-in. The new session lives until the next round (60 seconds by default). It does not stop password guessing; it limits what a correct password used from abroad can do. Keep your reverse proxy's rate limits and Jellyfin's own lockout on.
- Sessions that already exist keep working anywhere. A device that was allowed once is never judged again.
- It sees the address **Jellyfin** records. Behind a reverse proxy Jellyfin must be told to trust the proxy, or every address looks like your own network. `doctor` warns about that.
- VPN users appear in the country of their VPN exit.

What it **never** does: delete or change users, passwords or policies; touch libraries or media; end a session that is already known; end anything when the country is unknown, when the database is missing, or when more than `MAX_BLOCKS_PER_ROUND` different accounts are denied in one round (a safety stop for setup mistakes); block an account in `EXEMPT_USERS`; sit in the path of your traffic (if it stops, Jellyfin carries on as before).

More in [docs/how-it-works.md](docs/how-it-works.md) and [docs/security-review.md](docs/security-review.md).

## Install in a minute

1. In Jellyfin, open Dashboard > API Keys and create a key for LomboVisa.
2. Copy `.env.example` to `.env`; fill in the URL, the key and `LOMBO_VISA_HOME_COUNTRIES` (for example `US,CA`).
3. Make the data folder writable for the container user and start it:
   ```sh
   mkdir data && chown 999 data
   docker compose -f compose.example.yaml up -d
   docker compose -f compose.example.yaml exec lombo-visa lombo-visa geoip update
   docker compose -f compose.example.yaml exec lombo-visa lombo-visa doctor
   ```
4. Read what it decides for a few days: `docker compose -f compose.example.yaml logs lombo-visa`, or `lombo-visa plan`.
5. When the decisions look right, set `LOMBO_VISA_DRY_RUN=false` (alerts), later `LOMBO_VISA_BLOCK_SESSIONS=true` (sign-outs).

Setup details, including making Jellyfin see real client addresses behind a proxy: [docs/setup.md](docs/setup.md).

## Commands

| Command | What it does |
|---|---|
| `lombo-visa doctor` | Checks settings, Jellyfin, the key, the session list, the country database and the state file. Exit code 1 if something fails. |
| `lombo-visa plan` | Shows what the next round would do. Never changes anything. |
| `lombo-visa run [--loop]` | Does it. `--loop` repeats every `LOMBO_VISA_INTERVAL_SECONDS`. The first run only records the sessions that exist. |
| `lombo-visa travel add NAME DAYS [--country CC]` | Let a person sign in from abroad, starting now. |
| `lombo-visa travel list` / `revoke ID` | Show or end windows. |
| `lombo-visa geoip update [--force]` | Download the country database. Monthly is enough. |

## Settings

Everything is an environment variable with the `LOMBO_VISA_` prefix, listed with defaults in [.env.example](.env.example). A bad value, or a name that is not a setting (a typo such as `EXEMPT_USER`), stops the app at start-up and names the variable.

## Travel windows

```sh
lombo-visa travel add alice 14 --country DE   # alice may sign in from Germany for 14 days
lombo-visa travel list
lombo-visa travel revoke 1                    # ends it; devices that signed in under it are signed out on the next round
```

Windows are per Jellyfin account (a renamed account keeps its window), start now, last at most `MAX_WINDOW_DAYS`, and are never made for administrator accounts. When a window ends or is revoked, the devices that signed in abroad under it are signed out (with blocking on).

## FAQ

**Is it safe to try?** Dry run is the default and sends and ends nothing (it does write its state file; `plan` writes nothing). Even so, read the [security review](docs/security-review.md) first; the tool has never met a real server.

**Why a country database file, and where does it come from?** The free "IP to Country Lite" database from DB-IP is downloaded when you run `lombo-visa geoip update`. It is not part of this repository.

**What if the database is missing or old?** Every address becomes "unknown" and is let through. `doctor` warns.

**Why not a gateway in front of Jellyfin?** A gateway can refuse a sign-in before Jellyfin sees the password, but then it is a single point of failure on the path of every login. This app stays beside Jellyfin so that a crash cannot lock anyone out. The price is the delay described above.

**Does it work with Quick Connect and SSO?** It judges the session, whatever way it was created.

## Credits

IP geolocation by [DB-IP.com](https://db-ip.com) (the free "IP to Country Lite" database, licence CC BY 4.0). More in [NOTICE.md](NOTICE.md).

## Contributing and licence

Contribution, security and conduct rules come from the [Lombo-Forge organisation](https://github.com/Lombo-Forge/.github). Licensed under [GPL-3.0](LICENSE). Not affiliated with or endorsed by the Jellyfin project.
