# Build LomboVisa into your own app

LomboVisa decides who may sign in. Your dashboard, account page or sign-in help page only calls it, so none of the
rules have to live in your code. Everything here is optional and off until you set it.

## Contents

- [What you can connect](#what-you-can-connect)
- [Set up](#set-up)
- [Admin dashboard](#admin-dashboard)
- [Self-service trips](#self-service-trips)
- [Locked-out page](#locked-out-page)
- [Replace the defaults](#replace-the-defaults)
- [Without a dashboard](#without-a-dashboard)

## What you can connect

| Your piece | Calls | Turned on by |
|---|---|---|
| Admin dashboard: list, allow, revoke trips | `/v1/admin/windows` on `lombo-visa api` | `LOMBO_VISA_ADMIN_TOKEN` |
| Account page: "I'm travelling" | `/v1/travel` on `lombo-visa api` | `LOMBO_VISA_API_TOKEN` |
| Locked-out person: "ask to be let in" | `/visa/ask` on `lombo-visa gateway` | `LOMBO_VISA_ASK_PAGE=true` |

## Set up

1. Run the processes you need from one image, sharing one data folder and one `.env`: `gateway` (refuses before the
   sign-in exists), `run --loop` (**needed with the gateway too**: it signs out the devices of trips that have ended),
   and `api` (only for a dashboard or account page). See [examples/compose.gateway.yaml](../examples/compose.gateway.yaml).
2. Send Jellyfin's sign-in paths to the gateway with your reverse proxy ([examples/Caddyfile](../examples/Caddyfile)).
3. In Jellyfin, add the gateway and the proxy to **Dashboard > Networking > Known proxies** and restart Jellyfin.
   Without this Jellyfin records the gateway's address, and session mode takes every sign-in for a local one.
4. Keep the API port on a private network that only your app's **server** reaches. Never call it from a browser: the
   token would be public, and there are no CORS headers on purpose.
5. Run `lombo-visa doctor`, then follow the stages in [setup.md](setup.md) (log first, enforce later).

## Admin dashboard

> **Accepted risk: you take this on when you set the token.** Whoever holds `LOMBO_VISA_ADMIN_TOKEN` can let any
> non-administrator account sign in from anywhere. **It must never be reachable from the internet**: keep the API port
> on a private network, never publish it, never send the token to a browser, and protect your dashboard's own sign-in at
> least as well as Jellyfin's. If it leaks, change it and restart `api`, then check `lombo-visa travel list`.

Send `Authorization: Bearer $LOMBO_VISA_ADMIN_TOKEN`. It cannot allow Jellyfin administrator accounts.

| Request | Body | Answer |
|---|---|---|
| `GET /v1/admin/windows` | none | `{"ok": true, "max_days": 30, "windows": [{"id": 1, "user_id": "...", "name": "alex", "country": "JP", "starts": 1800000000, "ends": 1800432000, "self": false}], "last7": {"gateway": {"allow-home": 12, "deny": 2}}}` |
| `POST /v1/admin/windows` | `{"user_id": "<32 hex>", "days": 5, "country": "JP"}` (`country` optional) | `{"ok": true, "message": "...", "id": 1}` |
| `DELETE /v1/admin/windows/1` | none | `{"ok": true, "message": "window revoked"}` |

`last7` counts the last seven days' decisions from the audit log, by mode (`gateway`, `session`, `api`); it is `null`
without `LOMBO_VISA_AUDIT_FILE`. The `api` container needs to see that file.

A refusal is `{"ok": false, "message": "..."}` with 4xx: show the message. `502` means Jellyfin or the state file
could not be read and nothing changed. Times are Unix seconds. Admin calls write even while `DRY_RUN=true`, as the
`travel` commands do. A revoked window's devices are signed out on the next `run` round.

## Self-service trips

Your server signs the person in however it already does, then calls with `LOMBO_VISA_API_TOKEN`. **LomboVisa believes
the `user_id` you send**, so take it from your own session, never from the browser's request.

| Request | Body | Answer |
|---|---|---|
| `GET /v1/travel/{user_id}` | none | `{"ok": true, "active": null, "next_allowed": null, "max_days": 14}` |
| `POST /v1/travel` | `{"user_id": "<32 hex>", "days": 7}` | `{"ok": true, "message": "...", "active": {"id": 2, "ends": 1800000000}}` |

With `LOMBO_VISA_SELF_REQUIRE_PASSWORD=true` the body also needs `"password"`: the person's Jellyfin password (or
PIN). LomboVisa signs in with it and straight out again, and never stores or logs it. Two wrong ones lock that account's
requests for an hour, so a stolen login token cannot open a trip by guessing. Use this unless your page already checks
something of its own.

## Locked-out page

With `LOMBO_VISA_ASK_PAGE=true` and `LOMBO_VISA_WEBHOOK_URL` set, `https://your.server/visa/ask` shows a one-field
form, and a refused sign-in's answer carries `"away": "/visa/ask"`. The webhook gets the typed name and the country,
never the address. Limits: 3 tries an hour per address, 20 in all, one alert per name a day. The answer is the same
whatever was typed, so the page does not tell anyone which accounts exist. Nothing is sent while `DRY_RUN=true`.

Put your reverse proxy's rate limit on this path too. Jellyfin's apps show their own sign-in error, not this link, so
tell people the address beforehand.

## Replace the defaults

| Default | Your own |
|---|---|
| Refusal text | `LOMBO_VISA_GATEWAY_MESSAGE="Ask Sam to allow your trip."` |
| Built-in page | `LOMBO_VISA_ASK_PAGE_FILE=/data/away.html`, with a form that posts `name` to `/visa/ask`; or send `{"name": "..."}` as JSON from your script |
| The whole page and alert | Leave `ASK_PAGE` off and serve your own from your app |
| Password check | Leave `SELF_REQUIRE_PASSWORD` off and run your own check (two-factor, e-mail code) before calling the API |
| Alerts | Any webhook that takes JSON (`content` and `text` are both sent) |

## Without a dashboard

The same jobs from a shell: `lombo-visa travel add alex 5 --country JP`, `travel list`, `travel revoke 1`, and
`lombo-visa audit` to see who was let in or refused.
