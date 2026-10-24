#!/bin/sh
# Fails if the staged tree carries a secret or a personal string.
#  - generic secret patterns live here
#  - personal patterns (one ERE per line, '#' comments allowed) are NOT tracked: they are read from
#    $ZJ_FORBIDDEN_FILE, else <git-common-dir>/info/public-forbidden
# Exit: 0 clean, 1 found (file:line listed), 2 error / no forbidden file (override: ZJ_ALLOW_NO_FORBIDDEN=1).
set -u
cd "$(git rev-parse --show-toplevel)" || exit 2
common="$(git rev-parse --git-common-dir)" || exit 2
forbidden="${ZJ_FORBIDDEN_FILE:-$common/info/public-forbidden}"

pats="$(mktemp)" || exit 2
out="$(mktemp)" || exit 2
trap 'rm -f "$pats" "$out"' EXIT

cat > "$pats" <<'GENERIC'
sk-ant-
ghp_[A-Za-z0-9]
github_pat_
-----BEGIN .* PRIVATE KEY-----
ANTHROPIC_API_KEY=[^[:space:]"$'<]
GENERIC

if [ -f "$forbidden" ]; then
  grep -vE '^[[:space:]]*(#|$)' "$forbidden" >> "$pats"
elif [ "${ZJ_ALLOW_NO_FORBIDDEN:-0}" = 1 ]; then
  echo "check-public: warning: no personal-pattern file ($forbidden); only generic secrets checked" >&2
else
  echo "check-public: no personal-pattern file at $forbidden (set ZJ_FORBIDDEN_FILE, or ZJ_ALLOW_NO_FORBIDDEN=1)" >&2
  exit 2
fi

git grep --cached -nIiE -f "$pats" -- . ':!scripts/check-public.sh' > "$out"
rc=$?
if [ "$rc" = 0 ]; then
  echo "check-public: forbidden strings in the staged tree:"
  cat "$out"
  exit 1
elif [ "$rc" != 1 ]; then
  echo "check-public: git grep failed (exit $rc)" >&2
  exit 2
fi

git grep --cached -a -l -iE -f "$pats" -- . ':!scripts/check-public.sh' > "$out" 2>/dev/null
if [ "$?" = 0 ]; then
  echo "check-public: warning: pattern bytes found in binary/other files:" >&2
  cat "$out" >&2
fi
echo "check-public: clean"
