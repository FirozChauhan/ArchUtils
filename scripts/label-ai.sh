#!/usr/bin/env bash
# label-ai.sh — format an input line into "TITLE[NAME1,NAME2][TAG]".
# The TAG comes from $SCENE_LABEL in the monorepo .env (fallback: Tag).
#
# Usage: ./label-ai.sh Some Scene Title Jane Doe and John Smith
# Output: Some Scene Title[Jane Doe,John Smith][Tag]
#
# The title/name split is done by an AI model via the `ds` CLI (no API key).
# If ds is unavailable or returns nothing, a short error line is printed.

set -euo pipefail

# load SCENE_LABEL from the monorepo .env (gitignored), if present
_ENV="$(cd "$(dirname "$(readlink -f "$0")")/.." && pwd)/.env"
if [[ -f "$_ENV" ]]; then
  # shellcheck disable=SC1090
  set -a; . "$_ENV"; set +a
fi
LABEL="${SCENE_LABEL:-Tag}"

DS="${DS_BIN:-ds}"
input="$*"

if [[ -z "$input" ]]; then
  echo "usage: $(basename "$0") <scene title and names>" >&2
  exit 2
fi

PROMPT=$(cat <<EOF
Split the raw input below into its title and its trailing people names, then
reply with ONLY a JSON object, no other text:

{"title": "...", "names": ["...", "..."]}

Rules:
- The names are the people listed at the end of the input, joined by "and".
- "title" is everything before the names, trailing spaces removed.
- Copy each name verbatim.
- JSON only: no markdown, no code fences, no commentary.

Input: ${input}
EOF
)

if ! command -v "$DS" >/dev/null 2>&1; then
  printf 'error: formatter backend not found\n'
  exit 1
fi

raw="$("$DS" ask --json "$PROMPT" 2>/dev/null || true)"
reply="$(printf '%s' "$raw" | jq -r '.reply // empty' 2>/dev/null || true)"

if [[ -z "$reply" ]]; then
  printf 'error: formatter backend failed\n'
  exit 1
fi

# strip code fences / surrounding prose, keep the JSON object
json="$(printf '%s' "$reply" | sed -n 's/.*\({.*}\).*/\1/p' | head -n1)"
title="$(printf '%s' "$json" | jq -r '.title // empty' 2>/dev/null || true)"
names="$(printf '%s' "$json" | jq -r '(.names // []) | join(",")' 2>/dev/null || true)"

if [[ -z "$title" || -z "$names" ]]; then
  printf 'error: formatter backend failed\n'
  exit 1
fi

# build deterministically: TITLE[NAME1,NAME2][TAG]
reply="${title}[${names}][${LABEL}]"

# copy to clipboard (Wayland wl-copy, X11 xclip/xsel fallback)
if command -v wl-copy >/dev/null 2>&1; then
  printf '%s' "$reply" | wl-copy >/dev/null 2>&1 || true
elif command -v xclip >/dev/null 2>&1; then
  printf '%s' "$reply" | xclip -selection clipboard >/dev/null 2>&1 || true
elif command -v xsel >/dev/null 2>&1; then
  printf '%s' "$reply" | xsel --clipboard --input >/dev/null 2>&1 || true
fi

printf '%s\n' "$reply"
