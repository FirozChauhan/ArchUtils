"""tdm CLI."""
from __future__ import annotations

import argparse
import json
import logging
import os
import re
import sys
from pathlib import Path

from . import __version__
from .config import SAMPLE, default_dest, load_config, xdg_config_file, xdg_state_log
from .download import DownloadError, segmented_download
from .extract import resolve
from .probe import make_session, probe_url
from .util import check_disk, filename_from_cd, human, notify, parse_size, sanitize_filename, verify_checksum

LOG = logging.getLogger("tdm")


def lists_dir() -> Path:
    base = os.environ.get("XDG_DATA_HOME", str(Path.home() / ".local" / "share"))
    d = Path(base).expanduser() / "tdm" / "lists"
    d.mkdir(parents=True, exist_ok=True)
    return d


def sanitize_list_name(name: str) -> str:
    clean = re.sub(r"[^A-Za-z0-9_.-]+", "_", name.strip()).strip("._")
    if not clean:
        raise DownloadError("empty list name")
    return clean


def load_list(name: str) -> list[str]:
    p = lists_dir() / f"{sanitize_list_name(name)}.json"
    if not p.exists():
        raise DownloadError(f"no such list: {name} ({p})")
    try:
        data = json.loads(p.read_text())
        urls = data.get("urls", []) if isinstance(data, dict) else []
        return [u for u in urls if isinstance(u, str) and u.strip()]
    except Exception as e:
        raise DownloadError(f"corrupt list {name}: {e}") from e


def cmd_mk() -> int:
    if not sys.stdin.isatty():
        print("tdm mk needs an interactive terminal", file=sys.stderr)
        return 2
    try:
        name = input("List name: ").strip()
    except (EOFError, KeyboardInterrupt):
        print(file=sys.stderr)
        return 130
    if not name:
        print("empty name, aborted", file=sys.stderr)
        return 2
    name = sanitize_list_name(name)
    target = lists_dir() / f"{name}.json"
    if target.exists():
        try:
            keep = load_list(name)
            ans = input(f"List '{name}' exists ({len(keep)} urls). Overwrite? [y/N]: ").strip().lower()
        except (EOFError, KeyboardInterrupt):
            print(file=sys.stderr)
            return 130
        if ans not in ("y", "yes"):
            print("kept existing list", file=sys.stderr)
            return 0
    urls: list[str] = []
    while True:
        try:
            u = input(f"URL #{len(urls)+1} (empty to finish): ").strip()
        except (EOFError, KeyboardInterrupt):
            print(file=sys.stderr)
            break
        if not u:
            break
        if not u.startswith(("http://", "https://", "ftp://")):
            print(f"  ! doesn't look like a URL, kept anyway", file=sys.stderr)
        urls.append(u)
    if not urls:
        print("no urls, nothing saved", file=sys.stderr)
        return 2
    target.write_text(json.dumps({"name": name, "urls": urls}, indent=2))
    print(f"saved list '{name}' ({len(urls)} urls) -> {target}", file=sys.stderr)
    return 0


def cmd_ls() -> int:
    d = lists_dir()
    files = sorted(d.glob("*.json"))
    if not files:
        print("no lists yet (tdm mk to create one)", file=sys.stderr)
        return 0
    for f in files:
        try:
            n = len(json.loads(f.read_text()).get("urls", []))
        except Exception:
            n = -1
        print(f"{f.stem} ({n} urls)")
    return 0


def cmd_rm(name: str, no_prompt: bool = False) -> int:
    p = lists_dir() / f"{sanitize_list_name(name)}.json"
    if not p.exists():
        print(f"error: no such list: {name}", file=sys.stderr)
        return 1
    p.unlink()
    print(f"removed list '{p.stem}'", file=sys.stderr)
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="tdm", description="Robust CLI download manager (browser impersonation, resume, segments)."
    )
    p.add_argument("urls", nargs="*", help="URL(s) to download, or mk | ls | dl <list> | rm <list>")
    p.add_argument("-o", "--output", help="output file (default: auto from URL/Content-Disposition)")
    p.add_argument("-d", "--dest", help="destination directory (default ~/Downloads)")
    p.add_argument("-c", "--continue", dest="resume", action="store_true", help="resume .part if present (default: auto)")
    p.add_argument("--no-resume", action="store_true", help="ignore existing .part, start fresh")
    p.add_argument("-x", "--max-connections", type=int, default=None, help="max connections (default 8)")
    p.add_argument("--min-split-size", default=None, help="min chunk size e.g. 1M (default 1M)")
    p.add_argument("--limit-rate", default=None, help="global speed cap e.g. 500K, 4.2M")
    p.add_argument("-y", "--yes", "--no-prompt", dest="no_prompt", action="store_true",
                   help="never prompt for filename, use server default")
    p.add_argument("--prompt", action="store_true",
                   help="force filename prompt (useful with dl <list>)")
    p.add_argument("--impersonate", default=None, help='TLS profile: chrome|safari|firefox|"" (default chrome)')
    p.add_argument("--user-agent", default=None, help="override User-Agent")
    p.add_argument("--referer", default=None, help="Referer header (default: page URL if resolved)")
    p.add_argument("--cookies", default=None, help="Netscape cookies.txt path")
    p.add_argument("--cookies-from-browser", default=None, help="e.g. firefox[:profile] (exports via yt-dlp)")
    p.add_argument("--proxy", default=None, help="http://host:port or socks5://...")
    p.add_argument("--timeout", type=int, default=None, help="per-request timeout s (default 30)")
    p.add_argument("--retries", type=int, default=None, help="retries per chunk (default 5)")
    p.add_argument("--retry-delay", type=float, default=None, help="base backoff s (default 1.0)")
    p.add_argument("--checksum", default=None, help="verify e.g. sha256:<hex>")
    p.add_argument("--dry-run", action="store_true", help="probe + print plan, download nothing")
    p.add_argument("--no-extract", action="store_true", help="skip yt-dlp resolver, treat URL as direct file")
    p.add_argument("--notify", action="store_true", help="desktop notify on done/fail")
    p.add_argument("--json", action="store_true", help="machine JSON output")
    p.add_argument("--progress", choices=["bar", "plain", "none"], default=None)
    p.add_argument("--no-progress", action="store_true", help="disable progress bar")
    p.add_argument("--config", default=None, help="TOML config path")
    p.add_argument("--dump-config", action="store_true", help="print sample config and exit")
    p.add_argument("--init", action="store_true", help="write sample config to XDG location and exit")
    p.add_argument("-v", "--verbose", action="count", default=0)
    p.add_argument("-q", "--quiet", action="store_true")
    p.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    return p


def cookies_from_browser(spec: str) -> str:
    # spec "firefox" or "firefox:profile"
    import tempfile

    browser, _, profile = spec.partition(":")
    try:
        from yt_dlp.cookies import extract_cookies_from_browser
    except ImportError as e:
        raise DownloadError("yt-dlp required for --cookies-from-browser") from e
    tmp = Path(tempfile.gettempdir()) / f"tdm-cookies-{browser}.txt"
    jar = extract_cookies_from_browser(browser, profile or None, None, None)
    # jar is http.cookiejar; save as Mozilla jar
    jar.save(str(tmp), ignore_discard=True, ignore_expires=True)
    return str(tmp)


def setup_logging(verbose: int, quiet: bool, log_file: str = ""):
    level = logging.WARNING
    if quiet:
        level = logging.ERROR
    elif verbose >= 2:
        level = logging.DEBUG
    elif verbose == 1:
        level = logging.INFO
    logging.basicConfig(level=level, format="%(levelname)s: %(message)s", stream=sys.stderr)
    if log_file:
        try:
            from logging.handlers import RotatingFileHandler

            h = RotatingFileHandler(log_file, maxBytes=1_000_000, backupCount=3)
            h.setLevel(logging.DEBUG)
            logging.getLogger().addHandler(h)
        except Exception as e:
            LOG.warning("log file failed: %s", e)


def one(cfg: dict, args, raw_url: str) -> dict:
    net = cfg["network"]
    gen = cfg["general"]
    out = cfg["output"]

    impersonate = args.impersonate if args.impersonate is not None else net.get("impersonate", "chrome")
    user_agent = args.user_agent or net.get("user_agent", "")
    cookies = args.cookies or net.get("cookies", "")
    cfb = args.cookies_from_browser or net.get("cookies_from_browser", "")
    if cfb and not cookies:
        cookies = cookies_from_browser(cfb)
    proxy = args.proxy or net.get("proxy", "")
    timeout = args.timeout or net.get("timeout", 30)
    retries = args.retries if args.retries is not None else net.get("retries", 5)
    retry_delay = args.retry_delay if args.retry_delay is not None else net.get("retry_delay", 1.0)
    connections = args.max_connections or net.get("max_connections", 8)
    min_split = args.min_split_size or net.get("min_split_size", "1M")
    limit_rate = args.limit_rate if args.limit_rate is not None else net.get("limit_rate", "")
    progress = "none" if args.no_progress else (args.progress or out.get("progress", "bar"))
    quiet = args.quiet or out.get("quiet", False)

    # 1. resolve (yt-dlp) unless disabled
    page_url = raw_url
    headers_extra: dict = {}
    resolved_name: str | None = None
    dl_url = raw_url
    if not args.no_extract:
        try:
            r = resolve(raw_url, impersonate=impersonate, cookies=cookies, proxy=proxy)
            dl_url = r.url
            resolved_name = r.filename
            headers_extra = r.headers
            page_url = r.page_url
        except Exception as e:
            LOG.debug("resolver failed, using raw url: %s", e)

    referer = args.referer or net.get("referer", "") or (page_url if page_url != dl_url else "")

    # 2. probe (probe auto-retries hotlink 403s with derived Referers)
    sess = make_session(impersonate, user_agent, cookies, proxy, timeout)
    probe = probe_url(sess, dl_url, page_url=referer, timeout=timeout)
    if probe.status in (401, 404, 410):
        raise DownloadError(f"probe HTTP {probe.status} for {dl_url}")
    if probe.status == 403:
        raise DownloadError(
            f"probe HTTP 403 for {dl_url} (hotlink/expired token? "
            f"try --referer https://<embedding-site>/ or --cookies)"
        )
    if not referer and probe.referer:
        # probe discovered a working Referer (e.g. site hidden in acctoken);
        # the chunk fetches must send the same one or they get 403.
        referer = probe.referer

    # 3. filename
    if args.output:
        fname = sanitize_filename(Path(args.output).name)
        dest_dir = Path(args.output).expanduser().parent if len(Path(args.output).parts) > 1 else None
    else:
        fname = probe.filename or resolved_name or filename_from_cd(None, probe.final_url) or "file"
        fname = sanitize_filename(fname)
        dest_dir = None
        if (not getattr(args, "no_prompt", False) and not args.dry_run
                and not getattr(args, "json", False) and not quiet
                and sys.stdin.isatty()):
            try:
                ans = input(f"Filename [{fname}]: ").strip()
            except (EOFError, KeyboardInterrupt):
                print(file=sys.stderr)
                raise DownloadError("cancelled")
            if ans:
                given = sanitize_filename(Path(ans.strip().rstrip(".").rstrip()).name)
                if given:
                    if not Path(given).suffix and Path(fname).suffix:
                        given += Path(fname).suffix  # keep server extension
                    fname = given or fname
    raw_dest = args.dest or gen.get("dest", "")
    if raw_dest and raw_dest not in ("~/Downloads", "$HOME/Downloads"):
        base_dir = Path(raw_dest).expanduser()
    else:
        base_dir = default_dest()  # honors XDG user-dirs.dirs, falls back to ~/Downloads
    list_name = getattr(args, "list_name", "")
    if list_name:
        base_dir = base_dir / sanitize_list_name(list_name)
    if dest_dir and str(dest_dir) not in (".", ""):
        final = dest_dir.expanduser() / fname
    elif args.output and len(Path(args.output).expanduser().parts) > 1:
        final = Path(args.output).expanduser()
    else:
        final = base_dir / fname
    final.parent.mkdir(parents=True, exist_ok=True)

    if args.no_resume:
        for p in (final.with_name(final.name + ".part"), final.with_name(final.name + ".part.json")):
            try:
                p.unlink(missing_ok=True)
            except OSError:
                pass

    import math
    eff = 1
    if probe.resumable and probe.total:
        try:
            from .util import parse_size as _ps
            eff = max(1, min(connections, probe.total // max(_ps(min_split), 1), 16))
        except Exception:
            eff = 1
    plan = {
        "url": raw_url,
        "final_url": probe.final_url,
        "file": str(final),
        "size": probe.total,
        "size_h": human(probe.total) if probe.total else "unknown",
        "resumable": probe.resumable,
        "connections": eff,
        "content_type": probe.content_type,
        "referer": referer,
    }
    if getattr(args, "list_name", ""):
        plan["list"] = args.list_name

    if args.dry_run:
        return plan

    if not quiet and progress == "plain":
        print(f"downloading {final.name} ({plan['size_h']}) [{plan['connections']} conns]", file=sys.stderr)

    dest = segmented_download(
        dl_url, final, page_url=referer, impersonate=impersonate,
        user_agent=user_agent, cookies=cookies, proxy=proxy, headers=headers_extra,
        timeout=timeout, retries=retries, retry_delay=retry_delay,
        connections=connections, min_split=min_split, limit_rate=limit_rate,
        total=probe.total, resumable=probe.resumable, etag=probe.etag,
        progress_mode=progress, quiet=quiet,
    )
    if args.checksum:
        verify_checksum(dest, args.checksum)
    plan["done"] = str(dest)
    plan["bytes"] = dest.stat().st_size
    return plan


def main(argv=None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    if args.dump_config:
        print(SAMPLE)
        return 0
    if args.init:
        p = xdg_config_file()
        p.parent.mkdir(parents=True, exist_ok=True)
        if not p.exists():
            p.write_text(SAMPLE)
            print(str(p))
        else:
            print(f"exists: {p}", file=sys.stderr)
        return 0
    if not args.urls:
        parser.print_usage(sys.stderr)
        return 2
    if args.urls[0] == "mk" and len(args.urls) == 1:
        return cmd_mk()
    if args.urls[0] in ("ls", "lists") and len(args.urls) == 1:
        return cmd_ls()
    if args.urls[0] == "rm":
        if len(args.urls) != 2:
            print("usage: tdm rm <list>", file=sys.stderr)
            return 2
        return cmd_rm(args.urls[1], no_prompt=args.no_prompt)
    if args.urls[0] == "dl":
        if len(args.urls) != 2:
            print("usage: tdm dl <list>", file=sys.stderr)
            return 2
        if args.output:
            print("error: -o/--output can't be used with dl <list> (use -d/--dest)", file=sys.stderr)
            return 2
        try:
            list_name = sanitize_list_name(args.urls[1])
            args.urls = load_list(list_name)
            args.list_name = list_name
        except DownloadError as e:
            print(f"error: {e}", file=sys.stderr)
            return 1
        if not args.urls:
            print("list is empty", file=sys.stderr)
            return 2
        if not args.prompt:
            args.no_prompt = True  # lists don't ask per-file by default

    cfg = load_config(args.config)
    # state log default
    if not cfg["output"].get("log_file"):
        try:
            cfg["output"]["log_file"] = str(xdg_state_log())
            Path(str(xdg_state_log())).parent.mkdir(parents=True, exist_ok=True)
        except Exception:
            pass
    setup_logging(args.verbose, args.quiet, cfg["output"].get("log_file", ""))
    use_json = args.json or cfg["output"].get("json", False)
    do_notify = args.notify or cfg["general"].get("notify", False)

    results, failed = [], 0
    for u in args.urls:
        try:
            plan = one(cfg, args, u)
            results.append(plan)
            if not use_json and not args.quiet and not args.dry_run:
                print(f"saved: {plan.get('done')}", file=sys.stderr)
            if do_notify:
                notify("tdm done", str(plan.get("done") or plan.get("file")))
        except Exception as e:
            failed += 1
            results.append({"url": u, "error": str(e)})
            LOG.error("%s: %s", u, e)
            if do_notify:
                notify("tdm failed", f"{u}: {e}")
    if use_json:
        print(json.dumps(results, indent=2))
    elif args.dry_run and not args.quiet:
        for r in results:
            print(json.dumps(r, indent=2))
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
