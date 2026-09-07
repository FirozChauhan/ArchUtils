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
environment variables (env vars win). See .env.example.

Usage:
  Jackdaw.py <profile>             run the daemon (cycle every DELAY_SECONDS)
  Jackdaw.py <profile> --once      apply the next wallpaper and exit
  Jackdaw.py <profile> --set FILE  apply one specific wallpaper and exit
  Jackdaw.py <profile> --status    show current/next wallpaper and config
  Jackdaw.py <profile> --list      list the rotation, newest first
"""

import argparse
import fcntl
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
    "SETTER": "awww",
    "DELAY_SECONDS": "3600",
    "IMAGE_EXTENSIONS": ".avif,.png,.jpg,.jpeg,.webp,.gif,.bmp,.tiff,.tif,.jxl",
}

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
SETTER = ENV["SETTER"]
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
    fresh read means we never clobber another profile's newer entry.
    """
    with open(DB_PATH + ".lock", "w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        try:
            state = load_state()
            state[profile] = name
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


def set_wallpaper(folder, name):
    """Apply a wallpaper without a shell. Returns True on success."""
    path = os.path.join(folder, name)
    try:
        result = subprocess.run(
            [SETTER, "img", path, "--transition-type", "none"],
            capture_output=True,
            text=True,
            timeout=30,
        )
    except FileNotFoundError:
        log.error("'%s' disappeared from PATH - reinstall it and restart Jackdaw", SETTER)
        return False
    except subprocess.TimeoutExpired:
        log.error("%s timed out applying %s", SETTER, name)
        return False
    if result.returncode != 0:
        # e.g. setter daemon not running, or the file was deleted mid-cycle
        log.error("%s failed (%s): %s", SETTER, result.returncode,
                  result.stderr.strip() or name)
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
    if shutil.which(SETTER) is None:
        sys.exit(f"'{SETTER}' not found in PATH - install it or set SETTER in .env")


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
    sys.exit(f"{SETTER} failed to apply every wallpaper in {folder}")


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
        sys.exit(f"{SETTER} failed to apply {name}")
    merge_save(profile, name)
    print(f"[{profile}] set -> {name}")


def cmd_status(folder, profile):
    walls = scan_wallpapers(folder)
    current = load_state().get(profile)
    print(f"profile:  {profile}")
    print(f"folder:   {folder}")
    print(f"setter:   {SETTER}")
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
    log.info("watching %s every %ss via '%s'", folder, DEFAULT_DELAY, SETTER)

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
    parser.add_argument("profile", help="name of the profile folder under WALLPAPERS_ROOT")
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--once", action="store_true",
                       help="apply the next wallpaper and exit (for cron/timers)")
    group.add_argument("--set", metavar="FILE",
                       help="apply one specific wallpaper from the profile folder and exit")
    group.add_argument("--status", action="store_true",
                       help="show current/next wallpaper and effective config")
    group.add_argument("--list", action="store_true",
                       help="list wallpapers in rotation order (newest first)")
    args = parser.parse_args()

    folder = resolve_folder(args.profile)

    if args.once:
        cmd_once(folder, args.profile)
    elif args.set:
        cmd_set(folder, args.profile, args.set)
    elif args.status:
        cmd_status(folder, args.profile)
    elif args.list:
        cmd_list(folder, args.profile)
    else:
        cmd_daemon(folder, args.profile)


if __name__ == "__main__":
    main()
