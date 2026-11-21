#!/bin/sh
# Fails if a private or personal string from scripts/leak-patterns.txt appears anywhere in the repo.
# Run by CI and worth running before every push. Add your own patterns to the file (one regex per line).
set -eu
cd "$(dirname "$0")/.."
if git ls-files >/dev/null 2>&1 && [ -n "$(git ls-files)" ]; then
  files=$(git ls-files)
else
  files=$(find . -type f -not -path './.git/*' -not -path '*/__pycache__/*' -not -path './.pytest_cache/*' | sed 's|^\./||')
fi
hits=$(printf '%s\n' "$files" | grep -v -e '^scripts/leak-patterns.txt$' -e '^scripts/leak-check.sh$' -e '^LICENSE$' \
  | xargs grep -nIiE -f scripts/leak-patterns.txt 2>/dev/null || true)
if [ -n "$hits" ]; then
  printf 'Leak check FAILED. Remove these before publishing:\n%s\n' "$hits" >&2
  exit 1
fi
echo "Leak check passed."
