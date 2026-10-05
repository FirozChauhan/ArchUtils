"""Instagram resolver for tdm: anonymous GraphQL + HTML fallback.

Tries native anonymous lookup via curl_cffi (LSD token + ``api/graphql``
``PolarisLoggedOutDesktopWWWPostRootContentQuery`` + HTML fallback).
Datacenter IPs are usually gated (GraphQL returns null) — then the caller
falls back to yt-dlp, which works with ``--cookies`` / logged-in session.
"""
from __future__ import annotations

import html as _html
import json
import logging
import re
from dataclasses import dataclass
from urllib.parse import urlparse

LOG = logging.getLogger("tdm")

APP_ID = "936619743392459"
GRAPHQL_DOC_ID = "27130156389949648"

IG_RE = re.compile(
    r"https?://(?:www\.)?(?:instagram\.com|instagr\.am)(?:/(?!share/)[^/?#]+)?"
    r"/(?:p|tv|reels?(?!/audio/))/(?P<id>[^/?#&]+)",
)
IG_SHARE_RE = re.compile(r"https?://(?:www\.)?instagr\.am/(?:p|reel)/(?P<id>[^/?#&]+)")
LSD_RE = re.compile(r'\["LSD",\[\],\{"token":"([^"]+)"')

_ALPHABET = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_"


def is_instagram_url(url: str) -> bool:
    return bool(IG_RE.search(url) or IG_SHARE_RE.search(url))


def shortcode_from_url(url: str) -> str | None:
    m = IG_RE.search(url) or IG_SHARE_RE.search(url)
    if not m:
        return None
    code = m.group("id").strip().rstrip("/")
    # strip trailing private-post suffix? (long shortcodes stay as-is; media_id
    # conversion only uses the canonical prefix — see shortcode_to_pk)
    return code or None


def shortcode_to_pk(shortcode: str) -> str:
    """Same base64 conversion as yt-dlp ``_id_to_pk``."""
    code = shortcode.split("/")[0][:11]  # canonical prefix; drops private suffix
    pk = 0
    for c in code:
        pk = pk * 64 + _ALPHABET.index(c)
    return str(pk)


def pk_to_shortcode(pk: int | str) -> str:
    n = int(pk)
    out = ""
    while n > 0:
        n, r = divmod(n, 64)
        out = _ALPHABET[r] + out
    return out


def normalize_instagram_url(url: str) -> str:
    code = shortcode_from_url(url)
    if code:
        return f"https://www.instagram.com/p/{code}/"
    return url


def img_index_from_url(url: str) -> int | None:
    """Honor ``?img_index=N`` (0-based, as Instagram uses). Returns None if absent/invalid."""
    try:
        q = urlparse(url).query
        for k, v in __import__("urllib.parse", fromlist=["parse_qsl"]).parse_qsl(q, keep_blank_values=True):
            if k == "img_index":
                idx = int(str(v).strip())
                return idx if idx >= 0 else None
    except (ValueError, TypeError):
        return None
    return None


@dataclass
class IGMedia:
    url: str
    ext: str  # "mp4" | "jpg"
    filename: str
    width: int = 0
    height: int = 0


def _unescape_url(u: str) -> str:
    return u.replace("\\u0026", "&").replace("\\/", "/").strip()


def _best_video(cands: list[dict]) -> dict | None:
    best, best_w = None, -1
    for c in cands:
        if not isinstance(c, dict):
            continue
        u = c.get("url")
        if not u or not str(u).startswith("http"):
            continue
        w = c.get("width") or 0
        try:
            w = int(w)
        except (TypeError, ValueError):
            w = 0
        if w >= best_w:
            best, best_w = c, w
    return best


def items_from_product_info(info: dict, shortcode: str, username: str = "") -> list[IGMedia]:
    """Parse Instagram v1-API / logged-out-GraphQL product dicts."""
    out: list[IGMedia] = []
    user = username or str(((info.get("user") or {}) if isinstance(info.get("user"), dict) else {}).get("username") or "")
    base = f"{user}_{shortcode}" if user else shortcode

    carousels = info.get("carousel_media")
    if isinstance(carousels, list) and carousels:
        for i, m in enumerate(carousels, 1):
            if not isinstance(m, dict):
                continue
            vids = m.get("video_versions") if isinstance(m.get("video_versions"), list) else None
            if vids:
                best = _best_video(vids)
                if best:
                    out.append(IGMedia(url=_unescape_url(str(best["url"])), ext="mp4",
                                       filename=f"{base}_{i}.mp4",
                                       width=int(best.get("width") or 0), height=int(best.get("height") or 0)))
                    continue
            if isinstance(m.get("video_url"), str):
                out.append(IGMedia(url=_unescape_url(m["video_url"]), ext="mp4", filename=f"{base}_{i}.mp4"))
                continue
            thumbs = ((m.get("image_versions2") or {}).get("candidates")
                      if isinstance(m.get("image_versions2"), dict) else None)
            if isinstance(thumbs, list) and thumbs:
                best = _best_video(thumbs)
                if best:
                    out.append(IGMedia(url=_unescape_url(str(best["url"])), ext="jpg",
                                       filename=f"{base}_{i}.jpg"))
                    continue
            if isinstance(m.get("display_url"), str):
                out.append(IGMedia(url=_unescape_url(m["display_url"]), ext="jpg", filename=f"{base}_{i}.jpg"))
        if out:
            return out

    # GraphQL sidecar shape
    sidecar = ((info.get("edge_sidecar_to_children") or {}).get("edges")
               if isinstance(info.get("edge_sidecar_to_children"), dict) else None)
    if isinstance(sidecar, list) and sidecar:
        for i, e in enumerate(sidecar, 1):
            node = (e or {}).get("node") if isinstance(e, dict) else None
            if not isinstance(node, dict):
                continue
            if node.get("is_video") and isinstance(node.get("video_url"), str):
                out.append(IGMedia(url=_unescape_url(node["video_url"]), ext="mp4",
                                   filename=f"{base}_{i}.mp4"))
            elif isinstance(node.get("display_url"), str):
                out.append(IGMedia(url=_unescape_url(node["display_url"]), ext="jpg",
                                   filename=f"{base}_{i}.jpg"))
        if out:
            return out

    vids = info.get("video_versions") if isinstance(info.get("video_versions"), list) else None
    if vids:
        best = _best_video(vids)
        if best:
            return [IGMedia(url=_unescape_url(str(best["url"])), ext="mp4",
                            filename=f"{base}.mp4",
                            width=int(best.get("width") or 0), height=int(best.get("height") or 0))]
    if isinstance(info.get("video_url"), str):
        return [IGMedia(url=_unescape_url(info["video_url"]), ext="mp4", filename=f"{base}.mp4")]
    thumbs = ((info.get("image_versions2") or {}).get("candidates")
              if isinstance(info.get("image_versions2"), dict) else None)
    if isinstance(thumbs, list) and thumbs:
        best = _best_video(thumbs)
        if best:
            return [IGMedia(url=_unescape_url(str(best["url"])), ext="jpg", filename=f"{base}.jpg")]
    for key, ext in (("display_url", "jpg"), ("display_src", "jpg"), ("thumbnail_src", "jpg")):
        if isinstance(info.get(key), str):
            return [IGMedia(url=_unescape_url(info[key]), ext=ext, filename=f"{base}.{ext}")]
    return out


def items_from_html(html: str, shortcode: str) -> list[IGMedia]:
    """Last-resort scrape of post HTML: video_url/display_url JSON + og:* meta."""
    found: list[IGMedia] = []
    seen: set[str] = set()

    def add(u: str, ext: str):
        u = _unescape_url(_html.unescape(u))
        if not u.startswith("http") or u in seen:
            return
        seen.add(u)
        seen_idx = len(seen)
        found.append(IGMedia(url=u, ext=ext, filename=f"ig_{shortcode}_{seen_idx}.{ext}"))

    for m in re.finditer(r'"video_url"\s*:\s*"([^"]+)"', html):
        add(m.group(1), "mp4")
    if found:
        return found
    for m in re.finditer(r'<meta[^>]+property=["\']og:video(?::secure_url)?["\'][^>]+content=["\']([^"\']+)', html):
        add(m.group(1), "mp4")
    if found:
        return found
    for m in re.finditer(r'"display_url"\s*:\s*"([^"]+)"', html):
        add(m.group(1), "jpg")
    for m in re.finditer(r'<meta[^>]+property=["\']og:image(?::secure_url)?["\'][^>]+content=["\']([^"\']+)', html):
        add(m.group(1), "jpg")
    return found


def _api_headers(lsd: str = "", csrf: str = "") -> dict:
    h = {
        "X-IG-App-ID": APP_ID,
        "X-ASBD-ID": "359341",
        "X-IG-WWW-Claim": "0",
        "Origin": "https://www.instagram.com",
        "Accept": "*/*",
        "Referer": "https://www.instagram.com/",
    }
    if csrf:
        h["X-CSRFToken"] = csrf
    if lsd:
        h["X-FB-LSD"] = lsd
    return h


def _native_lookup(page_url: str, shortcode: str, impersonate: str,
                   cookies: str, proxy: str, timeout: int) -> list[IGMedia]:
    from .probe import make_session

    sess = make_session(impersonate or "chrome", "", cookies, proxy, timeout)
    lsd, csrf = "", ""
    try:
        r = sess.get("https://www.instagram.com/", timeout=timeout)
        lsd = (LSD_RE.search(r.text or "").group(1) if r.text else "") or ""
        try:
            csrf = sess.cookies.get("csrftoken") or ""
        except Exception:
            csrf = ""
    except Exception as e:
        LOG.debug("ig setup failed: %s", e)

    media_id = shortcode_to_pk(shortcode)
    # 0. logged-in lookup (same endpoint yt-dlp uses with sessionid cookies).
    # Anonymous GraphQL returns null for gated posts even with cookies, so try
    # the authenticated v1 API first when a session looks present.
    try:
        has_session = False
        try:
            has_session = bool(sess.cookies.get("sessionid"))
        except Exception:
            has_session = bool(cookies)
        if has_session:
            r = sess.get(f"https://www.instagram.com/api/v1/media/{media_id}/info/",
                         headers=_api_headers(lsd, csrf), timeout=timeout)
            if r.status_code == 200:
                payload = r.json()
                items_list = payload.get("items") if isinstance(payload, dict) else None
                if isinstance(items_list, list) and items_list and isinstance(items_list[0], dict):
                    items = items_from_product_info(items_list[0], shortcode)
                    if items:
                        return items
            else:
                LOG.debug("ig logged-in info: HTTP %s", r.status_code)
    except Exception as e:
        LOG.debug("ig logged-in lookup failed: %s", e)

    # 1. logged-out GraphQL (same doc as yt-dlp anonymous path)
    try:
        headers = dict(_api_headers(lsd, csrf))
        headers.update({
            "X-FB-Friendly-Name": "PolarisLoggedOutDesktopWWWPostRootContentQuery",
            "X-Requested-With": "XMLHttpRequest",
        })
        data = {
            "lsd": lsd,
            "fb_api_caller_class": "RelayModern",
            "fb_api_req_friendly_name": "PolarisLoggedOutDesktopWWWPostRootContentQuery",
            "server_timestamps": "true",
            "variables": json.dumps({"media_id": media_id}, separators=(",", ":")),
            "doc_id": GRAPHQL_DOC_ID,
        }
        r = sess.post("https://www.instagram.com/api/graphql", data=data,
                      headers=headers, timeout=timeout)
        payload = r.json() if r.status_code < 500 else {}
        media = ((payload.get("data") or {}).get("xig_polaris_media") or {})
        product = media.get("if_not_gated_logged_out")
        if isinstance(product, dict):
            items = items_from_product_info(product, shortcode)
            if items:
                return items
    except Exception as e:
        LOG.debug("ig graphql failed: %s", e)

    # 2. post HTML fallback (RelayPrefetchedStreamCache or raw video_url)
    try:
        r = sess.get(f"https://www.instagram.com/p/{shortcode}/", timeout=timeout)
        if r.status_code == 200 and r.text:
            items = items_from_html(r.text, shortcode)
            if items:
                return items
    except Exception as e:
        LOG.debug("ig html fallback failed: %s", e)
    return []


def resolve_instagram_media(page_url: str, impersonate: str = "chrome", cookies: str = "",
                            proxy: str = "", timeout: int = 30) -> list[IGMedia]:
    """Return direct media items for an Instagram post/reel URL (may be empty)."""
    shortcode = shortcode_from_url(page_url)
    if not shortcode:
        return []
    return _native_lookup(page_url, shortcode, impersonate, cookies, proxy, timeout)
