#!/usr/bin/env bash
# Wither — simple CLI junk cleaner launcher
set -euo pipefail
DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
exec python3 "$DIR/wither/wither.py" "$@"