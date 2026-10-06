"""Optional yt-dlp resolver: page URL -> direct file URL + headers."""
from __future__ import annotations

from dataclasses import dataclass


@dataclass
class Resolved:
    url: str
    filename: str | None
    headers: dict
    page_url: str


def _ytdlp_info(url: str, impersonate: str = "chrome", cookies: str = "", proxy: str = "") -> dict | None:
    import logging
    import os as _os

    try:
        from yt_dlp import YoutubeDL
    except ImportError:
        return None
    opts: dict = {
        "quiet": True,
        "no_warnings": True,
        "retries": 2,
        "fragment_retries": 2,
        "nocheckcertificate": False,
    }
    if impersonate:
        opts["impersonate"] = impersonate
    if cookies:
        cookiefile = _os.path.expanduser(cookies)
        if not _os.path.exists(cookiefile):
            logging.getLogger("tdm").debug("cookiefile not found: %s", cookies)
            return None
        opts["cookiefile"] = cookiefile
    if proxy:
        opts["proxy"] = proxy
    try:
        with YoutubeDL(opts) as ydl:
            return ydl.extract_info(url, download=False)
    except Exception as e:
        logging.getLogger("tdm").debug("yt-dlp failed for %s: %s", url, e)
        return None


def _from_ytdlp_info(url: str, info: dict | None) -> list[Resolved]:
    if not info:
        return []
    headers = dict(info.get("http_headers") or {})
    # playlist (carousel / album): one Resolved per entry
    if info.get("_type") == "playlist" and isinstance(info.get("entries"), list):
        out: list[Resolved] = []
        for i, e in enumerate(info["entries"], 1):
            if not isinstance(e, dict):
                continue
            one = _from_ytdlp_info(url, e)
            if one:
                out.extend(one)
            elif e.get("url", "").startswith("http"):
                out.append(Resolved(url=e["url"], filename=e.get("_filename") or e.get("title"),
                                    headers=dict(e.get("http_headers") or headers), page_url=url))
        return out
    direct = info.get("url")
    fname = info.get("_filename") or info.get("title")
    if direct:
        if fname:
            import posixpath

            fname = posixpath.basename(str(fname))
        return [Resolved(url=direct, filename=fname, headers=headers, page_url=url)]
    fmts = info.get("requested_formats") or info.get("formats") or []
    best = None
    for f in reversed(fmts):
        u = f.get("url", "")
        proto = (f.get("protocol") or "").lower()
        if u.startswith("http") and "m3u8" not in proto and "m3u8" not in u:
            best = f
            break
    if best:
        headers.update(best.get("http_headers") or {})
        ext = best.get("ext") or "mp4"
        title = info.get("title") or "file"
        return [Resolved(url=best["url"], filename=f"{title}.{ext}", headers=headers, page_url=url)]
    return []


def resolve_all(url: str, impersonate: str = "chrome", cookies: str = "",
                proxy: str = "", limit: int = 0, translate: bool = True) -> list[Resolved]:
    """Resolve a page URL to 1+ direct files. X posts go through the no-auth
    vxTwitter API, Instagram gets native handling (anonymous GraphQL; Saved
    collections via the logged-in feed API), everything else goes straight
    to yt-dlp. Returns [] when nothing resolved."""
    try:
        from .instagram import (img_index_from_url, is_instagram_url, is_saved_url,
                                resolve_instagram_media, resolve_saved_media)
        from .x import is_x_url, resolve_x_media
    except ImportError:
        is_instagram_url = lambda u: False  # type: ignore
        is_saved_url = lambda u: False  # type: ignore
        is_x_url = lambda u: False  # type: ignore
        resolve_instagram_media = None  # type: ignore
        resolve_saved_media = None  # type: ignore
        resolve_x_media = None  # type: ignore
        img_index_from_url = lambda u: None  # type: ignore
    if is_x_url(url):
        # vxTwitter API first (no auth, handles photos/GIFs); yt-dlp fallback.
        try:
            items = resolve_x_media(url, impersonate=impersonate,
                                    cookies=cookies, proxy=proxy,
                                    translate=translate) if resolve_x_media else []
        except Exception:
            items = []
        if items:
            return items
    if is_instagram_url(url):
        try:
            if is_saved_url(url) and resolve_saved_media:
                items = resolve_saved_media(impersonate=impersonate, cookies=cookies,
                                            proxy=proxy, limit=limit)
            else:
                items = resolve_instagram_media(url, impersonate=impersonate,
                                                cookies=cookies, proxy=proxy) if resolve_instagram_media else []
        except RuntimeError:
            raise
        except Exception:
            items = []
        if items:
            idx = img_index_from_url(url)
            if idx is not None and len(items) > 1 and 0 <= idx < len(items):
                items = [items[idx]]
            return [Resolved(url=m.url, filename=m.filename,
                             headers={"Referer": "https://www.instagram.com/"},
                             page_url=url) for m in items]
        # fall through to yt-dlp (handles logged-in cookies case)
    return _from_ytdlp_info(url, _ytdlp_info(url, impersonate, cookies, proxy))


def resolve(url: str, impersonate: str = "chrome", cookies: str = "", proxy: str = "") -> Resolved:
    """Try yt-dlp extraction. On failure, return url unchanged (direct download)."""
    try:
        items = resolve_all(url, impersonate=impersonate, cookies=cookies, proxy=proxy)
    except Exception:
        items = []
    if items:
        return items[0]
    return Resolved(url=url, filename=None, headers={}, page_url=url)
