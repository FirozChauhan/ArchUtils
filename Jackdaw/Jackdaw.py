#!/usr/bin/env python3
"""Jackdaw wallpaper daemon.

Cycles the wallpapers of a profile folder, persisting the current wallpaper
(filename, not index) to a JSON file next to this script. Using filenames
keeps the state valid even when the folder is re-sorted, or wallpapers are
added or removed, and the folder is re-scanned every cycle so new images
appear without a restart. The loop survives any per-cycle error (deleted
files, permission problems, a failing setter) and simply advances to the
next wallpaper.

Configuration comes from a .env file next to this script and/or real
environment variables (env vars win). See .env.example. The setter is
hyprpaper, applied via 'hyprctl hyprpaper wallpaper' (a running Hyprland
session with autostarted 'hyprpaper' is required).

Usage:
  Jackdaw.py <profile>             run the daemon (cycle every DELAY_SECONDS)
  Jackdaw.py <profile> --once      apply the next wallpaper and exit
  Jackdaw.py <profile> --set FILE  apply one specific wallpaper and exit
  Jackdaw.py [profile] --restore   re-apply the current wallpaper and exit
                                   (no rotation; boot use; skips the profile
                                   arg and uses the last-used one)
  Jackdaw.py <profile> --status    show current/next wallpaper and config
  Jackdaw.py <profile> --list      list the rotation, newest first
"""

import argparse
import fcntl
import glob
import json
import logging
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import time

SCRIPT_DIR = os.path.dirname(os.path.realpath(__file__))

# Defaults; override via .env or environment variables
DEFAULTS = {
    "WALLPAPERS_ROOT": os.path.expanduser("~/Pictures/wallpapers"),
    "DB_PATH": os.path.join(SCRIPT_DIR, "wallpaper.json"),
    "DELAY_SECONDS": "3600",
    "IMAGE_EXTENSIONS": ".png,.jpg,.jpeg,.webp,.jxl",
    "RESTORE_WAIT_SECONDS": "20",
}

# Reserved state key (never a real folder name) holding the last profile that
# applied a wallpaper, so --restore can rebuild the boot screen on its own.
LAST_KEY = "_last"

log = logging.getLogger("jackdaw")


def load_env_file(path):
    """Minimal .env parser: KEY=VALUE lines, # comments, optional quotes."""
    values = {}
    try:
        with open(path) as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, _, val = line.partition("=")
                key = key.strip()
                val = val.strip().strip("'\"")
                if key:
                    values[key] = val
    except FileNotFoundError:
        pass
    return values


ENV = {**DEFAULTS, **load_env_file(os.path.join(SCRIPT_DIR, ".env")), **{
    k: v for k, v in os.environ.items() if k in DEFAULTS
}}

WALLPAPERS_ROOT = ENV["WALLPAPERS_ROOT"]
# Relative DB_PATH resolves next to the script, so state works from any cwd
DB_PATH = ENV["DB_PATH"]
if not os.path.isabs(DB_PATH):
    DB_PATH = os.path.join(SCRIPT_DIR, DB_PATH)
DEFAULT_DELAY = int(ENV["DELAY_SECONDS"])
IMAGE_EXTS = {
    e.strip().lower() if e.strip().startswith(".") else "." + e.strip().lower()
    for e in ENV["IMAGE_EXTENSIONS"].split(",")
    if e.strip()
}


def load_state():
    """Load per-profile state, tolerating a missing/corrupt file."""
    try:
        with open(DB_PATH) as f:
            state = json.load(f)
        return state if isinstance(state, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def save_state(state):
    """Atomic write via a unique temp file, so a crash can't corrupt the JSON
    and concurrent daemons never fight over one shared .tmp path."""
    fd, tmp = tempfile.mkstemp(
        dir=os.path.dirname(DB_PATH) or ".", prefix=".wallpaper-", suffix=".tmp"
    )
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(state, f, indent=2)
        os.replace(tmp, DB_PATH)
    except BaseException:
        os.unlink(tmp)
        raise


def merge_save(profile, name):
    """Reload state under an exclusive lock, update only our profile key, save.

    Multiple daemons (one per profile) run concurrently; the lock plus the
    fresh read means we never clobber another profile's newer entry. Also
    records which profile applied last, for --restore at boot.
    """
    with open(DB_PATH + ".lock", "w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        try:
            state = load_state()
            state[profile] = name
            state[LAST_KEY] = profile
            save_state(state)
        finally:
            fcntl.flock(lock, fcntl.LOCK_UN)


def is_wallpaper(path):
    """Regular file with a known image extension (case-insensitive)."""
    if os.path.splitext(path)[1].lower() not in IMAGE_EXTS:
        return False
    try:
        return os.path.isfile(path)
    except OSError:
        return False


def scan_wallpapers(folder):
    """Image files in folder, newest first. Skips unreadable/vanished files."""
    walls = []
    try:
        entries = os.listdir(folder)
    except OSError as exc:
        log.warning("cannot list %s: %s", folder, exc)
        return []
    for name in entries:
        if name.startswith("."):
            continue  # hidden files (thumbnails, dotfiles) are never wallpapers
        path = os.path.join(folder, name)
        try:
            if is_wallpaper(path):
                walls.append((os.path.getmtime(path), name))
        except OSError:
            continue  # deleted or unreadable between listing and stat
    walls.sort(key=lambda t: t[0], reverse=True)
    return [name for _, name in walls]


def last_index(stored, walls):
    """Index of the last shown wallpaper, or -1 if unknown.

    Accepts the new filename format and legacy integer-index entries,
    so an existing wallpaper.json keeps working unchanged.
    """
    if isinstance(stored, str):
        try:
            return walls.index(stored)
        except ValueError:
            return -1  # that wallpaper is gone; start from the newest
    try:
        # Legacy entries stored the count before display, i.e. last index + 1
        index = int(stored) - 1
        return index if 0 <= index < len(walls) else -1
    except (TypeError, ValueError):
        return -1


def hyprpaper_running():
    """True if a hyprpaper IPC socket exists for the current Hyprland session.

    hyprpaper >= 0.8 exposes a hidden hyprwire socket named
    '.hyprpaper.sock' under $XDG_RUNTIME_DIR/hypr/<signature>/ (older
    releases used a visible 'hyprpaper.sock'); either one counts.
    """
    sig = os.environ.get("HYPRLAND_INSTANCE_SIGNATURE")
    runtime = os.environ.get("XDG_RUNTIME_DIR", f"/run/user/{os.getuid()}")
    if not sig:
        return False
    base = os.path.join(runtime, "hypr", sig)
    return bool(glob.glob(os.path.join(base, ".hyprpaper.sock"))
                or glob.glob(os.path.join(base, "hyprpaper.sock")))


def set_wallpaper(folder, name):
    """Apply a wallpaper to every monitor via 'hyprctl hyprpaper'.

    hyprpaper 0.8 speaks a binary protocol, so the supported way to poke
    it is hyprctl's hyprpaper subcommand: arguments are comma-separated
    [mon],[path],[fit_mode]. An *empty* monitor field is the all-monitors
    wildcard ('*' is rejected as an invalid monitor name by 0.8.4's IPC
    validator). hyprctl exits non-zero and prints 'error: ...' on any
    failure, and images are reference-counted inside hyprpaper, so there
    is no preload/unload bookkeeping to do here. Returns True on success.
    """
    path = os.path.join(folder, name)
    try:
        result = subprocess.run(
            ["hyprctl", "hyprpaper", "wallpaper", f",{path}"],
            capture_output=True,
            text=True,
            timeout=30,
        )
    except FileNotFoundError:
        log.error("'hyprctl' disappeared from PATH - install hyprland and restart Jackdaw")
        return False
    except subprocess.TimeoutExpired:
        log.error("hyprctl timed out applying %s", name)
        return False
    if result.returncode != 0:
        # e.g. hyprpaper died, or the file vanished between scan and apply
        detail = (result.stdout + result.stderr).strip()
        log.error("hyprpaper failed (%s): %s", result.returncode,
                  detail or "no wallpaper IPC response - is hyprpaper running?")
        return False
    return True


def _term(signum, frame):
    raise KeyboardInterrupt


def _now():
    # CLOCK_BOOTTIME keeps ticking during suspend, unlike CLOCK_MONOTONIC
    # (which time.monotonic/time.sleep follow), so a laptop suspended mid-sleep
    # catches up on resume instead of stretching every interval.
    return time.clock_gettime(time.CLOCK_BOOTTIME)


def sleep_until(deadline):
    """Sleep in short chunks, re-checking the boottime clock each wake."""
    while True:
        remaining = deadline - _now()
        if remaining <= 0:
            return
        time.sleep(min(remaining, 30))


def pick_next(profile, walls, failed=frozenset()):
    """Filename of the next wallpaper after the persisted one, skipping failures."""
    state = load_state()
    index = last_index(state.get(profile), walls)
    failed = set(failed) & set(walls)
    for _ in range(len(walls)):
        index = (index + 1) % len(walls)
        if walls[index] not in failed:
            break
    return walls[index]


def require_setter():
    if shutil.which("hyprctl") is None:
        sys.exit("'hyprctl' not found in PATH - Jackdaw applies wallpapers via 'hyprctl hyprpaper'")
    if not hyprpaper_running():
        sys.exit("hyprpaper is not running (no .hyprpaper.sock IPC socket) - start it or log in again")


def wait_for_hyprpaper(timeout):
    """Poll until hyprpaper's IPC socket appears (used by --restore at boot,
    where Jackdaw runs the same instant autostart spawns hyprpaper)."""
    deadline = _now() + timeout
    while not hyprpaper_running():
        if _now() >= deadline:
            return False
        time.sleep(0.3)
    return True


def resolve_folder(profile):
    folder = os.path.join(WALLPAPERS_ROOT, profile)
    if not os.path.isdir(folder):
        sys.exit(f"No wallpaper folder found: {folder}")
    return folder


def cmd_once(folder, profile):
    require_setter()
    walls = scan_wallpapers(folder)
    if not walls:
        sys.exit(f"No wallpapers in {folder}")
    failed = set()
    for _ in range(len(walls)):
        name = pick_next(profile, walls, failed)
        if set_wallpaper(folder, name):
            merge_save(profile, name)
            print(f"[{profile}] -> {name}")
            return
        failed.add(name)  # broken image must not block cron - try the next one
    sys.exit(f"hyprpaper failed to apply every wallpaper in {folder}")


def cmd_set(folder, profile, value):
    require_setter()
    path = value if os.path.dirname(value) else os.path.join(folder, value)
    path = os.path.realpath(path)
    if os.path.dirname(path) != os.path.realpath(folder):
        sys.exit(f"--set must name a file inside the profile folder: {folder}")
    if not os.path.isfile(path):
        sys.exit(f"No such file: {path}")
    name = os.path.basename(path)
    if not set_wallpaper(folder, name):
        sys.exit(f"hyprpaper failed to apply {name}")
    merge_save(profile, name)
    print(f"[{profile}] set -> {name}")


def cmd_restore(folder, profile):
    """Apply the profile's CURRENT wallpaper without advancing the rotation.

    Boot helper: hyprpaper starts empty (Jackdaw drives it over IPC, so
    hyprpaper.conf has no static wallpaper lines), which left a black screen
    until a hotkey press. This re-applies what wallpaper.json says is shown.
    Waits for hyprpaper's socket instead of failing fast, because it is meant
    to run right next to the autostart 'hyprpaper' line."""
    if shutil.which("hyprctl") is None:
        sys.exit("'hyprctl' not found in PATH - Jackdaw applies wallpapers via 'hyprctl hyprpaper'")
    if not wait_for_hyprpaper(int(ENV["RESTORE_WAIT_SECONDS"])):
        sys.exit("hyprpaper did not start in time (no .hyprpaper.sock IPC socket)")
    walls = scan_wallpapers(folder)
    if not walls:
        sys.exit(f"No wallpapers in {folder}")
    name = load_state().get(profile)
    if name not in walls:
        name = walls[0]  # remembered wallpaper was deleted/moved - newest wins
    if not set_wallpaper(folder, name):
        sys.exit(f"hyprpaper failed to apply {name}")
    merge_save(profile, name)
    print(f"[{profile}] restored -> {name}")


def cmd_status(folder, profile):
    walls = scan_wallpapers(folder)
    current = load_state().get(profile)
    print(f"profile:  {profile}")
    print(f"folder:   {folder}")
    print("setter:   hyprpaper (hyprctl hyprpaper wallpaper ,<path>)")
    print(f"interval: {DEFAULT_DELAY}s")
    print(f"state:    {DB_PATH}")
    print(f"images:   {len(walls)}")
    print(f"current:  {current or '(unknown - nothing applied yet)'}")
    if walls:
        print(f"next:     {pick_next(profile, walls)}")
    else:
        print("next:     (none - folder has no images)")


def cmd_list(folder, profile):
    walls = scan_wallpapers(folder)
    if not walls:
        sys.exit(f"No wallpapers in {folder}")
    current = load_state().get(profile)
    nxt = pick_next(profile, walls)
    for i, name in enumerate(walls):
        marker = "*" if name == current else ">" if name == nxt else " "
        print(f"{marker} {i:3d}  {name}")
    print("(* current, > next)")


def cmd_daemon(folder, profile):
    require_setter()
    signal.signal(signal.SIGTERM, _term)
    log.info("watching %s every %ss via hyprpaper", folder, DEFAULT_DELAY)

    failed = set()  # names that failed to apply since the last success

    try:
        while True:
            cycle_start = _now()
            try:
                walls = scan_wallpapers(folder)
                if not walls:
                    # Folder empty/unreadable (e.g. files being moved) - retry next cycle
                    log.warning("no wallpapers in %s, retrying in %ss", folder, DEFAULT_DELAY)
                else:
                    name = pick_next(profile, walls, failed)
                    if set_wallpaper(folder, name):
                        failed.discard(name)
                        merge_save(profile, name)
                        log.info("[%s] -> %s", profile, name)
                    else:
                        failed.add(name)
                        log.warning("[%s] %s failed to apply, skipping it next cycle",
                                    profile, name)
            except Exception:
                # A background daemon must never die mid-rotation
                log.exception("[%s] cycle failed, continuing", profile)

            # Deadline from cycle start: long setter calls don't drift the
            # schedule, and boottime sleep catches up after suspend.
            sleep_until(cycle_start + DEFAULT_DELAY)
    except KeyboardInterrupt:
        log.info("[%s] stopped - last wallpaper saved to %s", profile, DB_PATH)


def main():
    logging.basicConfig(level=logging.INFO, format="[%(levelname)s] %(message)s")

    parser = argparse.ArgumentParser(
        prog="Jackdaw.py", description="Wallpaper daemon: one folder per profile.")
    parser.add_argument("profile", nargs="?",
                        help="name of the profile folder under WALLPAPERS_ROOT "
                             "(optional with --restore: uses the last-used profile)")
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--once", action="store_true",
                       help="apply the next wallpaper and exit (for cron/timers)")
    group.add_argument("--set", metavar="FILE",
                       help="apply one specific wallpaper from the profile folder and exit")
    group.add_argument("--restore", action="store_true",
                       help="re-apply the current wallpaper (no rotation) and exit")
    group.add_argument("--status", action="store_true",
                       help="show current/next wallpaper and effective config")
    group.add_argument("--list", action="store_true",
                       help="list wallpapers in rotation order (newest first)")
    args = parser.parse_args()

    profile = args.profile
    if args.restore and profile is None:
        # Boot restore without a profile: show what was on screen when the
        # session last ended. State files predating '_last' fall back to the
        # first profile key, so the very first boot still succeeds quietly
        # (and merge_save records '_last' from there on).
        state = load_state()
        candidates = [state.get(LAST_KEY)] + [
            k for k in state if k != LAST_KEY
        ]
        profile = next(
            (c for c in candidates
             if c and os.path.isdir(os.path.join(WALLPAPERS_ROOT, c))),
            None,
        )
        if not profile:
            sys.exit("--restore needs a profile (no usable profiles in "
                     f"{DB_PATH}; pass <profile> explicitly)")
    if profile is None:
        parser.error("the 'profile' argument is required")
    folder = resolve_folder(profile)

    if args.once:
        cmd_once(folder, profile)
    elif args.set:
        cmd_set(folder, profile, args.set)
    elif args.restore:
        cmd_restore(folder, profile)
    elif args.status:
        cmd_status(folder, profile)
    elif args.list:
        cmd_list(folder, profile)
    else:
        cmd_daemon(folder, profile)


if __name__ == "__main__":
    main()
