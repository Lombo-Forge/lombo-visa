---
name: release
description: Cut a release of this app (version bump, pull request, tag, what the release workflow does). Use only when the owner asks for a release.
---

# Release

1. Bump `version` in `pyproject.toml` and `__version__` in `src/lombo_visa/__init__.py` (same number).
2. Follow `ship-a-change`: branch, checks, pull request into `main`, CI green, owner merges.
3. From an up-to-date `main`: `git tag -a vX.Y.Z -m "vX.Y.Z"` and `git push origin vX.Y.Z`.
4. The tag runs `.github/workflows/release.yml`. For an app it builds the container image, pushes it to
   `ghcr.io/<owner>/lombo-visa` and creates a GitHub release with generated notes. In the template repo itself
   (`is_template`) only the GitHub release is made, no image.
5. Verify: `gh run list --workflow Release --limit 1` and `gh release view vX.Y.Z`.

Tagging is outward-facing: get the owner's go-ahead first.
