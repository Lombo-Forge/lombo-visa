# Troubleshooting

## Contents

- [Start with doctor](#start-with-doctor)
- [Messages](#messages)
- [Nothing is ever denied](#nothing-is-ever-denied)
- [Someone was signed out and should not have been](#someone-was-signed-out-and-should-not-have-been)
- [Turning it off](#turning-it-off)

## Start with doctor

`lombo-visa doctor` checks, in order: Jellyfin answers, the key is accepted, the session list can be read, the country database opens,
the state file is writable. `[FAIL]` needs fixing; `[WARN]` is something to read.

## Messages

| Message | Meaning and fix |
|---|---|
| `[FAIL] settings: LOMBO_VISA_...` | A setting is missing or wrong; the message names it. |
| `API key accepted: /System/Info: HTTP 401` | The key is wrong or was revoked. Create a new one. |
| `Jellyfin reachable: ...` fails | The URL is wrong, or the container cannot reach Jellyfin (different Docker network). |
| `[WARN] country database ... missing or unusable` | Run `lombo-visa geoip update`. Until then every address is unknown and allowed. |
| `[FAIL] geoip: ... The old file, if any, was kept.` | The download failed or did not look like the database; the message says why. Nothing was replaced. |
| `[FAIL] state file ... Mount a writable folder at /data` | `chown 999 data` on the host folder, or set `LOMBO_VISA_STATE_FILE`. |
| `[WARN] ... is damaged ... Moved it to ...bad-...` | The state file could not be parsed. It was kept next to the new one; travel windows in it are gone, so add them again. Look at the old file to see what was lost. |
| `[FAIL] cannot read ... state` | The file exists but cannot be read (permissions, disk). The round was skipped and nothing was changed. |
| `[FAIL] LOMBO_VISA_X is not a setting. Did you mean ...` | A misspelled setting name; fix the name. |
| `[FAIL] ... HOME_COUNTRIES ... use GB` | Home countries must be ISO 3166 codes; the United Kingdom is `GB`. |
| `[WARN] home countries: none of ... appears in the database` | None of your codes occurs in the country file, so every public address would be judged foreign. Check the codes. |
| `[FAIL] unexpected ...Error` | An error nobody expected; the round was skipped and the loop goes on. Report it. |
| `No baseline yet` (from `plan`) | Run `lombo-visa run` once; it records the current sessions as allowed. |
| `all are on your own network` | See [Real addresses](setup.md#real-addresses). |
| `Sign-ins from abroad for more than N different accounts in one round. Nothing was ended` | The safety stop. Usually a wrong address source or a bad database; it can also be a real wave of stolen passwords. Check `plan`, then decide. |
| `Not ended: its device id also belongs to another account` | Two accounts used one device id. Ending it would sign the other one out, so it is only reported. |
| `Could not tell where ... signed in from` | Unknown country; the sign-in was let through. |
| `could not end the session` | Jellyfin refused the sign-out. It is retried every round while the session is listed. |

## Nothing is ever denied

Either everyone really is at home, or Jellyfin records private addresses (proxy not trusted), or the database is missing. `doctor` shows
all three. In dry run, `plan` prints every new session with the country it found.

## Someone was signed out and should not have been

Give them a window (`lombo-visa travel add NAME DAYS`), tell them to sign in again. If it was a country mix-up (the free database is
accurate at country level but not perfect, mobile networks sometimes resolve elsewhere), add the account to `LOMBO_VISA_EXEMPT_USERS` or the
country to `LOMBO_VISA_HOME_COUNTRIES`.

## Turning it off

Set `LOMBO_VISA_BLOCK_SESSIONS=false` and restart to stop sign-outs, or `docker stop lombo-visa` to stop everything. Jellyfin does not depend on it.
