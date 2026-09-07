#!/usr/bin/env bash
#
# bump-version.sh — Semantic Versioning release helper
#
# What it does:
#   1. Verifies you're in a git repo, shows changed files (in red), then waits
#      for Enter before auto-running git add . (stages everything).
#   2. Asks which part to bump: patch / minor / major — Enter defaults to
#      patch, so you can just go straight to the commit message.
#   3. Asks for a commit message.
#   4. Commits, creates a new "vX.Y.Z" tag bumped from the last one.
#      (Does NOT push — you push manually when you're ready.)
#
# Usage:  ./bump-version.sh

set -euo pipefail

# --- pretty output --------------------------------------------------------
RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'
CYAN='\033[0;36m'; BOLD='\033[1m'; NC='\033[0m'

info() { printf "${CYAN}›${NC} %b\n" "$*"; }
ok()   { printf "${GREEN}✓${NC} %b\n" "$*"; }
warn() { printf "${YELLOW}!${NC} %b\n" "$*"; }
err()  { printf "${RED}✗${NC} %b\n" "$*" >&2; }

# --- pre-flight checks ----------------------------------------------------

# Inside a git repo?
if ! git rev-parse --is-inside-work-tree >/dev/null 2>&1; then
    err "Not inside a git repository."
    exit 1
fi

# Show what's changed before staging (changed files in red)
changed=$(git status --short)
if [ -n "$changed" ]; then
    echo
    printf "${BOLD}Changed files (will be staged):${NC}\n"
    git -c color.status=always status --short
    echo
    printf "Press Enter to continue... "
    read -r _ || true
else
    warn "No changes detected — nothing to stage."
fi

# Auto-stage everything
info "Staging all changes..."
git add .

# Track whether we need --allow-empty for the commit
allow_empty=""
if git diff --quiet && git diff --cached --quiet; then
    allow_empty="--allow-empty"
fi

# --- find the latest version tag (the version we bump from) ---------------
# Tags matching "v<digits>...", sorted by version, newest first.
latest_tag=$(git tag --list 'v[0-9]*' --sort=-v:refname 2>/dev/null | head -n 1)

if [ -n "$latest_tag" ]; then
    ok "Latest version tag: ${BOLD}${latest_tag}${NC}"
    ver="${latest_tag#v}"                 # strip the leading 'v'
    IFS='.' read -r major minor patch <<< "$ver"
else
    warn "No version tags found — starting from v0.0.0."
    major=0; minor=0; patch=0
fi

# --- ask: major / minor / patch (Enter defaults to patch) -----------------
echo
printf "${BOLD}What kind of release is this?${NC}\n"
printf "  1) patch  (bug fixes)       [default — just press Enter]\n"
printf "  2) minor  (new features)\n"
printf "  3) major  (breaking changes)\n"
while true; do
    printf "Bump type [Enter=patch]: "
    read -r ans
    case "$ans" in
        ""|1)  patch=$((patch + 1))                   ; break ;;
        2)     minor=$((minor + 1)); patch=0          ; break ;;
        3)     major=$((major + 1)); minor=0; patch=0 ; break ;;
        *)     warn "Press Enter for patch, or choose 2 or 3." ;;
    esac
done

new_tag="v${major}.${minor}.${patch}"
echo
info "Bumping ${BOLD}${latest_tag:-v0.0.0}${NC} → ${GREEN}${BOLD}${new_tag}${NC}"

# --- ask: commit message -------------------------------------------------
while true; do
    printf "Enter commit message: "
    read -r commit_msg
    [ -n "$commit_msg" ] && break
    warn "Commit message cannot be empty."
done

# --- stage + commit ------------------------------------------------------
info "Committing: \"${commit_msg}\""
# shellcheck disable=SC2086  # intentional word-split of allow_empty
git commit $allow_empty -m "${commit_msg}"

# --- tag the commit ------------------------------------------------------
info "Creating tag ${new_tag}..."
git tag -a "${new_tag}" -m "${commit_msg}"

echo
ok "Done! Tagged ${GREEN}${BOLD}${new_tag}${NC}"
ok "Commit message: ${commit_msg}"
ok "Tag: ${new_tag}"
