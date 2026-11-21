---
name: ship-a-change
description: Checklist for getting a change into main of this repo (branch, checks, leak check, commit message, pull request). Use before committing or opening a pull request.
---

# Ship a change

1. **Branch** off an up-to-date `main`: `git switch -c <short-topic>`. Never commit or push to `main` (it is protected).
2. Make the smallest change that fully solves the task. Add or update a test for it.
3. **Checks, all must pass:**
   - `pre-commit run --all-files` (file hygiene, ruff lint and format, strict mypy, leak check)
   - `pytest -q --cov`
   - `sh scripts/leak-check.sh`
   Do not loosen a rule; fix the code, or add a narrow `noqa` with a reason.
4. **Leak check by eye** on the diff: no real hostnames, IPs, webhook URLs, keys, local paths, personal names.
5. **Commit message in a file**, no trailers (no Co-Authored-By, no "generated with"):
   `git commit -F /tmp/msg.txt`.
6. `git push -u origin <branch>`, then `gh pr create --base main` with a plain-language description (no attribution lines).
7. Wait for CI. The repo owner merges. Never push to `main` directly.

In the template itself, `tests/test_rename.py` renames a copy and runs its tests: it must keep passing.
