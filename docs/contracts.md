# Contracts other apps may rely on

## Contents

- [The travel API](#the-travel-api)
- [Every request](#every-request)
- [Read a person's window: `GET /v1/travel/{user_id}`](#read-a-persons-window)
- [Open a window: `POST /v1/travel`](#open-a-window)
- [Status codes](#status-codes)
- [What the caller must do](#what-the-caller-must-do)
- [What a change means](#what-a-change-means)

Everything below is a promise to other apps (a page service such as the Shelf plugin). If you change it, change this file in the
same pull request. Nothing here was tried against a real client or a real Jellyfin.

## The travel API

`lombo-visa api` is a small token-guarded HTTP server. It is **off** unless `LOMBO_VISA_API_TOKEN` is set. It exists so a signed-in
person can open their own travel window from a page, without an admin. Browsers never call it: the page service does, from its
own server side, with the person's id taken from the person it has verified.

| Setting | Default | Meaning |
|---|---|---|
| `LOMBO_VISA_API_TOKEN` | empty (API off) | At least 24 visible ASCII characters, no spaces. `openssl rand -hex 32` makes a good one. |
| `LOMBO_VISA_API_BIND` | `127.0.0.1:8097` | An IP address and a port (`[::1]:8097` for IPv6). |
| `LOMBO_VISA_SELF_MAX_DAYS` | `14` (or `MAX_WINDOW_DAYS` when that is smaller) | Longest window a person may open, 1 to `LOMBO_VISA_MAX_WINDOW_DAYS`. |
| `LOMBO_VISA_SELF_COOLDOWN_DAYS` | `30` | Days after a self-opened window **ends** before the same person may open another. 0 to 365; 0 = no cool-down. |
| `LOMBO_VISA_SELF_COOLDOWN_FROM` | `end` | `end`: the wait starts when that window ends. `start`: when it was made. |
| `LOMBO_VISA_DRY_RUN` | `true` | While true, `POST` checks every rule and answers, but writes nothing. |

## Every request

- Header `Authorization: Bearer <token>`. Missing or wrong: `401` with `{"ok": false, "message": "unauthorized"}` and nothing
  else. After 10 failed tries in 60 seconds from one address: `429` with `Retry-After: 60`, even for the right token until the minute is up.
- Every answer is JSON with `Cache-Control: no-store`. There are no CORS headers and no cookies.
- An unknown path is `404`, the wrong method on a known path is `405` with an `Allow` header. Both are answered only to a caller with a good token.
- `POST` bodies: `Content-Type: application/json`, a `Content-Length`, at most 4096 bytes, strict JSON (exactly the two keys below, no
  duplicate keys, no `NaN` or `Infinity`).
- Error messages are plain and carry no paths, tokens or Jellyfin details.
- `user_id` is a Jellyfin user id: 32 hex digits. Dashes are accepted and removed, capitals are lowered. Anything else is `400`.

## Read a person's window

`GET /v1/travel/{user_id}`

```json
{"ok": true, "active": {"id": 3, "ends": 1800000000}, "next_allowed": 1802592000, "max_days": 14}
```

| Field | Meaning |
|---|---|
| `active` | A window that is open now or starts later, of any kind (self-opened or an admin's). The one that ends last if there are several. `null` when there is none. Revoked windows never count. `ends` is Unix time in seconds. |
| `next_allowed` | `null` when the person may open a window now (as far as the cool-down goes). Otherwise the Unix time when the cool-down ends: the end of their last self-opened window (or its revoke time, if earlier; its start with `SELF_COOLDOWN_FROM=start`) plus `SELF_COOLDOWN_DAYS`. While that window is still open the time is already given. |
| `max_days` | `LOMBO_VISA_SELF_MAX_DAYS`. |

This call reads only Visa's own state file; it does **not** ask Jellyfin. A user id Visa has never seen answers `200` with both
fields `null`. It does not say whether the user exists, is an administrator or would be refused: only `POST` checks that.

## Open a window

`POST /v1/travel` with exactly `{"user_id": "<id>", "days": 7}`; `days` is a whole number (not a string, a decimal or a boolean).

The window starts now, lasts `days` days, allows any foreign country and is stored with `"self": true`. It is the same window
`lombo-visa travel add NAME DAYS` makes. Rules, each refused with `422` and `{"ok": false, "message": "..."}` (plain text meant for the person):

| Rule | Message says |
|---|---|
| `days` is not 1 to `SELF_MAX_DAYS` | the allowed range |
| Jellyfin has no such user | that the account was not found |
| The user is an administrator | to use the admin tools |
| The person already has an open or upcoming window | that one is already open or coming up |
| The person is inside a cool-down | the date (UTC) from which they may set up the next trip |

The rules are checked in that order. Jellyfin is asked who the person is on every `POST` (never cached).

Success (`200`):

```json
{"ok": true, "message": "travel window opened for 7 day(s)", "active": {"id": 3, "ends": 1800000000}}
```

With `LOMBO_VISA_DRY_RUN=true` the rules still run (so a refusal shows up), and a request that would have succeeded answers
`200` with `{"ok": true, "message": "dry run: nothing was changed", "active": null}` and writes nothing. If `LOMBO_VISA_AUDIT_FILE` is set, both leave a
line in the audit log: `mode` `api`, decision `self-window` (written) or `would-self-window` (dry run), the user's name, no address and never the token.

## Status codes

| Status | Meaning |
|---|---|
| 200 | Done, or a read. |
| 400 | The request itself was wrong: not strict JSON, wrong keys or types, a malformed `user_id`. |
| 401 / 429 | No or wrong token / too many failed tries. |
| 404 / 405 | Unknown path / wrong method (with `Allow`). |
| 411 / 413 / 415 | No `Content-Length` / body over 4096 bytes / not `application/json`. |
| 422 | A rule above refused the request. The message says why. |
| 502 | Jellyfin could not be asked, or the state file could not be read or written. Nothing was changed. The message is generic; Visa's log has the detail. |
| 500 | An unexpected error. Generic message. Nothing is promised about what was written. |

## What the caller must do

- Take `user_id` from the verified signed-in person (for the Shelf plugin: the user Jellyfin has just authenticated), **never** from
  anything the browser sends in a body, query or header. Visa trusts whoever holds the token to name the right person.
- Keep the token on the server side. Never put it in a page, a script a browser downloads, or a log.
- Show the `422` message to the person as it is; it is written for them.
- Treat `502` as "try again later", not as a refusal.

## What a change means

- **Same `/v1`:** a new, optional field in an answer, or a new optional setting. A caller must ignore fields it does not know.
- **`/v2`:** a field removed or renamed, a changed meaning or type, a new required request key, or a changed rule order. `/v1` is then
  kept beside it for at least one release where that is possible, and the release notes say so.
