# AGENTS.md

Notes for an AI coding agent (or a person) working in this repo. `CLAUDE.md` pulls this file in.

## Contents

- [What this is](#what-this-is)
- [Layout](#layout)
- [Rules](#rules)
- [Test](#test)
- [Working efficiently](#working-efficiently)
- [Skills](#skills-claudeskills)

## What this is

LomboVisa is a small Python 3.12 program that runs beside a Jellyfin server. It polls the session list, judges each **new** sign-in by the
country of its address, alerts on foreign ones without a travel window, and (only when told to) signs that device out.
It can end people's sessions, so **every doubt must end in "allow"**. Standard library only (add a dependency only when it clearly earns its place).
It has not been run against a real Jellyfin and has not had an independent review; `docs/security-review.md` is the threat model and the release checklist.

## Layout

| Path | What |
|---|---|
| `src/lombo_visa/core.py` | The decisions. Pure: no network, disk or clock (time is passed in). `decide()`, `plan_round()`, `due_signouts()`, `new_window()`, `clean_text()`. |
| `src/lombo_visa/mmdb.py` | A minimal MMDB reader for untrusted files. Raises `MmdbError` on anything odd. |
| `src/lombo_visa/geoip.py` | The `Lookup` interface, opening the database, and the safe download/update. |
| `src/lombo_visa/jellyfin.py` | The Jellyfin client: sessions, users, sign out a device. |
| `src/lombo_visa/state.py` | The JSON state file (allowed devices, notes, travel windows). |
| `src/lombo_visa/config.py` | Settings from `LOMBO_VISA_*` environment variables. |
| `src/lombo_visa/notify.py` | Webhook alerts (mentions off). |
| `src/lombo_visa/gateway.py` | Optional second mode: a stdlib HTTP server for the sign-in paths only. Classifies the path (decode once, refuse ambiguous forms), finds the client address (`X-Forwarded-For` only from `TRUSTED_PROXIES`), decides with `core.decide`, forwards byte for byte or answers 403. Never a general proxy; never logs bodies, tokens or addresses. Notes in `docs/security.md`. |
| `src/lombo_visa/audit.py` | Opt-in JSON-lines decision log (`AUDIT_FILE`); addresses only as a keyed hash; rotation to one `.1` file; never raises. |
| `src/lombo_visa/cli.py` | `doctor`, `plan`, `run`, `travel`, `geoip`, `gateway`, `audit`. The only place that prints, reads the clock or ends a session (the gateway prints its own log lines and may end a refused Quick Connect session). |
| `tests/` | pytest. Fake Jellyfin and webhook in `conftest.py`; `mmdb_builder.py` builds a tiny database in memory; `test_hardening.py` and `test_mmdb_fuzz.py` hold the audit regressions (every fix has a test that fails without it). |
| `scripts/leak-check.sh` | Fails when private or personal strings appear. CI runs it. |
| `docs/` | One topic per file, each with a `## Contents` list. |

## Rules

- **Keep `core.py` pure.** If a rule needs data, pass it in (the clock and the country function are arguments).
- **Fail open.** A new code path that can fail (lookup, parse, file, network) must end in "allow" and a message, never in a sign-out. Only the `deny` verdict may lead to `sign_out_device`.
- **Safe by default.** `DRY_RUN` defaults to true and `BLOCK_SESSIONS` to false. `plan` never writes anything.
- **Treat client text as hostile.** User names, device names and clients go through `core.clean_text` before any log line or alert.
- **No real database in the repo, ever.** DB-IP Lite is CC BY 4.0: it is downloaded at run time and credited in `NOTICE.md` and the README. Tests use `tests/mmdb_builder.py`. Test addresses must not be documentation ranges (`203.0.113.x` and the like count as private to Python).
- **A new setting** goes in `config.py` (validated, message names the variable), `SETTINGS`, `.env.example` (a test keeps them in sync).
- **A change to what a denial does** also changes `docs/how-it-works.md` and `docs/security-review.md`. A change to the gateway or the audit log also changes `docs/security.md` and the README "What is not verified" list.
- **The gateway forwards only exact sign-in paths** and answers 404 to everything else. Never widen `gateway.classify` to a prefix match, never log a body, a token, an `Authorization` header or an address, and never send a request anywhere but the configured upstream. Dry run (`DRY_RUN=true`) always makes it act as `log`.
- **Never put private details in the repo:** real hostnames, IPs, webhook URLs, keys, local paths. Use `jellyfin.example.com`, `/mnt/media`, `changeme`. `scripts/leak-check.sh` enforces it; patterns live in `scripts/leak-patterns.txt`.
- Plain language in docs and messages. Never imply the Jellyfin project made this.
- The Lombo look (every icon has the Lombo L) is in `docs/branding.md`.

## Test

```sh
pip install -e '.[dev]'
pre-commit install            # once: runs the checks below before every commit
pre-commit run --all-files    # ruff lint + format, strict mypy, file hygiene, leak check
pytest -q --cov               # tests with coverage (the gate is `fail_under` in pyproject.toml)
```

These are the same checks CI runs (one config: `.pre-commit-config.yaml` and `pyproject.toml`), so what passes
locally passes there. The tooling is the same in every Lombo-Forge repo: ruff for lint and format, strict mypy,
pytest. Do not loosen a rule to make a change pass; fix the code, or say why in a comment next to a narrow `noqa`.

Without Python installed: `docker run --rm -v "$PWD:/w" -w /w python:3.12-slim sh -c "pip install -q -e '.[dev]' && pytest -q"`.
Tests use a fake server; before claiming a Jellyfin behaviour works, check it against a throwaway
Jellyfin container, never someone's real server.

## Working efficiently

- Optional tools that live in your own profile, not in this repo. Skip any you do not have.
- Graphify: build the code-only graph with `graphify update .` (never `/graphify .`, which costs tokens) and run
  `graphify query "<question>"` before grepping. `graphify-out/` is generated and gitignored; INFERRED edges are guesses.
- Ponytail: the smallest change that fully solves the task. Keep diffs small and focused.

## Skills (`.claude/skills/`)

- `install-into-my-stack`: help a user put the app into their setup and run `doctor`.
- `troubleshoot`: read `doctor` and `plan` output and the logs.
- `adapt`: change the rules, thresholds or settings safely.
- `ship-a-change`, `release`: get a change into `main`; cut a release.
