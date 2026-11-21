---
name: adapt
description: Change LomboVisa's rules, verdicts, settings, travel windows or alerts without breaking its fail-open promise. Use when the user wants different behaviour, a new setting, a new Jellyfin call, a different country source or a different alert target.
---

# Adapt LomboVisa

LomboVisa can sign people out of Jellyfin, so the first rule is: **every doubt ends in "allow"**. Only the `deny` verdict may reach
`Jellyfin.sign_out_device`. Read `docs/how-it-works.md` and the threats in `docs/security-review.md` before changing a rule.

1. **Change a rule:** edit `decide()` or `plan_round()` in `core.py`. Write the test first with plain data (`tests/test_core.py`), watch it fail,
   then change the code. Keep `core.py` free of network, disk and clock; the time and the country function are arguments.
2. **Add a setting:** read and validate it in `config.py` (the error names the variable), add it to `SETTINGS`, add a commented line to
   `.env.example` (a test keeps the three in sync) and a bad-value case in `tests/test_app.py`.
3. **Add a Jellyfin call:** a method in `jellyfin.py`, the path in the fake in `tests/conftest.py`, a test. Confirm it against a throwaway
   Jellyfin (container named `lf-...`) before relying on it. Never test against someone's real server.
4. **Another country source:** write a class with `country(ip) -> str | None` next to `MmdbLookup` in `geoip.py` and use it in `cli._lookup`.
   `None` means "do not know" and must stay harmless. Do not commit any database file; DB-IP Lite is CC BY 4.0 (credit in `NOTICE.md`).
5. **Change what a denial does:** also update `docs/how-it-works.md` and the matching threat in `docs/security-review.md`, and keep
   `DRY_RUN` defaulting to true and `BLOCK_SESSIONS` to false.
6. **Anything that prints a name, device or client** must go through `core.clean_text`.
7. **Run:** `pytest -q --cov`, `ruff check`, `ruff format --check`, `mypy`, `sh scripts/leak-check.sh`. All must pass.
