---
name: troubleshoot
description: Diagnose LomboVisa problems from doctor and plan output and the logs. Use when the app fails to start, doctor shows FAIL or WARN, nothing is ever denied, someone was signed out wrongly, alerts do not arrive, or the country looks wrong.
---

# Troubleshoot LomboVisa

Start with `lombo-visa doctor`; each line says what failed. `docs/troubleshooting.md` has the table of messages and fixes; read it rather than
guessing. Then `lombo-visa plan` prints every new sign-in with its country and verdict without changing anything.

Rules of thumb:

- "Nothing is ever denied": check, in order, `DRY_RUN` (true means nothing is sent or ended), the database (`doctor` warns when it is missing),
  and the addresses (`doctor` says when every session looks local: Jellyfin is seeing a proxy, see `docs/setup.md`).
- A wrong sign-out: give the person a window (`travel add`), or add the account or country to the settings. Do not widen rules to hide one case.
  Remember the free database is only country-accurate, mobile networks and VPNs can resolve elsewhere.
- The safety stop ("different accounts in one round, nothing was ended") means: look at `plan` before anything else. A bad database or wrong address source
  looks exactly like this; so can a real wave of stolen passwords.
- The state file (`LOMBO_VISA_STATE_FILE`) holds allowed devices and travel windows. Deleting it re-records the current sessions as allowed and
  forgets all windows; tell the user before suggesting it.
- Never ask the user to turn on `BLOCK_SESSIONS` to "see if it works"; dry run shows the same decisions.
- Emergency stop: `BLOCK_SESSIONS=false` and restart, or stop the container. Jellyfin does not depend on LomboVisa.
- When the user reports a bug, ask for `doctor` and `plan` output with names, addresses and keys removed.
