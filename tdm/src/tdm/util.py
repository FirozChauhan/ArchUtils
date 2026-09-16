"""Shared helpers: sizes, filenames, retry-after, notify, disk, fallocate."""
from __future__ import annotations

import hashlib
import os
import posixpath
import re
import shutil
import subprocess
import time
from datetime import datetime, timezone
from email.message import Message
from email.utils import parsedate_to_datetime
from pathlib import Path
from urllib.parse import unquote, urlparse

SIZE_RE = re.compile(r"^\s*(\d+(?:\.\d+)?)\s*([KMGT]?)(?:[iI]?[bB])?\s*$")


def parse_size(s: str | int | None) -> int:
    if s is None or s == "":
        return 0
    if isinstance(s, int):
        return s
    m = SIZE_RE.match(str(s).upper().replace("IB", "B"))
    if not m:
        raise ValueError(f"bad size: {s!r} (try 500K, 1M, 4.2M)")
    num, unit = float(m.group(1)), m.group(2)
    mult = {"": 1, "K": 1024, "M": 1024**2, "G": 1024**3, "T": 1024**4}[unit]
    return int(num * mult)


def human(n: float) -> str:
    for u in ("B", "K", "M", "G", "T"):
        if n < 1024 or u == "T":
            return f"{n:.1f}{u}" if u != "B" else f"{int(n)}B"
        n /= 1024
    return f"{n:.1f}T"


def parse_retry_after(value: str | None, default: float) -> float:
    if not value:
        return default
    value = value.strip()
    if value.isdigit():
        return float(value)
    try:
        dt = parsedate_to_datetime(value)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return max(0.0, (dt - datetime.now(timezone.utc)).total_seconds())
    except Exception:
        return default


def filename_from_cd(cd: str | None, url: str) -> str | None:
    if cd:
        try:
            m = Message()
            m["content-disposition"] = cd
            fn = m.get_filename()
            if fn:
                return sanitize_filename(unquote(fn))
        except Exception:
            pass
    # fallback: URL basename
    try:
        path = urlparse(url).path
        base = posixpath.basename(path.rstrip("/"))
        if base:
            return sanitize_filename(unquote(base))
    except Exception:
        pass
    return None


def sanitize_filename(name: str) -> str:
    name = posixpath.basename(name.replace("\\", "/")).strip().lstrip(".")
    name = re.sub(r"[\x00-\x1f/]", "_", name)
    return name[:200] or "file"


def ensure_same_dir_part(final: Path) -> tuple[Path, Path]:
    part = final.with_name(final.name + ".part")
    state = final.with_name(final.name + ".part.json")
    return part, state


def check_disk(path: Path, need: int) -> None:
    d = path.parent
    d.mkdir(parents=True, exist_ok=True)
    free = shutil.disk_usage(d).free
    if need > 0 and free < need * 1.05:
        raise OSError(f"low disk: need ~{human(need)}, free {human(free)} in {d}")


def try_preallocate(fileno: int, size: int) -> None:
    if size <= 0:
        return
    try:
        if hasattr(os, "posix_fallocate"):
            os.posix_fallocate(fileno, 0, size)
            return
    except OSError:
        pass
    try:  # fallback: sparse truncate
        os.ftruncate(fileno, size)
    except OSError:
        pass


def fsync_dir(d: Path) -> None:
    try:
        fd = os.open(str(d), os.O_RDONLY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)
    except OSError:
        pass


def sha256_of(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for b in iter(lambda: f.read(1024 * 1024), b""):
            h.update(b)
    return h.hexdigest()


def verify_checksum(path: Path, spec: str) -> None:
    # spec like "sha256:abc..." or "abc..."
    algo, _, want = spec.partition(":")
    if not want:
        want, algo = algo, "sha256"
    algo = algo.lower() or "sha256"
    h = hashlib.new(algo)
    with open(path, "rb") as f:
        for b in iter(lambda: f.read(1024 * 1024), b""):
            h.update(b)
    if h.hexdigest().lower() != want.strip().lower():
        raise ValueError(f"checksum mismatch ({algo}): expected {want}, got {h.hexdigest()}")


def notify(title: str, body: str) -> None:
    if shutil.which("notify-send") is None:
        return
    try:
        subprocess.run(["notify-send", "-a", "tdm", title, body], timeout=5, check=False)
    except Exception:
        pass


def origin_of(url: str) -> str:
    p = urlparse(url)
    return f"{p.scheme}://{p.netloc}"
