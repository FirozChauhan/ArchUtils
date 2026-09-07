# Wither

> A dead-simple Linux junk cleaner — scan 24 categories, review the sizes, remove only what you approve.

![Python](https://img.shields.io/badge/Python-161B22?style=for-the-badge&logo=python&logoColor=white)
![Linux](https://img.shields.io/badge/Linux-161B22?style=for-the-badge&logo=linux&logoColor=white)
![Arch](https://img.shields.io/badge/Arch-161B22?style=for-the-badge&logo=archlinux&logoColor=white)
![Debian](https://img.shields.io/badge/Debian-161B22?style=for-the-badge&logo=debian&logoColor=white)

## Install

```bash
git clone <your-remote> && cd ArchUtils/Wither
chmod +x wither.sh
```

Python 3.9+ (uses `Path.is_relative_to`), standard library only — `requirements.txt` exists to say so. Optional back-ends are detected at runtime: `apt`/`dpkg`, `pacman`, `flatpak`, `snap`, `docker`, `journalctl`, `yay`/`paru`.

## Usage

```bash
./wither.sh                 # user-level scan, confirm before removing
sudo -E ./wither.sh         # also unlock apt, pacman, kernels, journal
./wither.sh --dry-run       # report only — removes nothing
./wither.sh -y -j 8         # skip the prompt, 8 scan workers
./wither.sh --version
```

## API Reference

| Flag | Takes | Default | Description |
|------|-------|---------|-------------|
| `-y, --yes` | — | prompt | Skip confirmation and clean immediately |
| `--dry-run` | — | off | Scan and report, remove nothing |
| `-j, --jobs` | `N` | `max(4, min(16, cpus*2))` | Parallel scan workers |
| `--version` | — | — | Print the version |

Exit code `0` on success; a run prints bytes freed, locations removed, and per-item errors when they happen.

## Features

24 registered categories, each tagged with a risk level and whether it needs root:

| Safe to clean | Medium | High risk |
|---------------|--------|-----------|
| Recycle bin, thumbnails, dev/package caches, browser caches, crash reports | Temp files, `~/.cache` entries, stale logs, editor backups, empty dirs, apt cache, system logs, leftover pkg configs, flatpak, snap, pacman cache, AUR cache, journal | Unused packages, broken packages, Docker junk, old kernels, orphaned packages, whole-disk sweep |

- **Risk-labeled summary** — per-category sizes plus the 8 largest individual items before anything is touched.
- **Nothing removed without consent** — interactive `[Y/n]` by default; `--dry-run` and `-y` are explicit overrides.
- **Path guards** — `assert_cleanable` refuses forbidden roots, library/dependency trees, private home dot-dirs, links resolving outside a known cleanable base, and the base directories themselves.
- **Tool-backed cleanup** — apt, pacman, flatpak, snap and Docker are pruned through their own CLIs rather than by raw deletion.
- **Hybrid detection** — `os.walk` scans combined with live queries (`docker system df`, `apt-get -s autoremove`, `pacman -Qdtq`, `dpkg --audit`, `journalctl --disk-usage`).
- **Root-aware** — privileged categories are listed as skipped, not silently missed, when run as a normal user.
- **Bounded concurrency** — capped worker pools and time budgets on the home and whole-disk sweeps keep a scan from thrashing the machine.

## Configuration

| Option | Type | Default | Description |
|--------|------|---------|-------------|
| `-j` | int | auto-scaled | Scan worker count |
| `--dry-run` | flag | off | Report without deleting |
| `-y` | flag | off | Non-interactive clean |

## Environment Variables

None — no config files by design. All behavior comes from CLI flags and whether the process is running as root.

## Development

```bash
python3 -m py_compile wither/*.py    # syntax check
./wither.sh --dry-run                # safe full pass
./wither.sh --version
```

Two modules: `wither.py` is CLI and presentation, `cleaners.py` is scanning and deletion. Adding a category is one `(id, label, icon, needs_root, risk, scan_fn)` tuple in `build_categories` — cleaners stay pure and individually testable. `assert_cleanable` is the file to read before contributing.

## Architecture

```mermaid
flowchart TD
    A[wither.sh launcher] --> B[build_categories<br/>24 scanners, thread pool]
    B --> C[_summarize<br/>sizes + risk + 8 largest]
    C --> D{--dry-run · -y · [Y/n]}
    D -- no --> E[Nothing removed]
    D -- yes --> F[clean_items]
    F --> G{assert_cleanable}
    G -- refused --> H[Path skipped, reported]
    G -- ok --> I[Delete]
    I --> J[Summary: bytes freed]
```

Scan → summarize → gate → guard → delete. Key decisions: every path re-validated at deletion time rather than trusted from the scan phase; delegate to package managers instead of removing their files; keep each cleaner a pure function behind a registry tuple; bound every walk with a worker cap and a time budget.

## Contributing

PRs welcome. Open an issue first for major changes — especially anything touching the path guards.
