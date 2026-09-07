"""Wither — junk detection & removal engine (no TUI dependency)."""

from __future__ import annotations

import os
import queue
import re
import shutil
import stat
import subprocess
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path

sys.setrecursionlimit(10000)

VERSION = "2.0.0"

HOME = Path.home()


class Risk(Enum):
    SAFE = "low"
    MEDIUM = "medium"
    HIGH = "high"


@dataclass
class JunkItem:
    path: Path
    desc: str
    risk: Risk = Risk.MEDIUM
    size: int = -1  # bytes; -1 = unknown
    mtime: float | None = field(default=None)
    cleaned: bool = field(default=False)
    display: str | None = field(default=None)
    meta: dict = field(default_factory=dict)

    @property
    def exists(self) -> bool:
        return os.path.lexists(self.path)

    def label(self) -> str:
        p = str(self.path)
        try:
            p = p.replace(str(HOME), "~")
        except Exception:
            pass
        return p

    def display_label(self) -> str:
        return self.display or self.label()

    def size_label(self) -> str:
        return "unknown" if self.size < 0 else human(self.size)


@dataclass
class Category:
    id: str
    label: str
    icon: str
    needs_root: bool = False
    default_risk: Risk = Risk.MEDIUM
    items: list[JunkItem] = field(default_factory=list)
    skipped_reason: str | None = None


@dataclass
class SystemInfo:
    root: bool = field(default_factory=lambda: hasattr(os, "geteuid") and os.geteuid() == 0)


def human(n: int) -> str:
    if n is None or n < 0:
        return "?"
    n = float(n)
    unit = "B"
    for unit in ("B", "KB", "MB", "GB", "TB", "PB"):
        if n < 1024 or unit == "PB":
            if unit == "B":
                return f"{int(n)} B"
            return f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} {unit}"


_WORKERS = max(4, min(16, (os.cpu_count() or 4) * 2))
_SIZE_EXECUTOR = ThreadPoolExecutor(max_workers=_WORKERS, thread_name_prefix="wither-size")
_SCAN_EXECUTOR = ThreadPoolExecutor(max_workers=_WORKERS, thread_name_prefix="wither-scan")


def set_workers(n: int) -> None:
    """Override the worker count (CLI --jobs)."""
    global _WORKERS, _SIZE_EXECUTOR, _SCAN_EXECUTOR
    n = max(1, int(n))
    _WORKERS = n
    _SIZE_EXECUTOR = ThreadPoolExecutor(max_workers=n, thread_name_prefix="wither-size")


def _dir_size_serial(path: Path) -> tuple[int, int]:
    """Serial recursive size walk — used beneath dir_size's top level."""
    total = 0
    n = 0
    try:
        with os.scandir(path) as it:
            for entry in it:
                try:
                    if entry.is_symlink():
                        continue
                    st = entry.stat(follow_symlinks=False)
                    if entry.is_dir(follow_symlinks=False):
                        s, m = _dir_size_serial(entry.path)
                        total += s
                        n += m
                    else:
                        total += st.st_size
                        n += 1
                except OSError:
                    continue
    except OSError:
        pass
    return total, n


def dir_size(path: Path) -> tuple[int, int]:
    """Return (total_bytes, n_entries), ignoring permission errors & symlinks.

    Measures the immediate children concurrently (I/O bound), then walks
    each subtree serially, so big trees and many entries are handled fast."""
    total = 0
    n = 0
    dirs: list[str] = []
    try:
        with os.scandir(path) as it:
            for entry in it:
                try:
                    if entry.is_symlink():
                        continue
                    if entry.is_dir(follow_symlinks=False):
                        dirs.append(entry.path)
                    else:
                        total += entry.stat(follow_symlinks=False).st_size
                        n += 1
                except OSError:
                    continue
    except OSError:
        pass
    if not dirs:
        return total, n
    if len(dirs) == 1:
        s, m = _dir_size_serial(Path(dirs[0]))
        return total + s, n + m
    futures = [_SIZE_EXECUTOR.submit(_dir_size_serial, Path(d)) for d in dirs]
    for f in futures:
        s, m = f.result()
        total += s
        n += m
    return total, n


def older_than_days(st: os.stat_result, days: float) -> bool:
    return time.time() - st.st_mtime > days * 86400.0


_PSEUDO_FS = {
    "proc", "sysfs", "devtmpfs", "devpts", "securityfs", "cgroup", "cgroup2",
    "debugfs", "tracefs", "fusectl", "configfs", "pstore", "hugetlbfs", "mqueue",
    "bpf", "autofs", "efivarfs", "binfmt_misc", "rpc_pipefs", "nsfs", "ramfs",
    "sockfs", "pipefs", "tmpfs", "overlay", "squashfs", "erofs", "iso9660",
}


def _is_pseudo_fstype(fstype: str) -> bool:
    if fstype in _PSEUDO_FS:
        return True
    if fstype.startswith("fuse") and fstype != "fuseblk":
        return True
    return False


_SAFETY_BASES = [
    HOME,
    Path("/tmp"),
    Path("/var/tmp"),
    Path("/var/cache"),
    Path("/var/log"),
    Path("/var/crash"),
]

_FORBIDDEN_ROOTS = {Path("/"), Path("/boot"), Path("/etc"), Path("/root"), Path("/usr"), Path("/var")}

_REFUSE_HOME = {
    ".cache",
    ".config",
    ".local",
    ".ssh",
    ".gnupg",
    ".var",
    ".cargo",
    ".mozilla",
    ".vim",
    ".gitconfig",
    ".bashrc",
    ".zshrc",
    ".profile",
}

# never delete anything inside dependency/library trees
_DATA_MARKERS = (
    "/site-packages/",
    "/node_modules/",
    "/venv/",
    "/.venv/",
    "/vendor/",
    "/bower_components/",
)

# user document folders — never scanned for junk
_DATA_SAFE_DIRS = {
    "Desktop", "Documents", "Downloads", "Music", "Pictures", "Public",
    "Templates", "Videos",
}

# dot-directories at the top level of any user home (private data)
_PRIVATE_HOME_DIRS = {
    ".cache", ".config", ".local", ".ssh", ".gnupg", ".var", ".mozilla",
    ".cargo", ".rustup", ".nvm", ".npm", ".m2", ".gradle", ".vim",
    ".aws", ".azure", ".kube", ".password-store", ".gnupg",
}

_HOME_PARENTS = {Path("/home"), Path("/root")}


def _real_mount_roots() -> list[Path]:
    """Every real (non-virtual) filesystem mount, minus OS-critical trees."""
    roots = []
    try:
        with open("/proc/self/mountinfo") as f:
            for line in f:
                parts = line.split()
                if len(parts) < 10 or "-" not in parts:
                    continue
                if _is_pseudo_fstype(parts[parts.index("-") + 1]):
                    continue
                roots.append(Path(parts[4]))
    except OSError:
        roots = [Path("/")]
    forbidden = _FORBIDDEN_ROOTS - {Path("/")}
    out = [
        r for r in roots
        if r != Path("/")
        and not any(r == f or r.is_relative_to(f) for f in forbidden)
    ]
    return out


# Whole real mounts (/home, /opt, /srv, /mnt, /media, …) are cleanable;
# OS-critical trees are not.
_SAFETY_BASES += _real_mount_roots()


def _resolve(p: Path) -> Path:
    try:
        return p.resolve(strict=False)
    except OSError:
        return p


def assert_cleanable(p: Path) -> None:
    """Raise if path must never be removed. Also bans base dirs themselves."""
    p = Path(p)
    if p.name == "" or p in _FORBIDDEN_ROOTS or p.parent in _FORBIDDEN_ROOTS:
        raise PermissionError(f"refusing to touch protected path: {p}")

    rp = _resolve(p)

    # never touch libraries / dependency trees, no matter what a scan found
    for marker in _DATA_MARKERS:
        if marker in str(rp):
            raise PermissionError(f"refusing to touch library/dependency path: {p}")

    # protect private dot-dirs at the top of ANY user home (not just ours)
    if rp.parent in _HOME_PARENTS:
        if rp.name in _PRIVATE_HOME_DIRS:
            raise PermissionError(f"refusing to touch protected path: {p}")

    if rp in _FORBIDDEN_ROOTS or (rp.name in _REFUSE_HOME and rp.parent == HOME):
        raise PermissionError(f"refusing to touch protected path: {p}")

    if p.is_symlink():
        if not any(_resolve(p.parent) == b or _resolve(p.parent).is_relative_to(b) for b in _SAFETY_BASES):
            raise PermissionError(f"refusing to touch link outside bases: {p}")
        return

    in_base = any(rp == b or rp.is_relative_to(b) for b in _SAFETY_BASES)
    if not in_base:
        raise PermissionError(f"path not inside a known cleanable base: {p}")
    if any(rp == b for b in _SAFETY_BASES):
        raise PermissionError(f"refusing to remove base directory itself: {p}")


def clean_items(items: list[JunkItem]) -> tuple[int, int, list[str]]:
    """Delete items. Returns (bytes_freed, n_removed, errors)."""
    freed = 0
    removed = 0
    errors: list[str] = []
    for it in items:
        p = it.path
        try:
            assert_cleanable(p)
            if not os.path.lexists(p):
                it.cleaned = True
                continue
            if p.is_symlink():
                sz = -1
                p.unlink()
            elif p.is_dir():
                sz, _ = dir_size(p)
                shutil.rmtree(p)
            else:
                sz = p.stat().st_size
                p.unlink()
            it.cleaned = True
            removed += 1
            if sz > 0:
                freed += sz
        except Exception as exc:  # noqa: BLE001 — report and keep going
            errors.append(f"{str(p)}: {exc}")
    return freed, removed, errors


# ---------------------------------------------------------------- scanners


def scan_trash(system: SystemInfo) -> list[JunkItem]:
    items: list[JunkItem] = []
    base = HOME / ".local/share/Trash"
    if base.is_dir():
        sz, _ = dir_size(base)
        items.append(JunkItem(base, "Recycle bin / trash", Risk.SAFE, sz))
    return items


def scan_temp(system: SystemInfo, days: float = 2) -> list[JunkItem]:
    items: list[JunkItem] = []
    for base in (Path("/tmp"), Path("/var/tmp")):
        if not base.is_dir():
            continue
        try:
            with os.scandir(base) as it:
                for entry in it:
                    try:
                        st = entry.stat(follow_symlinks=False)
                    except OSError:
                        continue
                    if not older_than_days(st, days):
                        continue
                    if entry.is_symlink():
                        sz = -1
                    elif entry.is_dir(follow_symlinks=False):
                        sz, _ = dir_size(entry.path)
                    else:
                        sz = st.st_size
                    items.append(
                        JunkItem(
                            Path(entry.path),
                            f"aged temp (>{int(days)}d)",
                            Risk.MEDIUM,
                            sz,
                            st.st_mtime,
                        )
                    )
        except OSError:
            continue
    return items


def scan_thumbs(system: SystemInfo) -> list[JunkItem]:
    items: list[JunkItem] = []
    for base in (HOME / ".cache/thumbnails", HOME / ".thumbnails"):
        if base.is_dir():
            sz, _ = dir_size(base)
            items.append(JunkItem(base, "Thumbnail cache", Risk.SAFE, sz))
    return items


def _simple_cache(items: list[JunkItem], base: Path, desc: str, risk: Risk = Risk.SAFE) -> None:
    if base.is_dir():
        sz, _ = dir_size(base)
        items.append(JunkItem(base, desc, risk, sz))


def scan_dev_caches(system: SystemInfo) -> list[JunkItem]:
    items: list[JunkItem] = []
    _simple_cache(items, HOME / ".cache/pip", "pip wheel cache")
    _simple_cache(items, HOME / ".npm", "npm cache + logs")
    _simple_cache(items, HOME / ".cache/yarn", "yarn cache")
    _simple_cache(items, HOME / ".yarn/cache", "yarn berry cache")
    _simple_cache(items, HOME / ".pnpm-store", "pnpm store")
    _simple_cache(items, HOME / ".local/share/pnpm/store", "pnpm store")
    _simple_cache(items, HOME / ".cache/pnpm", "pnpm cache")
    _simple_cache(items, HOME / ".cargo/registry/cache", "cargo crate archives")
    _simple_cache(items, HOME / ".cargo/registry/src", "cargo extracted crates")
    _simple_cache(items, HOME / ".cache/go-build", "go build cache")
    _simple_cache(items, HOME / ".cache/gradle", "gradle cache")
    _simple_cache(items, HOME / ".gradle/caches", "gradle caches")
    _simple_cache(items, HOME / ".m2/repository", "maven dependencies", Risk.MEDIUM)
    for base in (HOME / ".cache/uv", HOME / ".cache/rye", HOME / ".cache/poetry"):
        _simple_cache(items, base, f"{base.name.lower()} cache")
    return items


def _browser_profiles(base: Path) -> list[Path]:
    """Yield profile dirs (those containing a Cache dir)."""
    out: list[Path] = []
    if not base.is_dir():
        return out
    try:
        for child in base.iterdir():
            if child.is_dir() and (child / "Cache").is_dir():
                out.append(child)
    except OSError:
        pass
    return out


def scan_browsers(system: SystemInfo) -> list[JunkItem]:
    items: list[JunkItem] = []
    firefox = HOME / ".mozilla/firefox"
    if firefox.is_dir():
        for profile in _browser_profiles(firefox):
            for sub in ("cache2", "startupCache", "Cache"):
                p = profile / sub
                if p.is_dir():
                    sz, _ = dir_size(p)
                    items.append(JunkItem(p, f"Firefox cache ({profile.name})", Risk.SAFE, sz))
        cache_root = HOME / ".cache/mozilla"
        if cache_root.is_dir():
            for profile in _browser_profiles(cache_root):
                sz, _ = dir_size(profile)
                items.append(JunkItem(profile, f"Firefox cache ({profile.name})", Risk.SAFE, sz))

    browsers = {
        "google-chrome": ("~/.config/google-chrome", "~/.cache/google-chrome"),
        "chromium": ("~/.config/chromium", "~/.cache/chromium"),
        "Brave-Browser": ("~/.config/BraveSoftware/Brave-Browser", "~/.cache/BraveSoftware/Brave-Browser"),
        "microsoft-edge": ("~/.config/microsoft-edge", "~/.cache/microsoft-edge"),
        "vivaldi": ("~/.config/vivaldi", "~/.cache/vivaldi"),
        "opera": ("~/.config/opera", "~/.cache/opera"),
    }
    for name, (cfg, cch) in browsers.items():
        for profile in _browser_profiles(Path(cfg)):
            c = profile / "Cache"
            if c.is_dir():
                sz, _ = dir_size(c)
                items.append(JunkItem(c, f"{name} Cache ({profile.name})", Risk.SAFE, sz))
            for sub in ("Code Cache", "GPUCache", "GrShaderCache"):
                p = profile / sub
                if p.is_dir():
                    sz, _ = dir_size(p)
                    items.append(JunkItem(p, f"{name} {sub} ({profile.name})", Risk.SAFE, sz))
        cc = Path(cch)
        if cc.is_dir():
            sz, _ = dir_size(cc)
            items.append(JunkItem(cc, f"{name} cache dir", Risk.SAFE, sz))
    return items


_KNOWN_CACHE_NAMES = {
    "thumbnails",
    "Thumbnails",
    "pip",
    "go-build",
    "yarn",
    "npm",
    "pnpm",
    "mozilla",
    "google-chrome",
    "chromium",
    "BraveSoftware",
    "microsoft-edge",
    "vivaldi",
    "opera",
    "electron",
    "gcr",
    "mesa_shader_cache",
    "uv",
    "rye",
    "poetry",
    "wine",
    "Code",
}


def scan_user_cache(system: SystemInfo) -> list[JunkItem]:
    items: list[JunkItem] = []
    base = HOME / ".cache"
    if not base.is_dir():
        return items
    try:
        children = sorted(base.iterdir())
    except OSError:
        return items
    for child in children:
        try:
            if not child.is_dir() or child.is_symlink():
                continue
        except OSError:
            continue
        if child.name in _KNOWN_CACHE_NAMES:
            continue  # handled elsewhere (or too risky to blanket-delete)
        sz, _ = dir_size(child)
        risk = Risk.SAFE if child.name.lower() in {"flatpak"} else Risk.MEDIUM
        items.append(JunkItem(child, "~/.cache entry", risk, sz))
    return items


def scan_crash(system: SystemInfo) -> list[JunkItem]:
    items: list[JunkItem] = []
    base = HOME / ".cache"
    if base.is_dir():
        try:
            for child in base.iterdir():
                if child.is_dir() and ("crash" in child.name.lower() or "Crash" in child.name):
                    sz, _ = dir_size(child)
                    items.append(JunkItem(child, "Crash reports", Risk.SAFE, sz))
        except OSError:
            pass
    return items


_BACKUP_TAIL = ("~", ".swp", ".swo", ".swx", ".bak", ".orig", ".rej", ".tmp", ".old")
_PRUNE_DIRS = {".cache", ".npm", ".cargo", ".mozilla", "node_modules", ".git", "site-packages",
               ".local", ".config", ".var", "target", "dist", "__pycache__", ".rustup", "snap",
               "Trash", ".ssh", ".gnupg", ".aws", ".azure", ".kube", ".password-store"}


def _is_backup(name: str) -> bool:
    return name.endswith(_BACKUP_TAIL)


def scan_editor_junk(system: SystemInfo, max_entries: int = 4000, max_seconds: float = 8) -> list[JunkItem]:
    by_dir: dict[Path, tuple[int, int]] = {}  # dir -> (bytes, count)
    start = time.time()
    home = HOME
    fs_map = _fs_map()
    for root, dirs, files in os.walk(home, topdown=True, followlinks=False):
        depth = root[len(str(home)):].count(os.sep)
        dirs[:] = [d for d in dirs if d not in _PRUNE_DIRS and depth < 7
                   and not _is_pseudo(Path(root) / d, fs_map)]
        for f in files:
            if time.time() - start > max_seconds:
                break
            if not _is_backup(f):
                continue
            fp = Path(root) / f
            try:
                st = fp.lstat()
            except OSError:
                continue
            if not st.st_size or not older_than_days(st, 7):
                continue
            total, count = by_dir.get(root, (0, 0))
            by_dir[root] = (total + st.st_size, count + 1)
            if count > max_entries:
                break
        if time.time() - start > max_seconds:
            break
    items = []
    for d, (sz, cnt) in by_dir.items():
        items.append(JunkItem(d, f"{cnt} backup/editor temp files", Risk.MEDIUM, sz))
    return items


def scan_empty_dirs(system: SystemInfo, age_days: float = 30, max_seconds: float = 8) -> list[JunkItem]:
    items: list[JunkItem] = []
    home = HOME
    start = time.time()
    fs_map = _fs_map()
    for root, dirs, files in os.walk(home, topdown=True, followlinks=False):
        if time.time() - start > max_seconds:
            break
        depth = root[len(str(home)):].count(os.sep)
        dirs[:] = [d for d in dirs if d not in _PRUNE_DIRS and depth < 4
                   and not _is_pseudo(Path(root) / d, fs_map)]
        for d in list(dirs):
            p = Path(root) / d
            try:
                if d.startswith(".") and d not in (".config", ".local"):
                    continue
                if any(p.iterdir()):
                    continue
                st = p.lstat()
            except OSError:
                continue
            if older_than_days(st, age_days):
                items.append(JunkItem(p, "Empty directory", Risk.MEDIUM, 0, st.st_mtime))
    return items


def scan_logs(system: SystemInfo, days: float = 7) -> list[JunkItem]:
    items: list[JunkItem] = []
    bases = (HOME / ".config", HOME / ".local/share")
    rotated = re.compile(r"\.([0-9]+|gz|old|old\.[0-9]+)$")
    for base in bases:
        if not base.is_dir():
            continue
        try:
            for root, dirs, files in os.walk(base, topdown=True, followlinks=False):
                dirs[:] = [d for d in dirs if d not in ("node_modules", ".git", "Trash")]
                if Path(root).name.lower() not in ("log", "logs"):
                    continue
                size = 0
                count = 0
                for f in files:
                    p = Path(root) / f
                    try:
                        st = p.lstat()
                    except OSError:
                        continue
                    if st.st_size and (older_than_days(st, days) or rotated.search(f)):
                        size += st.st_size
                        count += 1
                if count:
                    items.append(JunkItem(Path(root), f"{count} stale log files", Risk.MEDIUM, size))
        except OSError:
            continue
    return items


# ------------------------------------------------- system-level (root / docker)


def scan_apt_cache(system: SystemInfo) -> list[JunkItem]:
    items: list[JunkItem] = []
    if not system.root:
        return items
    for base in (Path("/var/cache/apt/archives"), Path("/var/cache/apt/archives/partial")):
        if base.is_dir():
            sz, _ = dir_size(base)
            items.append(JunkItem(base, "apt package cache", Risk.MEDIUM, sz))
    return items


def scan_system_logs(system: SystemInfo, days: float = 30) -> list[JunkItem]:
    items: list[JunkItem] = []
    if not system.root:
        return items
    rotated = re.compile(r"\.([0-9]+|gz|old)$")
    for root in (Path("/var/log"),):
        if not root.is_dir():
            continue
        try:
            entries = list(root.iterdir())
        except OSError:
            continue
        by_dir: dict[Path, tuple[int, int]] = {}
        for p in entries:
            try:
                if p.is_dir(follow_symlinks=False):
                    if p.name.startswith("."):
                        continue
                    for f in p.iterdir():
                        if f.is_file(follow_symlinks=False) and rotated.search(f.name):
                            sz, cnt = by_dir.get(p, (0, 0))
                            try:
                                sz += f.stat().st_size
                            except OSError:
                                pass
                            by_dir[p] = (sz, cnt + 1)
                    continue
                if not p.is_file(follow_symlinks=False) or not rotated.search(p.name):
                    continue
                sz, cnt = by_dir.get(root, (0, 0))
                try:
                    sz += p.stat().st_size
                except OSError:
                    pass
                by_dir[root] = (sz, cnt + 1)
            except OSError:
                continue
        for d, (sz, cnt) in by_dir.items():
            items.append(JunkItem(d, f"{cnt} rotated system logs", Risk.MEDIUM, sz))
    return items


def _run(cmd: list[str]) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, capture_output=True, text=True, timeout=180)


_SIZE_UNITS = {"": 1, "K": 1000, "M": 1000**2, "G": 1000**3, "T": 1000**4, "P": 1000**5}


def _parse_human_size(s: str) -> int:
    """Parse a human size like "0B", "24.5MB", "512kB", "1.2G" → bytes."""
    s = s.strip()
    if not s:
        return 0
    m = re.match(r"^([\d.]+)\s*([KMGTP]?)(?:i?B)?$", s, re.IGNORECASE)
    if not m:
        try:
            return int(float(s))
        except ValueError:
            return 0
    try:
        return int(float(m.group(1)) * _SIZE_UNITS[m.group(2).upper()])
    except (ValueError, KeyError):
        return 0


def _extract_size(text: str) -> int:
    """Find the first human size anywhere in text ("logs take up 1.2G")."""
    m = re.search(r"([\d.]+\s*[KMGTP]?i?B?)", text, re.IGNORECASE)
    return _parse_human_size(m.group(1)) if m else 0


def docker_available() -> bool:
    if not shutil.which("docker"):
        return False
    try:
        return _run(["docker", "version", "--format", "{{.Server.Version}}"]).returncode == 0
    except Exception:
        return False


def scan_docker(system: SystemInfo) -> list[JunkItem]:
    items: list[JunkItem] = []
    if not docker_available():
        return items
    try:
        out = _run(["docker", "system", "df"])
        total = 0
        for line in out.stdout.splitlines()[1:]:
            cols = line.split()
            if len(cols) >= 4:
                total += _parse_human_size(cols[3])
        items.append(JunkItem(Path("/dev/null"), "Docker dangling images/containers/build cache",
                              Risk.HIGH, total if total > 0 else -1,
                              display="Docker dangling images/containers/build cache"))
    except Exception:
        pass
    return items


def scan_kernels(system: SystemInfo) -> list[JunkItem]:
    items: list[JunkItem] = []
    if not system.root or not shutil.which("dpkg-query"):
        return items
    try:
        running = _run(["uname", "-r"]).stdout.strip()
    except Exception:
        running = ""
    try:
        out = _run(["dpkg-query", "-W", "-f=${Package}\t${Version}\n", "linux-image-*", "linux-headers-*"])
    except Exception:
        out = None
    if not out or out.returncode != 0:
        return items
    for line in out.stdout.splitlines():
        pkg, _, _ = line.partition("\t")
        if pkg and running and running not in pkg:
            items.append(JunkItem(Path(f"/boot/{pkg}"), f"old kernel: {pkg}", Risk.HIGH, -1))
    return items


def clean_docker(system: SystemInfo) -> str:
    if not docker_available():
        return "docker not available"
    try:
        _run(["docker", "builder", "prune", "-f"])
        _run(["docker", "image", "prune", "-f"])
        _run(["docker", "container", "prune", "-f"])
        _run(["docker", "volume", "prune", "-f"])
        return "docker prune complete"
    except Exception as exc:
        return f"docker prune failed: {exc}"


def _dpkg_status() -> dict[str, tuple[str, int]]:
    """Parse /var/lib/dpkg/status -> {pkg: (status_line, installed_bytes)}."""
    result: dict[str, tuple[str, int]] = {}
    try:
        text = Path("/var/lib/dpkg/status").read_text(errors="ignore")
    except OSError:
        return result
    for sec in text.split("\n\n"):
        pkg = ""
        status = ""
        size = 0
        for line in sec.splitlines():
            if line.startswith("Package:"):
                pkg = line.split(maxsplit=1)[1].strip()
            elif line.startswith("Status:"):
                status = line[7:].strip()
            elif line.startswith("Installed-Size:"):
                try:
                    size = int(line.split()[1]) * 1024
                except (ValueError, IndexError):
                    size = 0
        if pkg:
            result[pkg] = (status, size)
    return result


def _names_preview(names: list[str], limit: int = 6) -> str:
    shown = ", ".join(names[:limit])
    return shown + ("…" if len(names) > limit else "")


def scan_pkg_residue(system: SystemInfo) -> list[JunkItem]:
    if not system.root:
        return []
    db = _dpkg_status()
    pkgs = [p for p, (s, _) in db.items() if s.startswith("deinstall") and "config-files" in s]
    if not pkgs:
        return []
    total = sum(db[p][1] for p in pkgs)
    it = JunkItem(
        Path("/var/lib/dpkg/status"),
        f"leftover package configs: {_names_preview(pkgs)}",
        Risk.MEDIUM,
        total,
        display=f"{len(pkgs)} leftover package config files ({_names_preview(pkgs)})",
    )
    it.meta["pkgs"] = pkgs
    return [it]


def scan_autoremove(system: SystemInfo) -> list[JunkItem]:
    if not system.root or not shutil.which("apt-get"):
        return []
    try:
        out = _run(["apt-get", "-s", "autoremove"])
    except Exception:
        return []
    pkgs: list[str] = []
    collecting = False
    for line in out.stdout.splitlines():
        if "will be REMOVED" in line:
            collecting = True
            continue
        if collecting:
            stripped = line.strip()
            if not stripped:
                continue
            if stripped and (stripped[0].isdigit() or stripped.count(")") > 2):
                break
            pkgs.extend(line.split())
    pkgs = sorted(set(p for p in pkgs if p))
    if not pkgs:
        return []
    db = _dpkg_status()
    total = sum(db.get(p, ("", 0))[1] for p in pkgs)
    it = JunkItem(
        Path("/var/lib/dpkg/status"),
        f"unused packages: {_names_preview(pkgs)}",
        Risk.HIGH,
        total,
        display=f"{len(pkgs)} unused packages (apt autoremove): {_names_preview(pkgs)}",
    )
    it.meta["pkgs"] = pkgs
    return [it]


def scan_broken(system: SystemInfo) -> list[JunkItem]:
    if not system.root or not shutil.which("dpkg"):
        return []
    try:
        out = _run(["dpkg", "--audit"])
    except Exception:
        return []
    pkgs: list[str] = []
    collecting = False
    for line in out.stdout.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        if stripped.endswith(":") and any(k in stripped.lower() for k in ("broken", "problem", "mess", "not yet configured")):
            collecting = True
            continue
        if collecting and not stripped.endswith(":") and not stripped.lower().startswith(
            ("the following", "please", "normally", "at least")
        ):
            pkgs.extend(line.split())
    pkgs = sorted(set(p for p in pkgs if p))
    if not pkgs:
        return []
    db = _dpkg_status()
    total = sum(db.get(p, ("", 0))[1] for p in pkgs)
    it = JunkItem(
        Path("/var/lib/dpkg/status"),
        f"broken packages: {_names_preview(pkgs)}",
        Risk.HIGH,
        total,
        display=f"{len(pkgs)} broken packages: {_names_preview(pkgs)}",
    )
    it.meta["pkgs"] = pkgs
    return [it]


def clean_pkg_purge(system: SystemInfo, pkgs: list[str]) -> str:
    if not system.root:
        return "needs root"
    if not pkgs:
        return "nothing selected"
    try:
        out = _run(["apt-get", "purge", "-y", "--auto-remove", *pkgs])
        return (out.stdout.strip()[-300:] or out.stderr.strip()[-300:]) or f"purged {len(pkgs)} packages"
    except Exception as exc:
        return f"apt purge failed: {exc}"


def clean_autoremove(system: SystemInfo) -> str:
    if not system.root:
        return "needs root"
    try:
        out = _run(["apt-get", "autoremove", "-y", "--purge"])
        return (out.stdout.strip()[-300:] or out.stderr.strip()[-300:]) or "autoremove done"
    except Exception as exc:
        return f"autoremove failed: {exc}"


def scan_flatpak(system: SystemInfo) -> list[JunkItem]:
    if not shutil.which("flatpak"):
        return []
    it = JunkItem(
        Path("/run/flatpak"),
        "unused flatpak runtimes & applications",
        Risk.MEDIUM,
        -1,
        display="Unused flatpak runtimes & applications (flatpak --unused)",
    )
    return [it]


def clean_flatpak(system: SystemInfo) -> str:
    try:
        out = _run(["flatpak", "uninstall", "-y", "--unused"])
        return (out.stdout.strip()[-300:] or out.stderr.strip()[-300:]) or "flatpak --unused complete"
    except Exception as exc:
        return f"flatpak failed: {exc}"


def scan_snap(system: SystemInfo) -> list[JunkItem]:
    if not shutil.which("snap"):
        return []
    items: list[JunkItem] = []
    try:
        out = _run(["snap", "list", "--all"])
    except Exception:
        return items
    for line in out.stdout.splitlines()[1:]:
        parts = line.split()
        if len(parts) >= 6 and parts[5] == "disabled":
            name, rev = parts[0], parts[2]
            it = JunkItem(
                Path(f"/var/snap/{name}"),
                f"disabled snap revision: {name} r{rev}",
                Risk.MEDIUM,
                -1,
                display=f"Old snap revision: {name} r{rev} (disabled)",
            )
            it.meta["snap"] = (name, rev)
            items.append(it)
    return items


def clean_snap(system: SystemInfo, revisions: list[tuple[str, str]]) -> str:
    msgs: list[str] = []
    for name, rev in revisions:
        try:
            out = _run(["snap", "remove", "--revision", rev, name])
            msgs.append(f"{name} r{rev}: {out.stdout.strip()[-60:] or 'removed'}")
        except Exception as exc:
            msgs.append(f"{name} r{rev}: failed ({exc})")
    return " | ".join(msgs)[-300:] or "no snap revisions removed"


def clean_kernels(system: SystemInfo, packages: list[str]) -> str:
    if not system.root:
        return "needs root"
    try:
        return _run(["apt-get", "purge", "-y", "--auto-remove", *packages]).stdout.strip()[-500:]
    except Exception as exc:
        return f"kernel removal failed: {exc}"


# --------------------------------------------------- Arch Linux & systemd


def scan_pacman_cache(system: SystemInfo) -> list[JunkItem]:
    items: list[JunkItem] = []
    if not system.root or not shutil.which("pacman"):
        return items
    for base in (Path("/var/cache/pacman/pkg"),):
        if base.is_dir():
            sz, _ = dir_size(base)
            items.append(JunkItem(base, "pacman package cache", Risk.MEDIUM, sz))
    return items


def clean_pacman_cache(system: SystemInfo) -> str:
    if not system.root:
        return "needs root"
    try:
        if shutil.which("paccache"):
            out = _run(["paccache", "-rk1"])
        else:
            out = _run(["pacman", "-Sc", "--noconfirm"])
        return (out.stdout.strip()[-300:] or out.stderr.strip()[-300:]) or "pacman cache cleaned"
    except Exception as exc:
        return f"pacman cache clean failed: {exc}"


def scan_aur_cache(system: SystemInfo) -> list[JunkItem]:
    items: list[JunkItem] = []
    for helper, cache in (("yay", HOME / ".cache/yay"), ("paru", HOME / ".cache/paru")):
        if shutil.which(helper) and cache.is_dir():
            sz, _ = dir_size(cache)
            items.append(JunkItem(cache, f"{helper} build cache", Risk.MEDIUM, sz))
    return items


def clean_aur_cache(system: SystemInfo) -> str:
    helper = shutil.which("yay") or shutil.which("paru")
    if not helper:
        return "no AUR helper found"
    try:
        out = _run([helper, "-Sc", "--noconfirm"])
        return (out.stdout.strip()[-300:] or out.stderr.strip()[-300:]) or "AUR cache cleaned"
    except Exception as exc:
        return f"AUR cache clean failed: {exc}"


def scan_orphans(system: SystemInfo) -> list[JunkItem]:
    if not system.root or not shutil.which("pacman"):
        return []
    try:
        out = _run(["pacman", "-Qdtq"])
    except Exception:
        return []
    pkgs = sorted(set(p.strip() for p in out.stdout.splitlines() if p.strip()))
    if not pkgs:
        return []
    it = JunkItem(
        Path("/var/lib/pacman"),
        f"orphaned packages: {_names_preview(pkgs)}",
        Risk.HIGH,
        -1,
        display=f"{len(pkgs)} orphaned packages: {_names_preview(pkgs)}",
    )
    it.meta["pkgs"] = pkgs
    return [it]


def clean_orphans(system: SystemInfo, packages: list[str]) -> str:
    if not system.root:
        return "needs root"
    if not packages:
        return "nothing selected"
    try:
        out = _run(["pacman", "-Rns", "--noconfirm", *packages])
        return (out.stdout.strip()[-300:] or out.stderr.strip()[-300:]) or f"removed {len(packages)} orphaned packages"
    except Exception as exc:
        return f"orphan removal failed: {exc}"


def scan_journal(system: SystemInfo) -> list[JunkItem]:
    if not system.root or not shutil.which("journalctl"):
        return []
    try:
        out = _run(["journalctl", "--disk-usage"])
    except Exception:
        return []
    line = out.stdout.strip().splitlines()[0] if out.stdout.strip() else ""
    size = _extract_size(line)
    it = JunkItem(
        Path("/var/log/journal"),
        "systemd journal",
        Risk.MEDIUM,
        size if size else -1,
        display=line or "systemd journal logs",
    )
    return [it]


def clean_journal(system: SystemInfo) -> str:
    if not system.root:
        return "needs root"
    try:
        out = _run(["journalctl", "--vacuum-time=2weeks"])
        return (out.stdout.strip()[-300:] or out.stderr.strip()[-300:]) or "journal vacuumed to 2 weeks"
    except Exception as exc:
        return f"journal vacuum failed: {exc}"


# ----------------------------------------------------------- whole-disk scan

_ROTATED = re.compile(r"\.([0-9]+|gz|old|old\.[0-9]+)$")
_TEMP_TAIL = (".tmp", ".temp")
_CACHE_NAMES = {"cache", "caches"}
_PRUNED_DIR_NAMES = {"node_modules", "__pycache__", ".git"}

# areas already covered by other categories — skip in the whole-disk walk
_SKIP_DISK_BASES = {
    HOME, Path("/tmp"), Path("/var/tmp"), Path("/var/cache"), Path("/var/log"),
    Path("/var/crash"), Path("/var/lib"),
}


def _fs_map() -> dict[tuple[int, int], str]:
    """major:minor device -> fstype, from /proc/self/mountinfo."""
    if _fs_map.cache is not None:
        return _fs_map.cache
    out: dict[tuple[int, int], str] = {}
    try:
        with open("/proc/self/mountinfo") as f:
            for line in f:
                parts = line.split()
                if len(parts) < 10 or "-" not in parts:
                    continue
                dev = parts[2]
                fstype = parts[parts.index("-") + 1]
                try:
                    major, minor = dev.split(":")
                    out[(int(major), int(minor))] = fstype
                except ValueError:
                    continue
    except OSError:
        pass
    _fs_map.cache = out
    return out


_fs_map.cache: dict[tuple[int, int], str] | None = None


def _is_pseudo(path: Path, fs_map: dict[tuple[int, int], str]) -> bool:
    """True if path sits on a virtual filesystem (should not be walked)."""
    try:
        st = path.stat(follow_symlinks=False)
        dev = (os.major(st.st_dev), os.minor(st.st_dev))
        return _is_pseudo_fstype(fs_map.get(dev, ""))
    except OSError:
        return True


def _walk_all(max_seconds: float = 60.0):
    """Yield (root, dirs, files) across every real filesystem mount.

    Starts at /, descends into any real filesystem (including separate
    mounts like /home, /var, external drives) and skips virtual ones
    (proc, sys, dev, tmpfs, …). Known-junk bases covered by other
    categories are pruned to avoid duplicates."""
    fs_map = _fs_map()
    start = time.time()
    for top in (Path("/"),):
        if not top.is_dir() or _is_pseudo(top, fs_map):
            continue
        for root, dirs, files in os.walk(
            top, topdown=True, followlinks=False, onerror=lambda _e: None
        ):
            if time.time() - start > max_seconds:
                return
            root = Path(root)
            dirs[:] = [
                d for d in dirs
                if d not in _PRUNED_DIR_NAMES
                and not _is_pseudo(Path(root) / d, fs_map)
                and (root / d) not in _SKIP_DISK_BASES
            ]
            yield root, dirs, files


def scan_whole_disk(system: SystemInfo, max_seconds: float = 60.0) -> list[JunkItem]:
    """Search every bit of the machine for junk: backup files, stale temp
    files, stale logs and cache directories. Only actionable junk is
    reported; OS-critical trees stay protected."""
    items: list[JunkItem] = []
    backups: dict[Path, tuple[int, int]] = {}
    temp: dict[Path, tuple[int, int]] = {}
    logs: dict[Path, tuple[int, int]] = {}
    caches: dict[Path, int] = {}
    start = time.time()

    def budget() -> bool:
        return time.time() - start < max_seconds

    for root, dirs, files in _walk_all(max_seconds=max_seconds):
        dname = root.name.lower()
        for f in files:
            if not budget():
                break
            if _is_backup(f):
                try:
                    st = (root / f).lstat()
                except OSError:
                    continue
                if st.st_size and older_than_days(st, 7):
                    sz, cnt = backups.get(root, (0, 0))
                    backups[root] = (sz + st.st_size, cnt + 1)
            elif f.endswith(_TEMP_TAIL):
                try:
                    st = (root / f).lstat()
                except OSError:
                    continue
                if st.st_size and older_than_days(st, 2):
                    sz, cnt = temp.get(root, (0, 0))
                    temp[root] = (sz + st.st_size, cnt + 1)
        if dname in ("log", "logs"):
            sz = 0
            cnt = 0
            for f in files:
                if not budget():
                    break
                p = root / f
                try:
                    st = p.lstat()
                except OSError:
                    continue
                if st.st_size and (older_than_days(st, 7) or _ROTATED.search(f)):
                    sz += st.st_size
                    cnt += 1
            if cnt:
                logs[root] = (sz, cnt)
        if dname in _CACHE_NAMES:
            try:
                if root.is_dir():
                    sz, _ = dir_size(root)
                    if sz > 0:
                        caches[root] = sz
            except OSError:
                pass

    def keep(path: Path, desc: str, size: int, mtime: float | None = None) -> None:
        try:
            assert_cleanable(path)
        except PermissionError:
            return
        items.append(JunkItem(path, desc, Risk.MEDIUM, size, mtime))

    for d, (sz, cnt) in backups.items():
        keep(d, f"{cnt} backup files", sz)
    for d, (sz, cnt) in temp.items():
        keep(d, f"{cnt} stale temp files", sz)
    for d, (sz, cnt) in logs.items():
        keep(d, f"{cnt} stale log files", sz)
    for d, sz in caches.items():
        keep(d, "cache directory", sz)
    return items


# ---------------------------------------------------------------- registry


def build_categories(system: SystemInfo, report=None) -> list[Category]:
    specs = [
        ("trash", "Recycle bin", "🗑", False, Risk.SAFE, scan_trash),
        ("temp", "Temporary files", "🌡", False, Risk.MEDIUM, scan_temp),
        ("thumbs", "Thumbnails", "🖼", False, Risk.SAFE, scan_thumbs),
        ("dev", "Dev / package caches", "📦", False, Risk.SAFE, scan_dev_caches),
        ("browser", "Browser caches", "🌐", False, Risk.SAFE, scan_browsers),
        ("usercache", "~/.cache entries", "🗃", False, Risk.MEDIUM, scan_user_cache),
        ("crash", "Crash reports", "💥", False, Risk.SAFE, scan_crash),
        ("logs", "Stale user logs", "📄", False, Risk.MEDIUM, scan_logs),
        ("backup", "Editor backup files", "🧹", False, Risk.MEDIUM, scan_editor_junk),
        ("empty", "Empty directories", "📭", False, Risk.MEDIUM, scan_empty_dirs),
        ("apt", "apt cache", "🟠", True, Risk.MEDIUM, scan_apt_cache),
        ("syslogs", "System logs", "🖥", True, Risk.MEDIUM, scan_system_logs),
        ("pkgresidue", "Leftover pkg configs", "🧾", True, Risk.MEDIUM, scan_pkg_residue),
        ("autoremove", "Unused packages", "📉", True, Risk.HIGH, scan_autoremove),
        ("broken", "Broken packages", "💔", True, Risk.HIGH, scan_broken),
        ("flatpak", "Unused flatpak refs", "🧩", False, Risk.MEDIUM, scan_flatpak),
        ("snap", "Old snap revisions", "🐍", False, Risk.MEDIUM, scan_snap),
        ("docker", "Docker junk", "🐳", False, Risk.HIGH, scan_docker),
        ("kernels", "Old kernels", "🧠", True, Risk.HIGH, scan_kernels),
        ("pacman", "pacman cache", "🗂", True, Risk.MEDIUM, scan_pacman_cache),
        ("aur", "AUR helper cache", "🅰", False, Risk.MEDIUM, scan_aur_cache),
        ("orphans", "Orphaned packages", "🧟", True, Risk.HIGH, scan_orphans),
        ("journal", "Systemd journal", "🗒", True, Risk.MEDIUM, scan_journal),
        ("alldisk", "Whole-disk junk", "💾", False, Risk.HIGH, scan_whole_disk),
    ]
    cats: list[Category] = []
    pending: dict = {}
    for cid, label, icon, needs_root, risk, fn in specs:
        cat = Category(id=cid, label=label, icon=icon, needs_root=needs_root, default_risk=risk)
        if needs_root and not system.root:
            cat.skipped_reason = "needs root — re-run as 'sudo python3 wither.py'"
        cats.append(cat)
        if cat.skipped_reason:
            continue
        pending[_SCAN_EXECUTOR.submit(fn, system)] = cat
    for idx, (future, cat) in enumerate(pending.items(), start=1):
        try:
            cat.items = future.result()
        except Exception as exc:  # noqa: BLE001 — keep the rest of the scan going
            cat.skipped_reason = f"scan failed: {exc}"
        if report:
            report(cat.label, idx, len(specs))
    return cats


def category_totals(cats: list[Category]) -> tuple[int, int]:
    total = 0
    n = 0
    for c in cats:
        for it in c.items:
            n += 1
            if it.size > 0:
                total += it.size
    return total, n
