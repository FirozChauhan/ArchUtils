"""Browser-impersonated probing + session factory."""
from __future__ import annotations

import http.cookiejar
from dataclasses import dataclass
from pathlib import Path

from curl_cffi import requests as cr

from .util import filename_from_cd, origin_of, referer_candidates

CHROME_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/136.0.0.0 Safari/537.36"
)

NAV_HEADERS = {
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
    "Upgrade-Insecure-Requests": "1",
    "Sec-Fetch-Site": "none",
    "Sec-Fetch-Mode": "navigate",
    "Sec-Fetch-User": "?1",
    "Sec-Fetch-Dest": "document",
}

DL_HEADERS = {
    "Accept": "*/*",
    "Accept-Language": "en-US,en;q=0.9",
    "Sec-Fetch-Mode": "no-cors",
    "Sec-Fetch-Dest": "empty",
}


@dataclass
class Probe:
    url: str
    final_url: str
    total: int  # 0 = unknown
    resumable: bool
    filename: str | None
    content_type: str
    etag: str
    last_modified: str
    status: int
    referer: str = ""  # Referer value that produced this result ("" = none sent)


def make_session(
    impersonate: str = "chrome",
    user_agent: str = "",
    cookies: str = "",
    proxy: str = "",
    timeout: int = 30,
) -> cr.Session:
    imp = impersonate.strip() or False
    s = cr.Session(impersonate=imp or False)  # type: ignore[arg-type]
    s.headers.update(NAV_HEADERS)
    s.headers.update({"User-Agent": user_agent or CHROME_UA})
    s.timeout = timeout
    if proxy:
        s.proxies = {"http": proxy, "https": proxy}
    if cookies:
        jar = http.cookiejar.MozillaCookieJar(str(Path(cookies).expanduser()))
        try:
            jar.load(ignore_discard=True, ignore_expires=True)
            for c in jar:
                s.cookies.set(c.name, c.value or "", domain=c.domain, path=c.path)
        except FileNotFoundError as e:
            raise FileNotFoundError(f"cookies file not found: {cookies}") from e
    return s


def _total_from(resp) -> int:
    crng = resp.headers.get("content-range", "")
    if "/" in crng:
        try:
            return int(crng.rsplit("/", 1)[1])
        except ValueError:
            pass
    try:
        return int(resp.headers.get("content-length", 0) or 0)
    except ValueError:
        return 0


def _probe_once(s: cr.Session, url: str, ref: str, timeout: int) -> Probe:
    """Single 1-byte probe attempt with the given Referer ("" = none)."""
    headers = dict(DL_HEADERS)
    headers["Accept-Encoding"] = "identity"
    headers["Range"] = "bytes=0-0"
    if ref:
        headers["Referer"] = ref
        try:
            headers["Sec-Fetch-Site"] = (
                "same-origin" if origin_of(ref) == origin_of(url) else "cross-site"
            )
        except Exception:
            headers["Sec-Fetch-Site"] = "cross-site"
    else:
        headers.pop("Sec-Fetch-Site", None)
    r = s.get(url, headers=headers, stream=True, timeout=timeout, allow_redirects=True)
    # read at most 1 byte to let server send headers, then close
    try:
        _ = r.content  # small (0-1B + headers)
    except Exception:
        pass
    status = r.status_code
    resumable = status == 206 and "content-range" in {k.lower() for k in r.headers.keys()}
    if not resumable and r.headers.get("accept-ranges", "").lower() == "bytes":
        resumable = True  # optimistic: try ranged fetch, downgrade on 200
    total = _total_from(r)
    cd = r.headers.get("content-disposition")
    fname = filename_from_cd(cd, str(r.url))
    return Probe(
        url=url,
        final_url=str(r.url),
        total=total,
        resumable=resumable,
        filename=fname,
        content_type=r.headers.get("content-type", ""),
        etag=r.headers.get("etag", ""),
        last_modified=r.headers.get("last-modified", ""),
        status=status,
        referer=ref,
    )


def probe_url(s: cr.Session, url: str, page_url: str = "", timeout: int = 30) -> Probe:
    """Cheap 1-byte probe. Falls back gracefully when server ignores Range.

    Hotlink-protected hosts answer 403 when no (or a wrong) Referer is sent.
    On 403, retry with Referer candidates derived from the URL itself (e.g.
    the embedding site hidden inside cdntrex-style acctoken params) and
    return the first non-403 result, remembering the working Referer.
    """
    first = _probe_once(s, url, page_url or "", timeout)
    if first.status != 403:
        return first
    seen = {page_url or ""}
    for cand in referer_candidates(url):
        if cand in seen:
            continue
        seen.add(cand)
        try:
            p = _probe_once(s, url, cand, timeout)
        except Exception:
            continue
        if p.status != 403:
            return p
    return first


def is_challenge(resp) -> bool:
    if resp.status_code != 403:
        return False
    if resp.headers.get("cf-mitigated") == "challenge":
        return True
    try:
        body = (resp.content or b"")[:4096]
        return b"Attention Required! | Cloudflare" in body
    except Exception:
        return False
