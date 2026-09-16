"""Optional yt-dlp resolver: page URL -> direct file URL + headers."""
from __future__ import annotations

from dataclasses import dataclass


@dataclass
class Resolved:
    url: str
    filename: str | None
    headers: dict
    page_url: str


def resolve(url: str, impersonate: str = "chrome", cookies: str = "", proxy: str = "") -> Resolved:
    """Try yt-dlp extraction. On failure, return url unchanged (direct download)."""
    try:
        from yt_dlp import YoutubeDL
    except ImportError:
        return Resolved(url=url, filename=None, headers={}, page_url=url)

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
        opts["cookiefile"] = cookies
    if proxy:
        opts["proxy"] = proxy
    try:
        with YoutubeDL(opts) as ydl:
            info = ydl.extract_info(url, download=False)
    except Exception:
        return Resolved(url=url, filename=None, headers={}, page_url=url)
    if not info:
        return Resolved(url=url, filename=None, headers={}, page_url=url)
    # direct file (generic) or single-format video
    direct = info.get("url")
    fname = info.get("_filename") or info.get("title")
    headers = dict(info.get("http_headers") or {})
    if direct:
        if fname:
            import posixpath

            fname = posixpath.basename(str(fname))
        return Resolved(url=direct, filename=fname, headers=headers, page_url=url)
    # multi-format: pick best http (non-hls) format for our downloader,
    # else hand back page url and let downloader/ffmpeg path fail clearly.
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
        return Resolved(
            url=best["url"], filename=f"{title}.{ext}", headers=headers, page_url=url
        )
    return Resolved(url=url, filename=None, headers=headers, page_url=url)
