"""X (Twitter) resolver: no-auth vxTwitter API first, yt-dlp fallback.

yt-dlp's Twitter extractor often fails anonymously from datacenter IPs and
only handles videos ("No video could be found in this tweet" on GIF/photo
posts). The vxTwitter API (`api.vxtwitter.com`) needs no key and returns
every attached photo/GIF/video with direct `twimg.com` URLs.
"""
from __future__ import annotations

import json
import logging
import re
import urllib.parse as _up

LOG = logging.getLogger("tdm")

API_BASE = "https://api.vxtwitter.com"

X_RE = re.compile(
    r"https?://(?:www\.|mobile\.)?(?:twitter\.com|x\.com)"
    r"/(?:i/web/)?(?P<user>[^/?#]+)/status(?:es)?/(?P<id>\d+)"
    r"(?:/(?P<kind>photo|video)/(?P<index>\d+))?",
)


def is_x_url(url: str) -> bool:
    return bool(X_RE.search(url))


def status_id_from_url(url: str) -> str | None:
    m = X_RE.search(url)
    return m.group("id") if m else None


def user_from_url(url: str) -> str:
    m = X_RE.search(url)
    u = m.group("user") if m else ""
    return u if u and u != "i" else ""


def x_index_from_url(url: str) -> tuple[str, int] | None:
    """Trailing /photo/N or /video/N selector (1-based), else None."""
    m = X_RE.search(url)
    if m and m.group("kind") and m.group("index"):
        try:
            return m.group("kind"), int(m.group("index"))
        except ValueError:
            return None
    return None


def x_filename(user: str, tid: str, ext: str, idx: int | None = None) -> str:
    """Human filenames: `@user --- 12345.mp4`, multi-media gets `_N`."""
    who = f"@{user}" if user else "@unknown"
    stem = f"{who} --- {tid}" + (f"_{idx}" if idx else "")
    return f"{stem}.{ext}"


def upgrade_photo_url(url: str) -> str:
    """pbs.twimg.com serves scaled-down files by default; `name=orig` fetches
    the original quality (often ~2x bytes)."""
    try:
        p = _up.urlparse(url)
        if "pbs.twimg.com" not in (p.netloc or ""):
            return url
        q = dict(_up.parse_qsl(p.query, keep_blank_values=True))
        ext = (p.path.rsplit(".", 1)[-1] if "." in p.path else "jpg").lower()
        if ext not in ("jpg", "jpeg", "png", "webp"):
            ext = "jpg"
        q["format"] = ext if ext != "jpeg" else "jpg"
        q["name"] = "orig"
        return _up.urlunparse(p._replace(query=_up.urlencode(q)))
    except Exception:
        return url


def _ext_for_image(url: str) -> str:
    try:
        ext = _up.urlparse(url).path.rsplit(".", 1)[-1].lower()
    except Exception:
        return "jpg"
    return ext if ext in ("jpg", "jpeg", "png", "webp") else "jpg"


def items_from_api(payload: dict, tid: str) -> tuple[str, list[dict]]:
    """Parse vxTwitter JSON -> (screen_name, [{url, ext}]). Empty list when
    the tweet has no downloadable media."""
    user = str(payload.get("user_screen_name") or "")
    out: list[dict] = []
    for m in payload.get("media_extended") or []:
        if not isinstance(m, dict):
            continue
        url = m.get("url") or ""
        if not isinstance(url, str) or not url.startswith("http"):
            continue
        typ = str(m.get("type") or "")
        if typ == "video" or typ == "gif":
            out.append({"url": url, "ext": "mp4"})
        elif typ == "image":
            out.append({"url": upgrade_photo_url(url), "ext": _ext_for_image(url)})
        # unknown types ignored (polls, cards)
    return user, out


def _api_lookup(tid: str, impersonate: str, cookies: str, proxy: str,
                timeout: int) -> tuple[str, list[dict]]:
    import urllib.request

    # NOTE: plain stdlib HTTPS on purpose — Cloudflare in front of the API
    # blocks impersonated TLS fingerprints (curl_cffi, any profile) but
    # allows vanilla clients. Respects https_proxy env automatically.
    req = urllib.request.Request(
        f"{API_BASE}/i/status/{tid}",
        headers={"Accept": "application/json", "User-Agent": "tdm/1.0"},
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout or 30) as resp:
            if resp.status != 200:
                LOG.debug("x api HTTP %s for %s", resp.status, tid)
                return "", []
            payload = json.loads(resp.read().decode("utf-8", "ignore"))
    except Exception as e:
        LOG.debug("x api failed for %s: %s", tid, e)
        return "", []
    if not isinstance(payload, dict):
        return "", []
    return items_from_api(payload, tid)


def resolve_x_media(page_url: str, impersonate: str = "chrome", cookies: str = "",
                    proxy: str = "", timeout: int = 30) -> list:
    """Return [Resolved-like dicts] for an X status URL (may be empty)."""
    from .extract import Resolved

    tid = status_id_from_url(page_url)
    if not tid:
        return []
    user, items = _api_lookup(tid, impersonate, cookies, proxy, timeout)
    if not items:
        return []
    user = user or user_from_url(page_url)
    numbered = [(i if len(items) > 1 else None, m) for i, m in enumerate(items, 1)]
    sel = x_index_from_url(page_url)
    if sel:
        kind, idx = sel
        want_mp4 = kind == "video"
        pool = [(i, m) for i, m in numbered if (m["ext"] == "mp4") == want_mp4]
        if 1 <= idx <= len(pool):
            numbered = [pool[idx - 1]]
    return [Resolved(
        url=m["url"],
        filename=x_filename(user, tid, m["ext"], i),
        headers={},
        page_url=page_url,
    ) for i, m in numbered]
