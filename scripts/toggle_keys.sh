#!/bin/bash
#===============================================================================
# toggle_keys.sh — hotkey switch for "typing-safe" workspace mode
#
# What it does
#   DISABLE: removes the bare-number workspace switches (1..9,0) and the
#            CTRL+Space flameshot screenshot bind, so pressing 1..9 types
#            digits instead of jumping workspaces.
#   ENABLE:  restores exactly those binds.
#   Keys with modifiers (SUPER+n movetoworkspace, CTRL_ALT+n app launchers)
#   are never touched — hyprland.lua owns those.
#
# How to run
#   Bound to SUPER+F12 in hyprland.lua; installed as a symlink:
#     ~/.config/hypr/toggle_keys.sh -> ArchUtils/scripts/toggle_keys.sh
#   (Edit the ArchUtils copy; the symlink picks it up with no reload.)
#
# Requirements
#   Hyprland >= 0.56 using the Lua config parser (hyprland.lua).
#   Runtime binding edits go through `hyprctl eval`, which executes Lua in
#   the live config environment using the same hl.bind/hl.unbind API the
#   config itself uses. `hyprctl keyword` no longer exists there.
#
# State
#   A sentinel file (/tmp/workspace_keys_disabled) marks "disabled".
#   /tmp is wiped on reboot — matching Hyprland restoring all binds at
#   startup. A config *reload* while disabled also restores the binds but
#   leaves the sentinel behind; the next F12 (enable) is safe because it
#   purges-then-binds, and the F12 after that disables again — self-healing.
#===============================================================================

set -u  # unset vars are errors; no set -e: one failed eval shouldn't abort mid-toggle

STATE_FILE="/tmp/workspace_keys_disabled"

# Bare keys to toggle; key "0" selects workspace 10 (Hyprland 1-based).
declare -a KEYS=(1 2 3 4 5 6 7 8 9 0)

# Screenshot bind (matches hyprland.lua: hl.bind("CTRL + space", ...)).
SHOT_KEY="CTRL + space"
SHOT_CMD="flameshot gui"

#-------------------------------------------------------------------------------
# lu <lua-chunk>
#   Run a Lua chunk in the live Hyprland config via `hyprctl eval`.
#   Success is exactly: rc=0 AND stdout == "ok". Anything else ("error: ..."
#   from the parser, rc=7; "Couldn't open a socket" when no session is
#   reachable) is reported to stderr and returns non-zero — but never exits,
#   so callers keep toggling the remaining keys.
#-------------------------------------------------------------------------------
lu() {
    local out rc
    out=$(hyprctl eval "$1" 2>&1)
    rc=$?
    if (( rc != 0 )) || [[ "$out" != ok ]]; then
        echo "toggle_keys: $out" >&2
        return 1
    fi
    return 0
}

# Workspace number a bare key should focus ("0" -> 10, else the digit).
ws_of() { [[ "$1" == 0 ]] && echo 10 || echo "$1"; }

# Bring all binds back to the live config. hl.bind APPENDS (it does NOT
# replace), and hl.unbind removes *all* binds matching key+mods — so we
# purge each key first; enable is therefore idempotent and can never
# stack duplicate flameshot/workspace binds.
enable_hotkeys() {
    local k
    for k in "${KEYS[@]}"; do
        lu "hl.unbind(\"$k\")"
        lu "hl.bind(\"$k\", hl.dsp.focus({ workspace = $(ws_of "$k") }))"
    done
    lu "hl.unbind(\"$SHOT_KEY\")"
    lu "hl.bind(\"$SHOT_KEY\", hl.dsp.exec_cmd(\"$SHOT_CMD\"))"
    notify-send "Hotkeys ENABLED"
}

# Strip the binds from the live config (exact mod-combo match: plain "1"
# is removed while "SUPER + 1" survives).
disable_hotkeys() {
    local k
    for k in "${KEYS[@]}"; do
        lu "hl.unbind(\"$k\")"
    done
    lu "hl.unbind(\"$SHOT_KEY\")"
    notify-send "Hotkeys DISABLED"
}

#-------------------------------------------------------------------------------
main() {
    # Health check: bail early (before touching state) if we're not in a
    # Hyprland Lua-config session.
    if ! lu 'return 1'; then
        echo "toggle_keys: hyprctl eval failed — is Hyprland (Lua config) running?" >&2
        exit 1
    fi

    # Flip the sentinel first, then apply — the binds always agree with
    # the sentinel once the script ends.
    if [[ -f $STATE_FILE ]]; then
        rm -f "$STATE_FILE"
        enable_hotkeys
    else
        touch "$STATE_FILE"
        disable_hotkeys
    fi
}

main "$@"
