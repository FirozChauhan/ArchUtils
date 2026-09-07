"""Wither — a dead-simple CLI junk cleaner: scan, review, remove."""

from __future__ import annotations

import argparse

from cleaners import (
    Category,
    JunkItem,
    SystemInfo,
    VERSION,
    build_categories,
    category_totals,
    clean_aur_cache,
    clean_autoremove,
    clean_docker,
    clean_flatpak,
    clean_items,
    clean_journal,
    clean_kernels,
    clean_orphans,
    clean_pacman_cache,
    clean_pkg_purge,
    clean_snap,
    human,
    set_workers,
)


def _scan(system: SystemInfo) -> list[Category]:
    print(f"Scanning ({'root' if system.root else 'user'})…")
    done = 0

    def report(label: str, idx: int, total: int) -> None:
        nonlocal done
        done += 1
        print(f"\r  [{done}/{total}] {label:<26}", end="", flush=True)

    cats = build_categories(system, report=report)
    print("\r" + " " * 70 + "\r", end="", flush=True)
    return cats


def _largest_items(cats: list[Category], limit: int = 8) -> list[JunkItem]:
    items = [it for c in cats for it in c.items if it.size > 0]
    items.sort(key=lambda it: it.size, reverse=True)
    return items[:limit]


def _summarize(cats: list[Category]) -> tuple[int, int]:
    total, n = category_totals(cats)
    if n == 0:
        print("Nothing to clean — your PC is already tidy.")
        return total, n
    print(f"Found {human(total)} of junk in {n} locations:\n")
    for c in cats:
        if not c.items:
            continue
        sz = sum(it.size for it in c.items if it.size > 0)
        print(
            f"  {c.icon} {c.label:<26} {len(c.items):>4} item(s)  "
            f"{human(sz):>9}  [{c.default_risk.value}]"
        )
    skipped = [c for c in cats if c.skipped_reason]
    if skipped:
        print("\n  Skipped:")
        for c in skipped:
            print(f"    {c.icon} {c.label}: {c.skipped_reason}")
    big = _largest_items(cats)
    if big:
        print("\n  Largest items:")
        for it in big:
            print(f"    {human(it.size):>9}  [{it.risk.value}]  {it.display_label()}")
    return total, n


def _clean(cats: list[Category], system: SystemInfo) -> None:
    freed = 0
    removed = 0
    errors: list[str] = []
    try:
        for cat in cats:
            items = cat.items
            if not items:
                continue
            print(f"  {cat.icon} {cat.label}…")
            if cat.id == "docker":
                msg = clean_docker(system)
            elif cat.id == "kernels":
                pkgs = sorted({it.path.name for it in items})
                msg = clean_kernels(system, pkgs) if pkgs else "nothing selected"
            elif cat.id == "autoremove":
                msg = clean_autoremove(system)
            elif cat.id == "flatpak":
                msg = clean_flatpak(system)
            elif cat.id == "snap":
                revs = [it.meta.get("snap") for it in items if it.meta.get("snap")]
                msg = clean_snap(system, revs) if revs else "nothing selected"
            elif cat.id == "pacman":
                msg = clean_pacman_cache(system)
            elif cat.id == "aur":
                msg = clean_aur_cache(system)
            elif cat.id == "orphans":
                pkgs = [p for it in items for p in it.meta.get("pkgs", [])]
                msg = clean_orphans(system, sorted(set(pkgs))) if pkgs else "nothing selected"
            elif cat.id == "journal":
                msg = clean_journal(system)
            elif cat.id in ("pkgresidue", "broken"):
                pkgs = [p for it in items for p in it.meta.get("pkgs", [])]
                msg = clean_pkg_purge(system, sorted(set(pkgs))) if pkgs else "nothing selected"
            else:
                f, r, e = clean_items(items)
                freed += f
                removed += r
                errors.extend(e)
                for err in e:
                    print(f"    ✖ {err}")
                continue
            if msg:
                print(f"    {msg}")
    except KeyboardInterrupt:
        print("\nInterrupted — stopping clean early.")
    summary = f"Done! Freed {human(freed)} · removed {removed} locations"
    if errors:
        summary += f" · {len(errors)} errors"
        if any("Permission denied" in e for e in errors):
            summary += "\nSome items are root-owned — re-run as root to free them: sudo -E ./wither.sh"
    else:
        summary += " · no errors"
    print(f"\n{summary}")


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(
        prog="wither",
        description="Scan and remove junk from your Linux machine.",
        epilog="Run with sudo to also clean system-level junk (apt, logs, kernels).",
    )
    parser.add_argument("--version", action="version", version=f"wither {VERSION}")
    parser.add_argument(
        "-y", "--yes", action="store_true",
        help="skip the confirmation prompt and clean right away",
    )
    parser.add_argument(
        "--dry-run", action="store_true",
        help="scan and report only; remove nothing",
    )
    parser.add_argument(
        "-j", "--jobs", type=int, metavar="N",
        help="number of parallel scan workers (default: auto)",
    )
    args = parser.parse_args(argv)

    if args.jobs:
        set_workers(args.jobs)

    system = SystemInfo()
    cats = _scan(system)
    total, n = _summarize(cats)
    if n == 0:
        return
    if args.dry_run:
        print("\nDry run — nothing was removed.")
        return
    if not args.yes:
        try:
            answer = input(f"\nRemove all this junk [{human(total)}]? [Y/n] ").strip().lower()
        except (EOFError, KeyboardInterrupt):
            answer = ""
        if answer not in ("", "y", "yes"):
            print("Nothing removed. Goodbye.")
            return
    print()
    _clean(cats, system)


if __name__ == "__main__":
    main()
