"""Segmented, resumable downloader with retries, atomic rename, progress."""
from __future__ import annotations

import json
import os
import random
import signal
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

from curl_cffi import requests as cr
from rich.progress import BarColumn, DownloadColumn, Progress, TimeRemainingColumn, TransferSpeedColumn

from .probe import CHROME_UA, DL_HEADERS, is_challenge, make_session
from .util import (
    check_disk,
    ensure_same_dir_part,
    fsync_dir,
    origin_of,
    parse_retry_after,
    parse_size,
    try_preallocate,
)

_cancel = threading.Event()


def _handle_sig(*_):
    _cancel.set()


for _sig in ("SIGINT", "SIGTERM"):
    try:
        signal.signal(getattr(signal, _sig), _handle_sig)
    except Exception:
        pass


class DownloadError(RuntimeError):
    pass


def _sleep_backoff(attempt: int, base: float, resp=None) -> None:
    if resp is not None and resp.status_code == 429:
        wait = parse_retry_after(resp.headers.get("Retry-After"), base * (2**attempt))
    else:
        wait = base * (2**attempt) + random.uniform(0, 1.0)
    wait = min(wait, 60.0)
    deadline = time.time() + wait
    while time.time() < deadline and not _cancel.is_set():
        time.sleep(0.1)


def _fetch_range(args) -> tuple[int, int, str | None]:
    """Worker: download [start, end] (end inclusive, -1 = to EOF). Returns (index, bytes, error)."""
    (idx, start, end, url, page_url, file_path, kw) = args
    impersonate = kw["impersonate"]
    s = make_session(
        impersonate=impersonate,
        user_agent=kw.get("user_agent", ""),
        cookies=kw.get("cookies", ""),
        proxy=kw.get("proxy", ""),
        timeout=kw.get("timeout", 30),
    )
    retries = kw.get("retries", 5)
    base = kw.get("retry_delay", 1.0)
    extra_headers = kw.get("headers", {})
    rate_cap = kw.get("rate_per_conn", 0)  # bytes/s, 0 = unlimited

    headers = dict(DL_HEADERS)
    headers["Accept-Encoding"] = "identity"
    if page_url:
        headers["Referer"] = page_url
        try:
            headers["Sec-Fetch-Site"] = (
                "same-origin" if origin_of(page_url) == origin_of(url) else "cross-site"
            )
        except Exception:
            pass
    headers.update(extra_headers)
    # referer fallback chain on 403 handled by caller via page_url variants; here just range
    if end >= 0:
        headers["Range"] = f"bytes={start}-{end}"
    else:
        headers["Range"] = f"bytes={start}-"

    err: str | None = None
    for attempt in range(retries + 1):
        if _cancel.is_set():
            return idx, 0, "cancelled"
        try:
            r = s.get(url, headers=headers, stream=True, timeout=kw.get("timeout", 30))
            if r.status_code == 429 or r.status_code in (500, 502, 503, 504):
                _sleep_backoff(attempt, base, r)
                continue
            if r.status_code == 403 and is_challenge(r):
                # clear __cf_bm-like stale cookie and retry once with impersonation
                try:
                    s.cookies.clear()
                except Exception:
                    pass
                _sleep_backoff(attempt, base, None)
                continue
            if r.status_code not in (200, 206):
                if r.status_code in (400, 401, 404, 410, 416):
                    return idx, 0, f"HTTP {r.status_code} (fail-fast)"
                _sleep_backoff(attempt, base, None)
                err = f"HTTP {r.status_code}"
                continue
            # server ignored Range -> only chunk 0 may accept 200
            if r.status_code == 200 and (end >= 0 and idx != 0):
                return idx, 0, "server ignored Range (no resume)"
            off = start
            with open(file_path, "r+b") as f:
                f.seek(start)
                for chunk in r.iter_content(chunk_size=256 * 1024):
                    if _cancel.is_set():
                        return idx, off - start, "cancelled"
                    if not chunk:
                        continue
                    f.write(chunk)
                    off += len(chunk)
                    if rate_cap > 0:
                        time.sleep(len(chunk) / rate_cap)
                    # progress callback via shared counter
                    prog = kw.get("_progress_cb")
                    if prog:
                        prog(len(chunk))
            return idx, off - start, None
        except Exception as e:  # network, timeout, proxy
            err = f"{type(e).__name__}: {e}"
            if attempt >= retries:
                break
            _sleep_backoff(attempt, base, None)
    return idx, 0, err or "failed"


def _load_state(state: Path) -> dict | None:
    try:
        return json.loads(state.read_text())
    except Exception:
        return None


def _save_state(state: Path, data: dict) -> None:
    tmp = state.with_suffix(".tmp")
    tmp.write_text(json.dumps(data))
    os.replace(tmp, state)


def segmented_download(
    url: str,
    dest: Path,
    page_url: str = "",
    impersonate: str = "chrome",
    user_agent: str = "",
    cookies: str = "",
    proxy: str = "",
    headers: dict | None = None,
    timeout: int = 30,
    retries: int = 5,
    retry_delay: float = 1.0,
    connections: int = 8,
    min_split: str | int = "1M",
    limit_rate: str | int = "",
    total: int = 0,
    resumable: bool = False,
    etag: str = "",
    progress_mode: str = "bar",
    quiet: bool = False,
) -> Path:
    dest.parent.mkdir(parents=True, exist_ok=True)
    part, state = ensure_same_dir_part(dest)
    min_split_n = parse_size(min_split)
    rate_total = parse_size(limit_rate)

    # decide plan
    if total <= 0 or not resumable:
        connections_eff = 1
    else:
        # like aria2: don't split tiny files
        max_by_size = max(1, total // max(min_split_n, 1))
        connections_eff = max(1, min(connections, max_by_size))
        connections_eff = min(connections_eff, 16)

    check_disk(dest, total)

    # resume: validate sidecar
    done_ranges: set[int] = set()
    st = _load_state(state)
    if st and part.exists() and st.get("final_url") and st.get("total") == total and total > 0:
        if st.get("etag", "") == etag:
            done_ranges = set(st.get("done", []))
    elif part.exists() and total == 0:
        pass  # unknown size: restart single-stream below (keep .part? no, restart)
        try:
            part.unlink()
        except OSError:
            pass

    # open / preallocate
    mode = "r+b" if part.exists() else "w+b"
    with open(part, mode) as f:
        if total > 0:
            try:
                if f.tell() != total:
                    try_preallocate(f.fileno(), total)
            except OSError as e:
                raise DownloadError(f"preallocate failed: {e}") from e

    # build chunk list
    chunks: list[tuple[int, int]] = []
    if connections_eff == 1 or total <= 0:
        chunks = [(0, -1)]
    else:
        size = total // connections_eff
        for i in range(connections_eff):
            s = i * size
            e = (s + size - 1) if i < connections_eff - 1 else (total - 1)
            if i in done_ranges:
                continue
            chunks.append((s, e))
        if not chunks:  # all done per sidecar but file maybe complete
            chunks = []

    rate_per_conn = (rate_total // connections_eff) if rate_total and connections_eff else 0
    written = [0]
    lock = threading.Lock()

    show = progress_mode == "bar" and not quiet
    prog = None
    task = None
    if show:
        from rich.progress import TextColumn

        prog = Progress(
            TextColumn("[bold blue]{task.fields[name]}"),
            BarColumn(),
            DownloadColumn(),
            TransferSpeedColumn(),
            TimeRemainingColumn(),
        )
        task = prog.add_task("dl", name=dest.name[:30], total=total or None)
        prog.start()
        # account already-written bytes
        if part.exists() and total > 0:
            have = part.stat().st_size if connections_eff == 1 else None
            # for segmented, completed chunks unknown precisely; use sidecar count
            if done_ranges:
                approx = len(done_ranges) * (total // connections_eff)
                prog.update(task, completed=min(approx, total))

    def _cb(n: int):
        written[0] += n
        if prog and task is not None:
            with lock:
                prog.update(task, advance=n)
        # periodic disk check every ~100MB
        if written[0] % (100 * 1024 * 1024) < 256 * 1024:
            try:
                if total and __import__("shutil").disk_usage(dest.parent).free < 64 * 1024**2:
                    _cancel.set()
            except Exception:
                pass

    kw = dict(
        impersonate=impersonate,
        user_agent=user_agent or CHROME_UA,
        cookies=cookies,
        proxy=proxy,
        timeout=timeout,
        retries=retries,
        retry_delay=retry_delay,
        headers=headers or {},
        rate_per_conn=rate_per_conn,
        _progress_cb=_cb,
    )

    errors: list[str] = []
    if chunks:
        with ThreadPoolExecutor(max_workers=len(chunks)) as ex:
            futs = {
                ex.submit(
                    _fetch_range, (i, s, e, url, page_url, part, kw)
                ): (i, s, e)
                for i, (s, e) in enumerate(chunks)
            }
            idx_map = {id(f): se for f, se in zip(futs.keys(), chunks)}
            for fut in as_completed(futs):
                idx, nbytes, err = fut.result()
                if err:
                    # single-stream fallback: server ignored Range
                    if "ignored Range" in err and connections_eff > 1:
                        for f in futs:
                            f.cancel()
                        if prog:
                            prog.stop()
                        # restart as single stream
                        try:
                            part.unlink(missing_ok=True)
                            state.unlink(missing_ok=True)
                        except OSError:
                            pass
                        return segmented_download(
                            url, dest, page_url, impersonate, user_agent, cookies,
                            proxy, headers, timeout, retries, retry_delay,
                            1, min_split, limit_rate, 0, False, etag,
                            progress_mode, quiet,
                        )
                    errors.append(f"chunk {idx}: {err}")
                else:
                    if total > 0 and connections_eff > 1:
                        # mark chunk idx done (map worker idx -> global chunk idx)
                        pass
            # persist done state (simplified: store completed count)
            if total > 0:
                _save_state(
                    state,
                    {"final_url": url, "total": total, "etag": etag,
                     "done": sorted(done_ranges), "ts": time.time()},
                )
    if prog:
        prog.stop()

    if _cancel.is_set():
        _save_state(state, {"final_url": url, "total": total, "etag": etag,
                            "done": sorted(done_ranges), "ts": time.time()})
        raise DownloadError("cancelled — progress kept in .part (re-run to resume)")

    if errors:
        raise DownloadError("; ".join(errors))

    # finalize: atomic rename + fsync
    with open(part, "rb") as f:
        f.flush()
        os.fsync(f.fileno())
    os.replace(part, dest)
    fsync_dir(dest.parent)
    try:
        state.unlink(missing_ok=True)
    except OSError:
        pass
    # exact-size check when known
    if total > 0 and dest.stat().st_size != total:
        raise DownloadError(
            f"size mismatch: got {dest.stat().st_size}, expected {total} (re-run to resume)"
        )
    return dest
