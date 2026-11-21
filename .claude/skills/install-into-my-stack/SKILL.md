---
name: install-into-my-stack
description: Help a user install LomboVisa next to their own Jellyfin server, set the home countries, fetch the country database and verify it with doctor. Use when someone asks to set it up, install it, or add it to their compose file or server.
---

# Install LomboVisa into someone's stack

Say first, in one line: LomboVisa has not been run against a real Jellyfin and has not had an independent review (see the README); it starts in dry run
and changes nothing until they say so.

Ask, then write the answers into `.env` (never commit it):

1. Jellyfin address as the container sees it (`LOMBO_VISA_JELLYFIN_URL`) and whether Jellyfin is behind a reverse proxy.
2. Which countries people normally sign in from (`LOMBO_VISA_HOME_COUNTRIES`, two-letter codes). There is no default on purpose.
3. A Jellyfin API key made for this app (Dashboard > API Keys). It is admin-level; say so.
4. Optional webhook URL (Discord, Slack or a bridge).
5. Which accounts are administrators, and whether the owner wants a way back in (`LOMBO_VISA_EXEMPT_USERS` or a travel window).

Then:

- Add the service from `compose.example.yaml`; `mkdir data && chown 999 data`; `docker compose up -d`.
- `lombo-visa geoip update`, then `lombo-visa doctor`. Fix every `[FAIL]`; read every `[WARN]`. If doctor says every session is on the own network and
  they have remote users, fix Jellyfin's known-proxy setting first (`docs/setup.md`, "Real addresses"); until then the tool does nothing.
- Leave `DRY_RUN=true` for a week or two and read the log together. Move to alerts (`DRY_RUN=false`) and only later to sign-outs
  (`BLOCK_SESSIONS=true`), each with the owner's explicit yes. Follow `docs/security-review.md`, "Checklist before the first release", for what to confirm.
- Show them `lombo-visa travel add NAME DAYS [--country CC]` and `lombo-visa geoip update` for a monthly cron job.

Never put the real key, address or webhook into a file that is committed or into an issue.
