#!/usr/bin/env python3
"""
Vox — convert videos to HEVC (H.265) or AV1 using ffmpeg.

Run it inside the folder with your videos:

    python vox.py

It converts the file(s) you choose into a new "Vox Output" folder,
leaving your originals untouched. Works on a single file or the
whole folder, and lets you pick an ffmpeg quality preset and CRF.

Requires ffmpeg (and ffprobe) on your PATH.
"""

from __future__ import annotations

import argparse
import json
import os
import select
import shutil
import subprocess
import sys
import time
from pathlib import Path

try:  # POSIX-only, used by the TUI
    import termios
    import tty as _tty
    _TUI_AVAILABLE = True
    _IFLAG, _OFLAG, _CFLAG, _LFLAG, _ISPEED, _OSPEED, _CC = range(7)
except ImportError:
    termios = None  # type: ignore[assignment]
    _tty = None  # type: ignore[assignment]
    _TUI_AVAILABLE = False

# --------------------------------------------------------------------------- #
# Constants
# --------------------------------------------------------------------------- #

VIDEO_EXTENSIONS = frozenset({
    ".mp4", ".mkv", ".mov", ".avi", ".webm", ".flv", ".m4v",
    ".wmv", ".mpg", ".mpeg", ".ts", ".m2ts", ".mts", ".3gp", ".ogv",
})

IMAGE_EXTENSIONS = frozenset({
    ".png", ".jpg", ".jpeg", ".bmp", ".tif", ".tiff", ".webp", ".avif",
})

# Suffixes that can never be a video — we probe unknown extensions with ffprobe
# (some downloaders drop the extension entirely), but skip these obvious ones.
KNOWN_NON_VIDEO_EXTENSIONS = frozenset({
    ".txt", ".md", ".pdf", ".doc", ".docx", ".xls", ".xlsx", ".ppt", ".pptx",
    ".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp", ".svg", ".ico", ".tiff", ".heic", ".avif",
    ".zip", ".rar", ".7z", ".tar", ".gz", ".bz2", ".xz", ".zst",
    ".sh", ".py", ".js", ".json", ".xml", ".html", ".htm", ".css", ".log", ".ini", ".cfg",
    ".torrent", ".part", ".crdownload", ".nfo", ".srt", ".sub", ".ass", ".vtt",
    ".db", ".sqlite", ".iso", ".exe", ".msi", ".deb", ".rpm", ".apk", ".so", ".dll", ".o", ".a",
})

OUTPUT_DIR_NAME = "Vox Output"

X265_PRESETS = [
    "ultrafast", "superfast", "veryfast", "faster", "fast",
    "medium", "slow", "slower", "veryslow",
]
X265_DEFAULT_PRESET = "medium"
X265_DEFAULT_CRF = 24
X265_CRF_MIN, X265_CRF_MAX = 0, 51

SVT_PRESET_MIN, SVT_PRESET_MAX = 0, 13  # libsvtav1: 0 = slowest/best, 13 = fastest
SVT_DEFAULT_PRESET = 8
AOM_PRESET_MIN, AOM_PRESET_MAX = 0, 11  # libaom-av1 -cpu-used: 0 = slowest, 11 = fastest
AOM_DEFAULT_PRESET = 6
AV1_DEFAULT_CRF = 30
AV1_CRF_MIN, AV1_CRF_MAX = 0, 63

# pix_fmts that may carry a transparency channel — ffmpeg's AVIF muxer cannot
# store alpha, so these are flattened onto a white background first.
MAY_HAVE_ALPHA_PIXFMTS = frozenset({
    "rgba", "abgr", "argb", "bgra", "gbrap", "gbrap10", "gbrap12",
    "yuva420p", "yuva422p", "yuva444p", "yuva444p12",
    "ya8", "ya16", "pal8",
})

AUDIO_BITRATE = "192k"

GREEN = "\033[32m"
YELLOW = "\033[33m"
RED = "\033[31m"
BOLD = "\033[1m"
RESET = "\033[0m"


# --------------------------------------------------------------------------- #
# Small I/O helpers
# --------------------------------------------------------------------------- #

def ask(prompt: str) -> str:
    """input() that never raises on closed/non-interactive stdin."""
    try:
        return input(prompt).strip()
    except EOFError:
        return ""


def ask_int(prompt: str, lo: int, hi: int, default: int) -> int:
    while True:
        raw = ask(f"{prompt} [{default}]: ").strip()
        if not raw:
            return default
        try:
            n = int(raw)
        except ValueError:
            print(f"{RED}Please enter a whole number.{RESET}")
            continue
        if lo <= n <= hi:
            return n
        print(f"{RED}Must be between {lo} and {hi}.{RESET}")


# --------------------------------------------------------------------------- #
# ffmpeg environment discovery
# --------------------------------------------------------------------------- #

def available_encoders() -> set[str]:
    """Names of encoders ffmpeg was built with (e.g. {'libx265', 'libsvtav1', ...})."""
    try:
        out = subprocess.run(
            ["ffmpeg", "-hide_banner", "-encoders"],
            capture_output=True, text=True, timeout=30,
        ).stdout
    except (OSError, subprocess.TimeoutExpired):
        return set()
    encoders: set[str] = set()
    for line in out.splitlines():
        parts = line.split()
        if len(parts) >= 2 and parts[0].startswith(("V", "A", "S")):
            encoders.add(parts[1])
    return encoders


def pick_av1_encoder(encoders: set[str]) -> str | None:
    for name in ("libsvtav1", "libaom-av1"):
        if name in encoders:
            return name
    return None


def has_avif_muxer() -> bool:
    """True if this ffmpeg build can write .avif files."""
    try:
        out = subprocess.run(
            ["ffmpeg", "-hide_banner", "-muxers"],
            capture_output=True, text=True, timeout=30,
        ).stdout
    except (OSError, subprocess.TimeoutExpired):
        return False
    return any(len(p) >= 2 and p[1] == "avif" for p in (line.split() for line in out.splitlines()))


# --------------------------------------------------------------------------- #
# File discovery / probing
# --------------------------------------------------------------------------- #

def has_video_stream(path: Path) -> bool:
    """True if ffprobe finds at least one video stream in the file."""
    cmd = [
        "ffprobe", "-v", "error",
        "-select_streams", "v:0",
        "-show_entries", "stream=codec_name",
        "-of", "csv=p=0", str(path),
    ]
    try:
        out = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
        return bool(out.stdout.strip())
    except (OSError, subprocess.TimeoutExpired):
        return False


def discover_videos(cwd: Path) -> list[Path]:
    videos = []
    for p in sorted(cwd.iterdir()):
        if not p.is_file() or p.name.startswith("."):
            continue
        # rstrip: names like "movie.mp4 " (trailing space) would otherwise
        # give a suffix of ".mp4 " that never matches.
        suffix = p.suffix.lower().rstrip()
        if suffix in VIDEO_EXTENSIONS:
            videos.append(p)
        elif suffix not in KNOWN_NON_VIDEO_EXTENSIONS and has_video_stream(p):
            # Some downloaders drop the extension entirely — accept if ffprobe
            # confirms it really is a video.
            videos.append(p)
    return videos


def probe_pix_fmt(path: Path) -> str | None:
    """Pixel format of the first video stream, or None on failure."""
    cmd = [
        "ffprobe", "-v", "error", "-select_streams", "v:0",
        "-show_entries", "stream=pix_fmt", "-of", "csv=p=0", str(path),
    ]
    try:
        out = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
        return out.stdout.strip() or None
    except (OSError, subprocess.TimeoutExpired):
        return None


def probe_video(path: Path) -> tuple[float | None, str | None]:
    """Return (duration_seconds, video_codec_name); (None, None) on any failure."""
    cmd = [
        "ffprobe", "-v", "error",
        "-select_streams", "v:0",
        "-show_entries", "stream=codec_name:format=duration",
        "-of", "json", str(path),
    ]
    try:
        out = subprocess.run(cmd, capture_output=True, text=True, timeout=60).stdout
        data = json.loads(out)
        streams = data.get("streams") or []
        codec = streams[0].get("codec_name") if streams else None
        dur = data.get("format", {}).get("duration")
        return (float(dur) if dur else None), codec
    except (OSError, ValueError, KeyError, json.JSONDecodeError):
        return None, None


def discover_images(cwd: Path) -> list[Path]:
    images = []
    for p in sorted(cwd.iterdir()):
        if not p.is_file() or p.name.startswith("."):
            continue
        if p.suffix.lower().rstrip() in IMAGE_EXTENSIONS:
            images.append(p)
    return images


# --------------------------------------------------------------------------- #
# Settings: codec / preset / CRF
# --------------------------------------------------------------------------- #

def validate_preset(codec: str, av1_encoder: str | None, value: str) -> str:
    if codec == "hevc":
        if value not in X265_PRESETS:
            raise ValueError(
                f"Invalid x265 preset '{value}'. Choose from: {', '.join(X265_PRESETS)}"
            )
        return value
    try:
        n = int(value)
    except ValueError:
        raise ValueError(f"AV1 preset must be an integer, got '{value}'.") from None
    lo, hi = (SVT_PRESET_MIN, SVT_PRESET_MAX) if av1_encoder == "libsvtav1" else (AOM_PRESET_MIN, AOM_PRESET_MAX)
    if not (lo <= n <= hi):
        raise ValueError(f"Preset must be between {lo} and {hi} for {av1_encoder}, got {n}.")
    return str(n)


def prompt_preset(codec: str, av1_encoder: str | None, forced: str | None) -> str:
    if forced is not None:
        try:
            return validate_preset(codec, av1_encoder, forced)
        except ValueError as e:
            print(f"{RED}{e}{RESET}")
            sys.exit(1)

    if codec == "hevc":
        print(f"\n{BOLD}x265 preset{RESET} (faster = quicker but bigger files; slower = smaller files):")
        print("  " + "  ".join(X265_PRESETS))
        while True:
            choice = ask(f"Pick one [{X265_DEFAULT_PRESET}]: ").strip().lower()
            if not choice:
                return X265_DEFAULT_PRESET
            if choice in X265_PRESETS:
                return choice
            print(f"{RED}Unknown preset '{choice}'. Choose from: {', '.join(X265_PRESETS)}{RESET}")
    else:
        if av1_encoder == "libsvtav1":
            print(f"\n{BOLD}SVT-AV1 preset{RESET} (0 = slowest/best quality, 13 = fastest):")
            print("  Recommended range 6-10; 8 is a good balance.")
            lo, hi, default = SVT_PRESET_MIN, SVT_PRESET_MAX, SVT_DEFAULT_PRESET
        else:
            print(f"\n{BOLD}libaom-av1 cpu-used{RESET} (0 = slowest/best quality, 11 = fastest):")
            print("  Recommended range 6-10. Note: aom is very slow; SVT-AV1 is preferred.")
            lo, hi, default = AOM_PRESET_MIN, AOM_PRESET_MAX, AOM_DEFAULT_PRESET
        while True:
            raw = ask(f"Pick a number between {lo} and {hi} [{default}]: ").strip()
            if not raw:
                return str(default)
            try:
                n = int(raw)
            except ValueError:
                print(f"{RED}Please enter an integer.{RESET}")
                continue
            if lo <= n <= hi:
                return str(n)
            print(f"{RED}Must be between {lo} and {hi}.{RESET}")


def prompt_crf(codec: str, forced: int | None) -> int:
    if codec == "hevc":
        lo, hi, default = X265_CRF_MIN, X265_CRF_MAX, X265_DEFAULT_CRF
        hint = "Typical range 18-28. Lower = better quality, bigger file."
    else:
        lo, hi, default = AV1_CRF_MIN, AV1_CRF_MAX, AV1_DEFAULT_CRF
        hint = "Typical range 24-40. Lower = better quality, bigger file."
    if forced is not None:
        if not (lo <= forced <= hi):
            print(f"{RED}CRF must be between {lo} and {hi} (got {forced}).{RESET}")
            sys.exit(1)
        return forced
    print(f"\n{BOLD}Quality (CRF):{RESET} {hint}")
    return ask_int("CRF value", lo, hi, default)


def resolve_settings(args: argparse.Namespace, encoders: set[str]) -> tuple[str, str | None, str, int]:
    """Return (codec, av1_encoder, preset, crf)."""
    av1_enc = pick_av1_encoder(encoders)
    hevc_ok = "libx265" in encoders

    if args.codec is None:
        options = []
        if hevc_ok:
            options.append(("1", "hevc", "HEVC (H.265) — great compatibility, ~50% smaller than H.264"))
        if av1_enc:
            options.append(("2", "av1", f"AV1 ({av1_enc}) — best compression, slower encodes, needs newer players"))
        if not options:
            print(f"{RED}No usable encoder found. ffmpeg is installed but lacks libx265 and AV1 encoders.{RESET}")
            print("On Debian/Ubuntu:  sudo apt install ffmpeg libx265-dev  (or use a static ffmpeg build)")
            sys.exit(1)
        if len(options) == 1:
            codec = options[0][1]
            print(f"{YELLOW}Only {options[0][2].split(' — ')[0]} is available — using it.{RESET}")
        else:
            print(f"\n{BOLD}Target codec:{RESET}")
            for key, _, desc in options:
                print(f"  [{key}] {desc}")
            choice = ask("Pick a codec [1]: ").strip() or "1"
            chosen = next((c for k, c, _ in options if k == choice), None)
            if chosen is None:
                print(f"{RED}Invalid choice: {choice}{RESET}")
                sys.exit(1)
            codec = chosen
    else:
        codec = args.codec
        if codec == "hevc" and not hevc_ok:
            print(f"{RED}HEVC was requested but ffmpeg has no libx265 encoder.{RESET}")
            sys.exit(1)
        if codec == "av1" and not av1_enc:
            print(f"{RED}AV1 was requested but ffmpeg has neither libsvtav1 nor libaom-av1.{RESET}")
            sys.exit(1)

    preset = prompt_preset(codec, av1_enc, args.preset)
    crf = prompt_crf(codec, args.crf)
    return codec, av1_enc, preset, crf


def resolve_image_settings(args: argparse.Namespace, encoders: set[str]) -> tuple[str, str, int]:
    """Return (av1_encoder, preset, crf) for AVIF still-image compression."""
    av1_enc = pick_av1_encoder(encoders)
    if not av1_enc:
        print(f"{RED}AVIF compression needs an AV1 encoder, but ffmpeg has neither "
              f"libsvtav1 nor libaom-av1.{RESET}")
        sys.exit(1)
    print(f"\n{YELLOW}Target format: AVIF — AV1 still-image compression (best ratio, newer apps/"
          f"browsers only).{RESET}")
    preset = prompt_preset("av1", av1_enc, args.preset)
    crf = prompt_crf("av1", args.crf)
    return av1_enc, preset, crf


# --------------------------------------------------------------------------- #
# File selection
# --------------------------------------------------------------------------- #

def _matches_extension(p: Path, kind: str) -> bool:
    suffix = p.suffix.lower().rstrip()
    if kind == "image":
        return suffix in IMAGE_EXTENSIONS
    return suffix in VIDEO_EXTENSIONS or (
        suffix not in KNOWN_NON_VIDEO_EXTENSIONS and has_video_stream(p)
    )


def select_files(args: argparse.Namespace, files: list[Path], cwd: Path, kind: str = "video") -> list[Path]:
    if args.files:
        selected = []
        for f in args.files:
            p = Path(f)
            if not p.is_absolute():
                p = cwd / p
            if p.is_file() and _matches_extension(p, kind):
                selected.append(p)
            else:
                print(f"{YELLOW}Warning: not a {kind} file, ignoring: {f}{RESET}")
        if not selected:
            print(f"{RED}None of the given files are valid {kind} files.{RESET}")
            sys.exit(1)
        return selected

    if args.all:
        return list(files)

    print(f"\n{BOLD}Found {len(files)} {kind} file(s) in {cwd}:{RESET}")
    for i, v in enumerate(files, 1):
        size_mb = v.stat().st_size / (1024 * 1024)
        print(f"  [{i:2d}] {v.name}  ({size_mb:,.1f} MB)")
    choice = ask("\nConvert [a]ll, [n]one, or numbers like 1,3,5? [a]: ").lower()
    if choice in ("", "a", "all"):
        return list(videos)
    if choice in ("n", "none", "q", "quit"):
        print("Nothing to do. Bye!")
        sys.exit(0)
    selected = []
    for part in choice.split(","):
        part = part.strip()
        if not part.isdigit():
            continue
        idx = int(part)
        if 1 <= idx <= len(videos):
            selected.append(videos[idx - 1])
    if not selected:
        print(f"{RED}No valid selection — aborting.{RESET}")
        sys.exit(1)
    return selected


# --------------------------------------------------------------------------- #
# Conversion
# --------------------------------------------------------------------------- #

def build_command(
    src: Path, out: Path, codec: str, av1_encoder: str | None,
    preset: str, crf: int, copy_audio: bool,
) -> list[str]:
    cmd = [
        "ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
        "-i", str(src),
        "-map", "0:v:0", "-map", "0:a?",
    ]
    if codec == "hevc":
        cmd += ["-c:v", "libx265", "-preset", preset, "-crf", str(crf), "-tag:v", "hvc1"]
    else:
        cmd += ["-c:v", av1_encoder]
        if av1_encoder == "libsvtav1":
            cmd += ["-preset", preset]
        else:
            cmd += ["-cpu-used", preset, "-row-mt", "1"]
        cmd += ["-crf", str(crf)]
    if copy_audio:
        cmd += ["-c:a", "copy"]
    else:
        cmd += ["-c:a", "aac", "-b:a", AUDIO_BITRATE]
    cmd += ["-movflags", "+faststart", "-progress", "pipe:1", "-nostats", str(out)]
    return cmd


def render_progress(percent: float, elapsed: float) -> str:
    width = 30
    filled = int(width * percent / 100)
    bar = "█" * filled + "░" * (width - filled)
    return f"\r{BOLD}{bar}{RESET} {percent:5.1f}%  {elapsed:5.1f}s"


def run_ffmpeg(
    cmd: list[str], duration: float | None, label: str,
    progress=None,
) -> tuple[str, str]:
    """Run ffmpeg. Returns ('ok', '') or ('failed'|'interrupted', detail).

    progress(percent_or_None, elapsed_seconds) is called periodically when given;
    otherwise an inline \r progress bar is drawn for CLI use.
    """
    try:
        proc = subprocess.Popen(
            cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, bufsize=1,
        )
    except OSError as e:
        return "failed", f"could not start ffmpeg: {e}"

    start = time.monotonic()
    last_render = 0.0
    out_time = 0.0
    interrupted = False
    try:
        for line in proc.stdout:  # type: ignore[union-attr]
            line = line.strip()
            if line.startswith("out_time_us=") or line.startswith("out_time_ms="):
                try:
                    out_time = int(line.split("=", 1)[1]) / 1_000_000
                except ValueError:
                    pass
            now = time.monotonic()
            if now - last_render >= 0.15:
                last_render = now
                elapsed = now - start
                if progress is not None:
                    progress(out_time / duration * 100.0 if duration else None, elapsed)
                elif duration:
                    percent = min(100.0, out_time / duration * 100.0)
                    sys.stdout.write(render_progress(percent, elapsed))
                else:
                    sys.stdout.write(f"\r{BOLD}encoding…{RESET} {elapsed:5.1f}s")
                sys.stdout.flush()
        if progress is None:
            sys.stdout.write("\r" + " " * 60 + "\r")
            sys.stdout.flush()
        proc.wait()
    except KeyboardInterrupt:
        interrupted = True
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()

    if interrupted:
        return "interrupted", f"{label} was not finished"
    if proc.returncode != 0:
        stderr = proc.stderr.read() if proc.stderr else ""
        lines = stderr.strip().splitlines()
        tail = "\n".join(lines[-12:]) if lines else "(no error output)"
        return "failed", tail
    return "ok", ""


def convert_core(
    src: Path, out_dir: Path, codec: str, av1_encoder: str | None,
    preset: str, crf: int, force: bool, copy_audio: bool,
    progress=None, decide=None,
) -> tuple[str, str]:
    """Shared conversion logic for CLI and TUI.

    Returns (status, note) with status in
    'ok' | 'already' | 'exists' | 'failed' | 'interrupted'.
    progress(percent_or_None, elapsed) streams live updates; decide(prompt, default)
    asks the user when given, otherwise skips are automatic.
    """
    codec_upper = codec.upper()
    out_path = out_dir / f"{src.stem.rstrip()}.mp4"
    duration, src_codec = probe_video(src)

    # Source is already the target codec — converting would only lose quality.
    if src_codec == codec:
        if not force:
            if decide is None:
                return "already", f"already {codec_upper}"
            choice = decide(
                f"{src.name} is already {codec_upper} — re-encoding loses quality. "
                f"[s]kip/[r]e-encode anyway [s]: ", "s",
            ).lower()
            if choice not in ("r", "re", "re-encode", "y", "yes"):
                return "already", f"already {codec_upper}"

    if out_path.exists() and not force:
        if decide is None:
            return "exists", f"{out_path.name} already exists"
        choice = decide(f"{out_path.name} already exists. [s]kip/[o]verwrite [s]: ", "s").lower()
        if choice not in ("o", "overwrite", "y", "yes"):
            return "exists", f"{out_path.name} already exists"

    cmd = build_command(src, out_path, codec, av1_encoder, preset, crf, copy_audio)
    status, note = run_ffmpeg(cmd, duration, src.name, progress)
    if status != "ok":
        # ffmpeg may have left a partial/corrupt file behind — remove it.
        try:
            out_path.unlink(missing_ok=True)
        except OSError:
            pass
        return status, note
    return "ok", out_path.name


def convert_one(
    src: Path, out_dir: Path, codec: str, av1_encoder: str | None,
    preset: str, crf: int, args: argparse.Namespace,
) -> str:
    """CLI variant of convert_core with human-readable output."""
    size_mb = src.stat().st_size / (1024 * 1024)
    print(f"{BOLD}▶ {src.name}{RESET}  ({size_mb:,.1f} MB)")

    decide = (lambda prompt, default: ask(prompt)) if sys.stdin.isatty() else None
    status, note = convert_core(
        src, out_dir, codec, av1_encoder, preset, crf,
        force=args.force, copy_audio=args.copy_audio,
        progress=None, decide=decide,
    )
    if status == "ok":
        new_mb = (out_dir / note).stat().st_size / (1024 * 1024)
        print(f"{GREEN}[ok] {note}  ({new_mb:,.1f} MB){RESET}")
        return "ok"
    if status == "failed":
        print(f"{RED}ffmpeg failed for {src.name}:{RESET}\n{note}")
        return "failed"
    if status == "interrupted":
        print(f"{YELLOW}Interrupted — {note}.{RESET}")
        return "failed"
    if status == "already":
        print(f"{YELLOW}[skip] {src.name} is already {codec.upper()} — nothing to convert "
              f"(use --force to re-encode anyway, or --codec hevc for the other format).{RESET}")
        return "already"
    print(f"{YELLOW}[skip] {note} (use --force to overwrite).{RESET}")
    return "exists"


# --------------------------------------------------------------------------- #
# Image compression (AVIF)
# --------------------------------------------------------------------------- #

def build_image_command(
    src: Path, out: Path, av1_encoder: str, preset: str, crf: int, pix_fmt: str | None,
) -> list[str]:
    cmd = [
        "ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
        "-i", str(src),
    ]
    if pix_fmt in MAY_HAVE_ALPHA_PIXFMTS:
        # ffmpeg's AVIF muxer can't store alpha — flatten transparency onto white
        # so transparent areas don't come out as garbage.
        cmd += ["-vf", "split[a][b];[b]drawbox=c=white:t=fill[bg];"
                "[a][bg]overlay=shortest=1:format=auto"]
    cmd += ["-c:v", av1_encoder]
    if av1_encoder == "libsvtav1":
        cmd += ["-preset", preset]
    else:
        cmd += ["-cpu-used", preset, "-row-mt", "1"]
    cmd += ["-crf", str(crf), "-still-picture", "1", "-strict", "-2"]
    cmd += ["-progress", "pipe:1", "-nostats", str(out)]
    return cmd


def convert_image_core(
    src: Path, out_dir: Path, av1_encoder: str, preset: str, crf: int,
    force: bool, progress=None, decide=None,
) -> tuple[str, str]:
    """Shared AVIF compression for CLI and TUI.

    Returns (status, note) with status in
    'ok' | 'already' | 'exists' | 'failed' | 'interrupted'.
    """
    out_path = out_dir / f"{src.stem.rstrip()}.avif"

    if src.suffix.lower().rstrip() == ".avif":
        return "already", "already AVIF"

    if out_path.exists() and not force:
        if decide is None:
            return "exists", f"{out_path.name} already exists"
        choice = decide(f"{out_path.name} already exists. [s]kip/[o]verwrite [s]: ", "s").lower()
        if choice not in ("o", "overwrite", "y", "yes"):
            return "exists", f"{out_path.name} already exists"

    pix_fmt = probe_pix_fmt(src)
    flattened = pix_fmt in MAY_HAVE_ALPHA_PIXFMTS
    cmd = build_image_command(src, out_path, av1_encoder, preset, crf, pix_fmt)
    status, note = run_ffmpeg(cmd, None, src.name, progress)
    if status != "ok":
        try:
            out_path.unlink(missing_ok=True)
        except OSError:
            pass
        return status, note
    if flattened:
        note = f"{out_path.name} (transparency flattened to white)"
    else:
        note = out_path.name
    return "ok", note


def convert_one_image(
    src: Path, out_dir: Path, av1_encoder: str, preset: str, crf: int,
    args: argparse.Namespace,
) -> str:
    """CLI variant of convert_image_core with human-readable output."""
    size_mb = src.stat().st_size / (1024 * 1024)
    print(f"{BOLD}▶ {src.name}{RESET}  ({size_mb:,.1f} MB)")

    decide = (lambda prompt, default: ask(prompt)) if sys.stdin.isatty() else None
    status, note = convert_image_core(
        src, out_dir, av1_encoder, preset, crf,
        force=args.force, progress=None, decide=decide,
    )
    if status == "ok":
        out_path = out_dir / f"{src.stem.rstrip()}.avif"
        new_mb = out_path.stat().st_size / (1024 * 1024)
        print(f"{GREEN}[ok] {note}  ({new_mb:,.1f} MB){RESET}")
        return "ok"
    if status == "failed":
        print(f"{RED}ffmpeg failed for {src.name}:{RESET}\n{note}")
        return "failed"
    if status == "interrupted":
        print(f"{YELLOW}Interrupted — {note}.{RESET}")
        return "failed"
    if status == "already":
        print(f"{YELLOW}[skip] {src.name} is already AVIF — nothing to compress "
              f"(use --force to re-encode anyway).{RESET}")
        return "already"
    print(f"{YELLOW}[skip] {note} (use --force to overwrite).{RESET}")
    return "exists"


# --------------------------------------------------------------------------- #
# Minimal TUI
# --------------------------------------------------------------------------- #

class Tui:
    """Keyboard-driven, full-screen wizard: mode → files → codec → preset → CRF → convert."""

    def __init__(self, videos, images, encoders, avif_ok, args, cwd):
        self.videos = videos
        self.images = images
        self.encoders = encoders
        self.avif_ok = avif_ok
        self.args = args
        self.cwd = cwd
        self.av1_enc = pick_av1_encoder(encoders)
        self.codec_options = []
        if "libx265" in encoders:
            self.codec_options.append("hevc")
        if self.av1_enc:
            self.codec_options.append("av1")
        # Only offer modes that are possible AND have files in this folder,
        # so e.g. a folder of only images skips the mode screen entirely.
        self.mode_options = []
        if self.codec_options and self.videos:
            self.mode_options.append("video")
        if self.av1_enc and self.avif_ok and self.images:
            self.mode_options.append("image")
        self.screen = "mode" if len(self.mode_options) > 1 else "files"
        self.cursor = 0
        self.selected: set[int] = set()
        self.info: dict[int, tuple[str, float]] = {}
        self.probed = False
        preferred = "image" if args.images else "video"
        self.mode = preferred if preferred in self.mode_options else self.mode_options[0]
        self._init_mode_state(self.mode)
        if self.screen == "mode":
            self.cursor = self.mode_options.index(self.mode)
        self.jobs = []
        self.exit_requested = False

    def _mode_files_for(self, mode):
        return self.images if mode == "image" else self.videos

    def _mode_files(self):
        return self._mode_files_for(self.mode)

    def _init_mode_state(self, mode):
        """(Re)initialise codec/preset/selection state for the given mode."""
        self.mode = mode
        if mode == "image":
            self.codec = "av1"
        else:
            self.codec = self.args.codec if self.args.codec in self.codec_options else self.codec_options[0]
        self._reset_for_codec()
        self.selected = set(range(len(self._mode_files())))
        self.cursor = 0
        self.scroll = 0
        self.probed = False
        self.info = {}

    # -- setup ------------------------------------------------------------- #

    def _preset_list(self) -> list[str]:
        if self.codec == "hevc":
            return list(X265_PRESETS)
        if self.av1_enc == "libsvtav1":
            return [str(n) for n in range(SVT_PRESET_MIN, SVT_PRESET_MAX + 1)]
        return [str(n) for n in range(AOM_PRESET_MIN, AOM_PRESET_MAX + 1)]

    def _default_preset(self) -> str:
        if self.codec == "hevc":
            return X265_DEFAULT_PRESET
        return str(SVT_DEFAULT_PRESET if self.av1_enc == "libsvtav1" else AOM_DEFAULT_PRESET)

    def _default_crf(self) -> int:
        return X265_DEFAULT_CRF if self.codec == "hevc" else AV1_DEFAULT_CRF

    def _reset_for_codec(self):
        self.preset_list = self._preset_list()
        if self.args.preset is not None and self.args.preset in self.preset_list:
            self.preset = self.args.preset
        else:
            self.preset = self._default_preset()
        self.preset_idx = self.preset_list.index(self.preset)
        self.crf_lo, self.crf_hi = (
            (X265_CRF_MIN, X265_CRF_MAX) if self.codec == "hevc" else (AV1_CRF_MIN, AV1_CRF_MAX)
        )
        if self.args.crf is not None:
            self.crf = max(self.crf_lo, min(self.crf_hi, self.args.crf))
        else:
            self.crf = self._default_crf()

    def _ensure_probed(self):
        if self.probed:
            return
        files = self._mode_files()
        if self.mode == "image":
            # images need no probing — just sizes
            self.info = {i: ("", f.stat().st_size / (1024 * 1024)) for i, f in enumerate(files)}
            self.probed = True
            return
        self.paint([self._title("Vox"), "", " Scanning video files…"])
        for i, v in enumerate(files):
            _, codec = probe_video(v)
            size = v.stat().st_size / (1024 * 1024)
            self.info[i] = (codec or "?", size)
        self.probed = True

    # -- low-level terminal helpers ---------------------------------------- #

    def run(self):
        old = None
        try:
            old = termios.tcgetattr(sys.stdin.fileno())
            _tty.setcbreak(sys.stdin.fileno())
            # cbreak turns off ISIG; re-enable it so Ctrl+C aborts a running encode.
            mode = termios.tcgetattr(sys.stdin.fileno())
            mode[_LFLAG] |= termios.ISIG
            termios.tcsetattr(sys.stdin.fileno(), termios.TCSADRAIN, mode)
            sys.stdout.write("\x1b[?1049h\x1b[?25l")  # alternate screen, hide cursor
            sys.stdout.flush()
            self._loop()
        except KeyboardInterrupt:
            pass
        finally:
            sys.stdout.write("\x1b[?25h\x1b[?1049l")
            sys.stdout.flush()
            if old is not None:
                try:
                    termios.tcsetattr(sys.stdin.fileno(), termios.TCSADRAIN, old)
                except termios.error:
                    pass

    def paint(self, lines):
        cols, _ = shutil.get_terminal_size((80, 24))
        out = ["\x1b[2J\x1b[H"]
        for ln in lines:
            out.append(ln if len(ln) <= cols - 1 else ln[: cols - 1])
        sys.stdout.write("\n".join(out))
        sys.stdout.flush()

    @staticmethod
    def _title(text):
        return f" {BOLD}{text}{RESET}"

    @staticmethod
    def hl(text, on=False):
        return f"\x1b[7m{text}\x1b[0m" if on else text

    def get_key(self) -> str:
        # os.read on the raw fd: reading through sys.stdin.buffer would hide
        # follow-up bytes (arrow sequences) from select() in its own buffer.
        fd = sys.stdin.fileno()
        b = os.read(fd, 1)
        if not b:
            return "eof"
        c = b[0]
        if c == 0x1B:  # ESC
            r, _, _ = select.select([fd], [], [], 0.05)
            if r:
                seq = os.read(fd, 2)
                if seq == b"[A":
                    return "up"
                if seq == b"[B":
                    return "down"
                if seq == b"[C":
                    return "right"
                if seq == b"[D":
                    return "left"
                if seq == b"[H":
                    return "home"
                if seq == b"[F":
                    return "end"
            return "esc"
        if c == 0x03:
            return "ctrl_c"
        if c in (0x0D, 0x0A):
            return "enter"
        if c == 0x20:
            return "space"
        if 32 <= c <= 126:
            return chr(c).lower()
        return "?"

    def _loop(self):
        while not self.exit_requested:
            self._render()
            key = self.get_key()
            if key in ("ctrl_c", "eof"):
                self.exit_requested = True
                return
            handler = getattr(self, f"_key_{self.screen}", None)
            if handler:
                handler(key)

    # -- screens ----------------------------------------------------------- #

    def _render(self):
        {
            "mode": self._render_mode,
            "files": self._render_files,
            "codec": self._render_codec,
            "preset": self._render_preset,
            "crf": self._render_crf,
            "done": self._render_done,
        }.get(self.screen, lambda: None)()

    def _render_files(self):
        self._ensure_probed()
        # get_terminal_size returns (columns, lines) — mind the order!
        _, rows = shutil.get_terminal_size((80, 24))
        files = self._mode_files()
        # header: title, folder, count line, blank; footer: blank, hint
        header_n, footer_n = 4, 2
        visible = max(1, rows - header_n - footer_n)
        # keep the cursor inside the visible window; scroll as it moves
        if self.cursor < self.scroll:
            self.scroll = self.cursor
        elif self.cursor >= self.scroll + visible:
            self.scroll = self.cursor - visible + 1
        self.scroll = max(0, min(self.scroll, max(0, len(files) - visible)))

        verb = "compress" if self.mode == "image" else "convert"
        lines = [self._title(f"Vox — pick images to {verb}" if self.mode == "image" else "Vox — pick videos to convert")]
        lines.append(f" Folder: {self.cwd}")
        count = f" {len(self.selected)} of {len(files)} selected"
        if len(files) > visible:
            lo = self.scroll + 1
            hi = min(len(files), self.scroll + visible)
            count += f"  ·  showing {lo}–{hi} of {len(files)}"
        lines.append(count)
        lines.append("")
        for i in range(self.scroll, min(len(files), self.scroll + visible)):
            v = files[i]
            label, size = self.info.get(i, ("", 0.0))
            mark = "x" if i in self.selected else " "
            size_str = f"{size:,.1f} MB" if size >= 1.0 else f"{size * 1024:,.0f} KB"
            if label:
                text = f" [{mark}] {v.name}  ({label} · {size_str})"
            else:
                text = f" [{mark}] {v.name}  ({size_str})"
            if i == self.cursor:
                text = self.hl(text, True)
            lines.append(text)
        lines.append("")
        if self.mode == "image":
            lines.append(" ↑/↓ move · Space toggle · a all · n none · Enter compress · q quit")
        else:
            lines.append(" ↑/↓ move · Space toggle · a all · n none · Enter convert · q quit")
        self.paint(lines)

    def _key_files(self, key):
        if key == "up":
            self.cursor = max(0, self.cursor - 1)
        elif key == "down":
            self.cursor = min(len(self.videos) - 1, self.cursor + 1)
        elif key in ("home", "end"):
            self.cursor = 0 if key == "home" else len(self.videos) - 1
        elif key == "space":
            if self.cursor in self.selected:
                self.selected.discard(self.cursor)
            else:
                self.selected.add(self.cursor)
        elif key == "a":
            self.selected = set(range(len(self.videos)))
        elif key == "n":
            self.selected.clear()
        elif key == "enter":
            if not self.selected:
                self._flash("Nothing selected — press Space to pick files, or a to select all.")
            elif self.mode == "image":
                # images have a single target format (AVIF) — straight to preset
                self.screen = "preset"
                self.cursor = self.preset_idx
            else:
                self.screen = "codec"
                self.cursor = self.codec_options.index(self.codec)
        elif key in ("q", "esc"):
            if self.mode == "image":
                self.screen = "mode"
                self.cursor = self.mode_options.index("image")
            else:
                self.exit_requested = True

    def _flash(self, message):
        self.paint([self._title("Vox"), "", f" {YELLOW}{message}{RESET}", "", " Press any key…"])
        self.get_key()

    def _render_mode(self):
        lines = [self._title("Vox — what do you want to do?"), ""]
        for i, m in enumerate(self.mode_options):
            if m == "video":
                desc = f"Convert {len(self.videos)} video(s) → HEVC or AV1"
            else:
                desc = f"Compress {len(self.images)} image(s) → AVIF"
            text = f"  {desc}"
            if i == self.cursor:
                text = self.hl(text, True)
            lines.append(text)
        lines.append("")
        lines.append(" ↑/↓ move · Enter select · q quit")
        self.paint(lines)

    def _key_mode(self, key):
        if key == "up":
            self.cursor = max(0, self.cursor - 1)
        elif key == "down":
            self.cursor = min(len(self.mode_options) - 1, self.cursor + 1)
        elif key == "enter":
            mode = self.mode_options[self.cursor]
            if not self._mode_files_for(mode):
                noun = "videos" if mode == "video" else "images"
                self._flash(f"No {noun} found in this folder.")
                return
            self._init_mode_state(mode)
            self.screen = "files"
            self.cursor = 0
        elif key in ("q", "esc"):
            self.exit_requested = True

    def _render_codec(self):
        descs = {
            "hevc": "HEVC (H.265) — great compatibility, ~50% smaller than H.264",
            "av1": f"AV1 ({self.av1_enc}) — best compression, slower encodes, newer players",
        }
        lines = [self._title("Target codec"), ""]
        for i, c in enumerate(self.codec_options):
            mark = "•" if c == self.codec else " "
            text = f" {mark} {descs[c]}"
            if i == self.cursor:
                text = self.hl(text, True)
            lines.append(text)
        lines.append("")
        lines.append(" ↑/↓ move · Enter select · Esc back")
        self.paint(lines)

    def _key_codec(self, key):
        if key == "up":
            self.cursor = max(0, self.cursor - 1)
        elif key == "down":
            self.cursor = min(len(self.codec_options) - 1, self.cursor + 1)
        elif key == "enter":
            self.codec = self.codec_options[self.cursor]
            self._reset_for_codec()
            self.screen = "preset"
            self.cursor = self.preset_idx
        elif key == "esc":
            self.screen = "files"

    def _preset_label(self, p):
        if self.codec == "hevc":
            return f"{p}  (default)" if p == X265_DEFAULT_PRESET else p
        n = int(p)
        if self.av1_enc == "libsvtav1":
            return {
                0: "0  (slowest, best quality)",
                8: "8  (recommended balance)",
                13: "13  (fastest, largest)",
            }.get(n, str(n))
        return {
            0: "0  (slowest, best quality)",
            6: "6  (recommended)",
            11: "11  (fastest, largest)",
        }.get(n, str(n))

    def _render_preset(self):
        if self.codec == "hevc":
            title = "x265 preset — faster = quicker but bigger · slower = smaller files"
        elif self.av1_enc == "libsvtav1":
            title = "SVT-AV1 preset — 0 = slowest/best · 13 = fastest"
        else:
            title = "libaom-av1 cpu-used — 0 = slowest/best · 11 = fastest"
        lines = [self._title(title), ""]
        for i, p in enumerate(self.preset_list):
            text = f"  {self._preset_label(p)}"
            if i == self.cursor:
                text = self.hl(text, True)
            lines.append(text)
        lines.append("")
        lines.append(" ↑/↓ move · Enter select · Esc back")
        self.paint(lines)

    def _key_preset(self, key):
        if key == "up":
            self.cursor = max(0, self.cursor - 1)
        elif key == "down":
            self.cursor = min(len(self.preset_list) - 1, self.cursor + 1)
        elif key in ("home", "end"):
            self.cursor = 0 if key == "home" else len(self.preset_list) - 1
        elif key == "enter":
            self.preset = self.preset_list[self.cursor]
            self.screen = "crf"
        elif key == "esc":
            if self.mode == "image":
                self.screen = "files"
            else:
                self.screen = "codec"
                self.cursor = self.codec_options.index(self.codec)

    def _render_crf(self):
        lo, hi = self.crf_lo, self.crf_hi
        hint = "HEVC: typical 18-28" if self.codec == "hevc" else "AV1: typical 24-40"
        scale_w = min(40, shutil.get_terminal_size((80, 24)).columns - 10)
        span = hi - lo
        pos = int((self.crf - lo) / span * scale_w) if span else 0
        bar = "-" * pos + "▲" + "-" * max(0, scale_w - pos - 1)
        lines = [
            self._title("Quality (CRF) — lower = better, bigger file"),
            "",
            f"  value: {BOLD}{self.crf}{RESET}    ({hint})",
            f"  {bar}",
            f"  {lo}{' ' * max(0, scale_w - 2)}{hi}",
            "",
            " ←/→ adjust · Home/End jump · Enter confirm · Esc back",
        ]
        self.paint(lines)

    def _key_crf(self, key):
        if key == "left":
            self.crf = max(self.crf_lo, self.crf - 1)
        elif key == "right":
            self.crf = min(self.crf_hi, self.crf + 1)
        elif key == "home":
            self.crf = self.crf_lo
        elif key == "end":
            self.crf = self.crf_hi
        elif key == "enter":
            self.screen = "convert"
            self._run_conversions()
            self.screen = "done"
        elif key == "esc":
            self.screen = "preset"
            self.cursor = self.preset_idx

    # -- conversion -------------------------------------------------------- #

    def _run_conversions(self):
        out_dir = self.cwd / OUTPUT_DIR_NAME
        out_dir.mkdir(exist_ok=True)
        files = self._mode_files()
        indices = sorted(self.selected)
        self.jobs = [
            {"name": files[i].name, "status": "pending", "pct": 0.0, "elapsed": 0.0, "note": ""}
            for i in indices
        ]
        self.converted = self.already = self.exists = 0
        self.failed_names = []
        self.interrupted = False
        self._paint_convert()
        for n, i in enumerate(indices):
            src = files[i]
            self.jobs[n]["status"] = "encoding"
            self._paint_convert()

            def progress(pct, elapsed, _n=n):
                self.jobs[_n]["pct"] = pct if pct is not None else -1.0
                self.jobs[_n]["elapsed"] = elapsed
                self._paint_convert()

            if self.mode == "image":
                status, note = convert_image_core(
                    src, out_dir, self.av1_enc, self.preset, self.crf,
                    force=self.args.force, progress=progress, decide=None,
                )
            else:
                status, note = convert_core(
                    src, out_dir, self.codec, self.av1_enc, self.preset, self.crf,
                    force=self.args.force, copy_audio=self.args.copy_audio,
                    progress=progress, decide=None,
                )
            j = self.jobs[n]
            if status == "ok":
                j["status"] = "ok"
                j["note"] = note
                self.converted += 1
            elif status == "already":
                j["status"] = "skipped"
                j["note"] = note
                self.already += 1
            elif status == "exists":
                j["status"] = "skipped"
                j["note"] = note
                self.exists += 1
            elif status == "interrupted":
                j["status"] = "failed"
                j["note"] = "interrupted"
                self.interrupted = True
                self.failed_names.append(src.name)
                break
            else:
                j["status"] = "failed"
                j["note"] = note
                self.failed_names.append(src.name)
            self._paint_convert()

    def _paint_convert(self):
        cols = shutil.get_terminal_size((80, 24)).columns
        if self.mode == "image":
            codec_label = f"AVIF ({self.av1_enc})"
        else:
            codec_label = "HEVC (x265)" if self.codec == "hevc" else f"AV1 ({self.av1_enc})"
        lines = [self._title("Converting")]
        lines.append(f" {len(self.jobs)} file(s) → {codec_label} · preset {self.preset} · CRF {self.crf}")
        lines.append(f" Output: {self.cwd / OUTPUT_DIR_NAME}")
        lines.append("")
        bar_w = max(10, min(30, cols - 44))
        for j in self.jobs:
            name = j["name"]
            if j["status"] == "pending":
                line = f" · {name}"
            elif j["status"] == "encoding":
                elapsed = j.get("elapsed", 0.0)
                pct = j.get("pct", 0.0)
                if pct < 0:
                    line = f" ▸ {name}  [{elapsed:5.1f}s]"
                else:
                    filled = int(bar_w * pct / 100)
                    bar = "█" * filled + "░" * max(0, bar_w - filled)
                    line = f" ▸ {name}  {bar} {pct:5.1f}%  {elapsed:5.1f}s"
            elif j["status"] == "ok":
                line = f" ✓ {name}  → {j['note']}"
            elif j["status"] == "skipped":
                line = f" – {name}  ({j['note']})"
            else:
                line = f" ✗ {name}  {j['note']}"
            lines.append(line if len(line) <= cols - 1 else line[: cols - 1])
        lines.append("")
        if self.interrupted:
            lines.append(f" {YELLOW}Interrupted.{RESET} Press any key…")
        else:
            lines.append(f" {YELLOW}Encoding…  (Ctrl+C to abort){RESET}")
        self.paint(lines)

    def _render_done(self):
        lines = [self._title("Done")]
        lines.append(f" {self.converted} converted · {self.already + self.exists} skipped · {len(self.failed_names)} failed")
        if self.failed_names:
            lines.append("")
            lines.append(f" {RED}Failed:{RESET}")
            for n in self.failed_names:
                lines.append(f"   ✗ {n}")
        if self.interrupted:
            lines.append("")
            lines.append(f" {YELLOW}Interrupted — remaining files were not processed.{RESET}")
        lines.append(f" Output: {self.cwd / OUTPUT_DIR_NAME}")
        lines.append("")
        lines.append(" Press any key to exit.")
        self.paint(lines)

    def _key_done(self, key):
        self.exit_requested = True


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #

def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        prog="vox",
        description="Convert videos to HEVC (H.265) or AV1, or compress images to AVIF, with ffmpeg.",
        epilog=(
            "examples:\n"
            "  python vox.py                            interactive TUI (mode → files → codec → preset → CRF)\n"
            "  python vox.py --all --codec hevc         convert every video to HEVC, no TUI\n"
            "  python vox.py movie.mkv --codec av1 --preset 8 --crf 30\n"
            "  python vox.py --images                   interactive TUI, compress images to AVIF\n"
            "  python vox.py --all --images --crf 34    compress every image to AVIF, no TUI\n"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument("files", nargs="*", help="specific files to convert (default: interactive selection)")
    p.add_argument("--codec", choices=["hevc", "av1"], help="target codec (default: ask)")
    p.add_argument("--preset", help="quality preset: x265 name (e.g. medium, slow) or AV1 number (0-13)")
    p.add_argument("--crf", type=int, help="quality: lower = better, bigger file (defaults: HEVC 24, AV1/AVIF 30)")
    p.add_argument("--all", action="store_true", help="convert every video in the folder without asking")
    p.add_argument("--images", action="store_true", help="compress images to AVIF instead of converting videos")
    p.add_argument("--force", action="store_true", help="overwrite files that already exist in Vox Output")
    p.add_argument("--copy-audio", action="store_true", help="copy the original audio stream instead of re-encoding to AAC")
    return p.parse_args()


def main() -> int:
    args = parse_args()

    if shutil.which("ffmpeg") is None:
        print(f"{RED}ffmpeg was not found on your PATH.{RESET}")
        print("Install it first, e.g.  sudo apt install ffmpeg   (Debian/Ubuntu)")
        return 1

    cwd = Path.cwd()
    videos = discover_videos(cwd)
    images = discover_images(cwd)
    # Explicit image files (e.g. `vox.py photo.png`) switch to image mode automatically.
    image_mode = args.images or bool(args.files) and all(
        Path(f).suffix.lower().rstrip() in IMAGE_EXTENSIONS for f in args.files
    )
    # A folder with only images shouldn't error — fall back to image mode.
    if not image_mode and not videos and images:
        image_mode = True

    if image_mode:
        if not images:
            print(f"{YELLOW}No image files found in {cwd}{RESET}")
            print("Supported extensions: " + ", ".join(sorted(IMAGE_EXTENSIONS)))
            return 1
    else:
        if not videos:
            print(f"{YELLOW}No video files found in {cwd}{RESET}")
            print("Supported extensions: " + ", ".join(sorted(VIDEO_EXTENSIONS)))
            return 1

    encoders = available_encoders()
    av1_enc = pick_av1_encoder(encoders)
    avif_ok = has_avif_muxer()

    # Full-screen TUI when launched interactively with no explicit targets.
    use_tui = (
        _TUI_AVAILABLE
        and not args.files
        and not args.all
        and sys.stdin.isatty()
        and getattr(sys.stdout, "isatty", lambda: False)()
    )
    if use_tui:
        if image_mode and (not av1_enc or not avif_ok):
            print(f"{RED}AVIF compression needs an AV1 encoder and AVIF muxer, but this ffmpeg "
                  f"build is missing one of them.{RESET}")
            return 1
        if not ("libx265" in encoders or av1_enc):
            print(f"{RED}No usable encoder found. ffmpeg is installed but lacks libx265 and AV1 encoders.{RESET}")
            print("On Debian/Ubuntu:  sudo apt install ffmpeg libx265-dev  (or use a static ffmpeg build)")
            return 1
        Tui(videos, images, encoders, avif_ok, args, cwd).run()
        return 0

    if image_mode:
        if not avif_ok:
            print(f"{RED}This ffmpeg build has no AVIF muxer — cannot write .avif files.{RESET}")
            return 1
        av1_encoder, preset, crf = resolve_image_settings(args, encoders)
        selected = select_files(args, images, cwd, "image")

        out_dir = cwd / OUTPUT_DIR_NAME
        out_dir.mkdir(exist_ok=True)

        print(f"\n{BOLD}Plan:{RESET} {len(selected)} image(s) → AVIF ({av1_encoder}), preset={preset}, CRF={crf}")
        print(f"Output folder: {out_dir}  (files keep their original names, .avif extension)\n")

        converted, already, exists, failed = 0, 0, 0, []
        for path in selected:
            status = convert_one_image(path, out_dir, av1_encoder, preset, crf, args)
            if status == "ok":
                converted += 1
            elif status == "failed":
                failed.append(path.name)
            elif status == "already":
                already += 1
            else:
                exists += 1
            print()

        skipped = already + exists
        if failed:
            print(f"{RED}Failed ({len(failed)}): {', '.join(failed)}{RESET}")
        print(f"{GREEN}Done. {converted} compressed, {skipped} skipped, {len(failed)} failed.{RESET}")
        if converted == 0 and already and not exists and not failed:
            print(f"{YELLOW}Tip: file(s) were skipped because they are already AVIF — nothing to compress.")
            print(f"      Use --force to re-encode anyway (not recommended, loses quality).{RESET}")
        print(f"{GREEN}Output: {out_dir}{RESET}")
        return 1 if failed else 0

    codec, av1_encoder, preset, crf = resolve_settings(args, encoders)
    selected = select_files(args, videos, cwd)

    out_dir = cwd / OUTPUT_DIR_NAME
    out_dir.mkdir(exist_ok=True)

    codec_label = "HEVC (x265)" if codec == "hevc" else f"AV1 ({av1_encoder})"
    print(f"\n{BOLD}Plan:{RESET} {len(selected)} file(s) → {codec_label}, preset={preset}, CRF={crf}")
    print(f"Output folder: {out_dir}  (files keep their original names)\n")

    converted, already, exists, failed = 0, 0, 0, []
    for path in selected:
        status = convert_one(path, out_dir, codec, av1_encoder, preset, crf, args)
        if status == "ok":
            converted += 1
        elif status == "failed":
            failed.append(path.name)
        elif status == "already":
            already += 1
        else:
            exists += 1
        print()

    skipped = already + exists
    if failed:
        print(f"{RED}Failed ({len(failed)}): {', '.join(failed)}{RESET}")
    print(f"{GREEN}Done. {converted} converted, {skipped} skipped, {len(failed)} failed.{RESET}")
    if converted == 0 and already and not exists and not failed:
        print(f"{YELLOW}Tip: file(s) were skipped because they are already {codec.upper()} — nothing to convert.")
        print(f"      Try --codec hevc for the other format, or --force to re-encode anyway (not recommended, loses quality).{RESET}")
    print(f"{GREEN}Output: {out_dir}{RESET}")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
