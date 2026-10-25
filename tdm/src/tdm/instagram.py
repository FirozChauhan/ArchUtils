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
import time
from dataclasses import dataclass
from urllib.parse import urlparse

from .util import parse_retry_after

LOG = logging.getLogger("tdm")

APP_ID = "936619743392459"
GRAPHQL_DOC_ID = "27130156389949648"

IG_RE = re.compile(
    r"https?://(?:www\.)?(?:instagram\.com|instagr\.am)(?:/(?!share/)[^/?#]+)?"
    r"/(?:p|tv|reels?(?!/audio/))/(?P<id>[^/?#&]+)",
)
IG_SHARE_RE = re.compile(r"https?://(?:www\.)?instagr\.am/(?:p|reel)/(?P<id>[^/?#&]+)")
SAVED_RE = re.compile(
    r"https?://(?:www\.)?instagram\.com/(?P<user>[^/?#]+)/saved(?:/all-posts)?/?(?:[?#]|$)"
)
PROFILE_RE = re.compile(
    r"https?://(?:www\.)?instagram\.com/(?P<user>[A-Za-z0-9._]{1,30})/?(?:[?#]|$)"
)
# Site routes that sit in the username position but are not profiles.
RESERVED_PATHS = frozenset({
    "p", "tv", "reel", "reels", "saved", "explore", "accounts", "stories",
    "direct", "about", "developer", "developers", "legal", "emails", "challenge",
    "web", "api", "oauth", "privacy", "terms", "help", "invites", "download",
    "directory", "session", "your_activity", "graphql", "static", "push",
})
LSD_RE = re.compile(r'\["LSD",\[\],\{"token":"([^"]+)"')

_ALPHABET = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_"


def is_instagram_url(url: str) -> bool:
    return bool(IG_RE.search(url) or IG_SHARE_RE.search(url) or SAVED_RE.search(url))


def is_saved_url(url: str) -> bool:
    return bool(SAVED_RE.search(url))


def profile_username_from_url(url: str) -> str | None:
    """Bare profile URL -> username; None for posts/reels/saved/site routes."""
    m = PROFILE_RE.match((url or "").strip())
    if not m:
        return None
    user = m.group("user")
    if user.lower() in RESERVED_PATHS or user.startswith(("_", ".")):
        return None
    return user


def is_profile_url(url: str) -> bool:
    return profile_username_from_url(url) is not None


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


def ig_filename(user: str, code: str, ext: str, idx: int | None = None) -> str:
    """Human filenames: `@user --- CODE.jpg`, carousel items get `_N`."""
    who = f"@{user}" if user else "@unknown"
    stem = f"{who} --- {code}" + (f"_{idx}" if idx else "")
    return f"{stem}.{ext}"


def _unescape_url(u: str) -> str:
    return u.replace("\\u0026", "&").replace("\\/", "/").strip()


def _dims(c) -> tuple[int, int]:
    try:
        return int(c.get("width") or 0), int(c.get("height") or 0)
    except (TypeError, ValueError, AttributeError):
        return 0, 0


def _best_video(cands: list[dict]) -> dict | None:
    """Largest-area video version (same content, different resolutions)."""
    best, best_a = None, -1
    for c in cands:
        if not isinstance(c, dict):
            continue
        u = c.get("url")
        if not u or not str(u).startswith("http"):
            continue
        w, h = _dims(c)
        a = w * h or w  # audio-only variants may lack height
        if a > best_a:
            best, best_a = c, a
    return best


def _orig_dims(media: dict, fallback: tuple[int, int] = (0, 0)) -> tuple[int, int]:
    try:
        w, h = int(media.get("original_width") or 0), int(media.get("original_height") or 0)
    except (TypeError, ValueError, AttributeError):
        w, h = 0, 0
    return (w, h) if w and h else fallback


def _best_image(cands: list[dict], orig_w: int = 0, orig_h: int = 0) -> dict | None:
    """Largest-area image, preferring the full-frame aspect over square crops.

    Instagram ships profile-grid square crops (e.g. 1080x1080) alongside the
    full image (e.g. 1080x1440); naive widest-wins grabs the cropped one.
    """
    valid = [c for c in cands
             if isinstance(c, dict) and str(c.get("url") or "").startswith("http")]
    if not valid:
        return None

    def _aspect(c) -> float:
        w, h = _dims(c)
        return (w / h) if h else 0.0

    if orig_w and orig_h:
        target = orig_w / orig_h
        same = [c for c in valid if abs(_aspect(c) - target) < 0.02]
        if same:
            valid = same
    return max(valid, key=lambda c: (_dims(c)[0] * _dims(c)[1]))


def items_from_product_info(info: dict, shortcode: str, username: str = "") -> list[IGMedia]:
    """Parse Instagram v1-API / logged-out-GraphQL product dicts."""
    out: list[IGMedia] = []
    user = username or str(((info.get("user") or {}) if isinstance(info.get("user"), dict) else {}).get("username") or "")
    ow, oh = _orig_dims(info)

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
                                       filename=ig_filename(user, shortcode, "mp4", i),
                                       width=int(best.get("width") or 0), height=int(best.get("height") or 0)))
                    continue
            if isinstance(m.get("video_url"), str):
                out.append(IGMedia(url=_unescape_url(m["video_url"]), ext="mp4",
                                   filename=ig_filename(user, shortcode, "mp4", i)))
                continue
            thumbs = ((m.get("image_versions2") or {}).get("candidates")
                      if isinstance(m.get("image_versions2"), dict) else None)
            if isinstance(thumbs, list) and thumbs:
                best = _best_image(thumbs, *_orig_dims(m, (ow, oh)))
                if best:
                    out.append(IGMedia(url=_unescape_url(str(best["url"])), ext="jpg",
                                       filename=ig_filename(user, shortcode, "jpg", i)))
                    continue
            if isinstance(m.get("display_url"), str):
                out.append(IGMedia(url=_unescape_url(m["display_url"]), ext="jpg",
                                   filename=ig_filename(user, shortcode, "jpg", i)))
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
                                   filename=ig_filename(user, shortcode, "mp4", i)))
            elif isinstance(node.get("display_url"), str):
                out.append(IGMedia(url=_unescape_url(node["display_url"]), ext="jpg",
                                   filename=ig_filename(user, shortcode, "jpg", i)))
        if out:
            return out

    vids = info.get("video_versions") if isinstance(info.get("video_versions"), list) else None
    if vids:
        best = _best_video(vids)
        if best:
            return [IGMedia(url=_unescape_url(str(best["url"])), ext="mp4",
                            filename=ig_filename(user, shortcode, "mp4"),
                            width=int(best.get("width") or 0), height=int(best.get("height") or 0))]
    if isinstance(info.get("video_url"), str):
        return [IGMedia(url=_unescape_url(info["video_url"]), ext="mp4",
                        filename=ig_filename(user, shortcode, "mp4"))]
    thumbs = ((info.get("image_versions2") or {}).get("candidates")
              if isinstance(info.get("image_versions2"), dict) else None)
    if isinstance(thumbs, list) and thumbs:
        best = _best_image(thumbs, ow, oh)
        if best:
            return [IGMedia(url=_unescape_url(str(best["url"])), ext="jpg",
                            filename=ig_filename(user, shortcode, "jpg"))]
    for key, ext in (("display_url", "jpg"), ("display_src", "jpg"), ("thumbnail_src", "jpg")):
        if isinstance(info.get(key), str):
            return [IGMedia(url=_unescape_url(info[key]), ext=ext,
                            filename=ig_filename(user, shortcode, ext))]
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
        found.append(IGMedia(url=u, ext=ext,
                             filename=ig_filename("", shortcode, ext, seen_idx)))

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


def _saved_headers(sess, lsd: str, csrf: str) -> dict:
    h = {
        "Accept": "*/*",
        "X-CSRFToken": csrf,
        "X-IG-App-ID": APP_ID,
        "X-ASBD-ID": "129477",
        "X-IG-WWW-Claim": "0",
        "X-Requested-With": "XMLHttpRequest",
        "Referer": "https://www.instagram.com/",
        "Sec-Fetch-Dest": "empty",
        "Sec-Fetch-Mode": "cors",
        "Sec-Fetch-Site": "same-origin",
    }
    if lsd:
        h["X-FB-LSD"] = lsd
    return h


def list_saved_posts(impersonate: str = "chrome", cookies: str = "",
                     proxy: str = "", timeout: int = 30, limit: int = 0) -> list[dict]:
    """List posts in the logged-in account's Saved collection.

    Returns [{shortcode, username, taken_at}] in saved order (newest first).
    Requires a logged-in session (``sessionid`` cookie) — raises RuntimeError
    with a human message otherwise.
    """
    from .probe import make_session

    if not cookies:
        raise RuntimeError("saved collections need a logged-in session: pass --cookies")
    sess = make_session(impersonate or "chrome", "", cookies, proxy, timeout)
    try:
        has_session = bool(sess.cookies.get("sessionid"))
    except Exception:
        has_session = False
    if not has_session:
        raise RuntimeError("no sessionid in cookies — re-export cookies.txt while logged in")

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

    posts: list[dict] = []
    max_id: str | None = ""
    while True:
        params: dict = {"count": 50}
        if max_id:
            params["max_id"] = max_id
        r = sess.get("https://www.instagram.com/api/v1/feed/saved/posts/",
                     params=params, headers=_saved_headers(sess, lsd, csrf), timeout=timeout)
        if r.status_code in (400, 401, 403):
            raise RuntimeError(f"saved feed HTTP {r.status_code} — session rejected, re-export cookies")
        if r.status_code != 200:
            raise RuntimeError(f"saved feed HTTP {r.status_code}")
        try:
            data = r.json()
        except Exception as e:
            raise RuntimeError(f"saved feed returned non-JSON: {e}") from e
        items = data.get("items") if isinstance(data, dict) else None
        if not isinstance(items, list):
            raise RuntimeError("unexpected saved feed shape (no items list)")
        for entry in items:
            media = (entry or {}).get("media") if isinstance(entry, dict) else None
            if not isinstance(media, dict):
                continue
            code = media.get("code") or ""
            user = media.get("user") or {}
            posts.append({
                "shortcode": str(code),
                "username": str(user.get("username") or "") if isinstance(user, dict) else "",
                "taken_at": media.get("taken_at") or 0,
                "media": media,
            })
            if limit and len(posts) >= limit:
                return posts
        if not data.get("more_available"):
            break
        max_id = data.get("next_max_id") or ""
        if not max_id:
            break
    return posts


def resolve_saved_media(impersonate: str = "chrome", cookies: str = "",
                        proxy: str = "", timeout: int = 30, limit: int = 0) -> list[IGMedia]:
    """Resolve every post in Saved to direct media items (uses feed payloads,
    no per-post lookup needed)."""
    out: list[IGMedia] = []
    for post in list_saved_posts(impersonate, cookies, proxy, timeout, limit=0):
        media = post["media"]
        code = post["shortcode"] or "post"
        user = post["username"]
        items = items_from_product_info(media, code, user) if code else []
        if not items and isinstance(media.get("video_url"), str):
            items = [IGMedia(url=_unescape_url(media["video_url"]), ext="mp4",
                             filename=ig_filename(user, code, "mp4"))]
        out.extend(items)
        if limit and len(out) >= limit:
            return out[:limit]
    return out


# --- whole-profile download -------------------------------------------------
# A profile page is paged by cursor: web_profile_info gives the numeric pk,
# then api/v1/feed/user/<pk>/?count=33&max_id=... returns the same v1 media
# objects the /saved/ feed does, newest first. Both need a logged-in session
# (Instagram answers 401 login_required to anonymous callers here).

@dataclass
class ProfilePage:
    items: list[IGMedia]
    cursor: str  # next_max_id, "" when the feed is exhausted
    posts: int  # posts on this page (files can be more: carousels)
    more: bool


def _bootstrap(sess, timeout: int = 30) -> tuple[str, str]:
    """Fetch the site root for an LSD token + csrftoken cookie."""
    lsd = csrf = ""
    try:
        r = sess.get("https://www.instagram.com/", timeout=timeout)
        m = LSD_RE.search(getattr(r, "text", "") or "")
        lsd = m.group(1) if m else ""
        try:
            csrf = sess.cookies.get("csrftoken") or ""
        except Exception:
            csrf = ""
    except Exception as e:
        LOG.debug("ig bootstrap failed: %s", e)
    return lsd, csrf


def _json_get(sess, url: str, headers: dict, params: dict | None = None,
              timeout: int = 30, label: str = "request", attempts: int = 3) -> dict:
    """GET JSON, pacing out 429s via Retry-After; clear errors for auth walls."""
    for attempt in range(attempts):
        r = sess.get(url, headers=headers, params=params, timeout=timeout)
        status = getattr(r, "status_code", 0)
        if status == 429:
            wait = parse_retry_after(getattr(r, "headers", {}).get("retry-after"),
                                     5.0 * (2 ** attempt))
            wait = min(max(wait, 1.0), 300.0)
            LOG.warning("instagram rate limit on %s, waiting %.0fs", label, wait)
            time.sleep(wait)
            continue
        if status in (401, 403):
            raise RuntimeError(
                f"{label}: HTTP {status} — Instagram wants a logged-in session. "
                "Run `tdm cookies <cookies.txt>` (export it while logged in), then retry."
            )
        if status == 404:
            raise RuntimeError(f"{label}: HTTP 404 — nothing there (check the username)")
        if status != 200:
            raise RuntimeError(f"{label}: HTTP {status}")
        ctype = str(getattr(r, "headers", {}).get("content-type", "")).lower()
        if ctype.startswith("text/html"):
            raise RuntimeError(
                f"{label}: Instagram answered with the web-app HTML shell instead of JSON — "
                "this endpoint (cursor paging) is not served to cookie/web sessions."
            )
        try:
            return r.json()
        except Exception as e:
            raise RuntimeError(f"{label}: response was not JSON ({e})") from e
    raise RuntimeError(
        f"{label}: Instagram refused this lookup with HTTP 429 after {attempts} tries.\n"
        "Profile LISTING is gated separately from single posts: /api/v1/users/* and the\n"
        "web GraphQL endpoint answer 429 or an app shell, while /api/v1/media/<pk>/info/\n"
        "(what tdm uses for one post) and /api/v1/feed/saved/posts/ keep working. So this\n"
        "is not a broken cookies file. Check whether the profile grid loads in a normal\n"
        "browser on this connection: if it does, retry with fresh cookies; if it does not,\n"
        "Instagram is gating this network and profile pulls cannot work from here."
    )


def profile_info(username: str, sess=None, impersonate: str = "chrome", cookies: str = "",
                 proxy: str = "", timeout: int = 30) -> dict:
    """Resolve @username -> {id, username, count, private}. Raises RuntimeError."""
    if not username:
        raise RuntimeError("empty username")
    if sess is None:
        from .probe import make_session

        sess = make_session(impersonate or "chrome", "", cookies, proxy, timeout)
    lsd, csrf = _bootstrap(sess, timeout)
    headers = _api_headers(lsd, csrf)
    headers.update({
        "X-Requested-With": "XMLHttpRequest",
        "Referer": f"https://www.instagram.com/{username}/",
    })
    data = _json_get(sess, "https://www.instagram.com/api/v1/users/web_profile_info/",
                     headers=headers, params={"username": username}, timeout=timeout,
                     label=f"@{username}", attempts=2)
    user = ((data.get("data") or {}).get("user") or {}) if isinstance(data, dict) else {}
    if not isinstance(user, dict) or not user:
        raise RuntimeError(f"@{username}: no such profile")
    uid = str(user.get("id") or "")
    if not uid:
        raise RuntimeError(f"@{username}: Instagram returned no user id")
    count = 0
    edge = user.get("edge_owner_to_timeline_media")
    if isinstance(edge, dict):
        try:
            count = int(edge.get("count") or 0)
        except (TypeError, ValueError):
            count = 0
    # web_profile_info ships the newest 12 posts inline; that is page 1 and the
    # only page some networks are allowed to see (cursor paging is app-only).
    edges = (edge or {}).get("edges") if isinstance(edge, dict) else None
    first_page = _items_from_edges(edges if isinstance(edges, list) else [],
                                  str(user.get("username") or username))
    pinfo = ((edge or {}).get("page_info") or {}) if isinstance(edge, dict) else {}
    return {"id": uid, "username": str(user.get("username") or username),
            "count": count, "private": bool(user.get("is_private")),
            "first_page": first_page, "first_count": len(edges) if isinstance(edges, list) else 0,
            "end_cursor": str(pinfo.get("end_cursor") or ""),
            "has_next": bool(pinfo.get("has_next_page"))}


def _items_from_edges(edges: list, username: str) -> list[IGMedia]:
    """Legacy GraphQL edges ([{node: {...}}]) -> IGMedia, reusing the v1 parser."""
    out: list[IGMedia] = []
    for e in edges:
        node = e.get("node") if isinstance(e, dict) else None
        if not isinstance(node, dict):
            continue
        code = str(node.get("shortcode") or node.get("code") or "")
        out.extend(items_from_product_info(node, code or "post", username))
    return out


def iter_profile_media(username: str, user_id: str, sess=None, impersonate: str = "chrome",
                       cookies: str = "", proxy: str = "", timeout: int = 30,
                       limit: int = 0, delay: float = 1.0, start_cursor: str = "",
                       page_size: int = 33, first_page: list[IGMedia] | None = None,
                       first_cursor: str = "", first_count: int = 0):
    """Yield one ProfilePage per feed page, newest first.

    ``first_page``/``first_cursor`` let the caller hand over the page that
    web_profile_info already returned, so page 1 costs no extra request.
    ``limit`` caps posts (not files). Pinned posts repeating across pages are
    de-duplicated.
    """
    seen_codes: set[str] = set()
    posts = 0
    if first_page:
        posts = first_count or len(first_page)
        yield ProfilePage(items=first_page, cursor=first_cursor, posts=posts,
                          more=bool(first_cursor))
        if not first_cursor:
            return
        if limit and posts >= limit:
            return
        if delay and delay > 0:
            time.sleep(delay)
    if sess is None:
        from .probe import make_session

        sess = make_session(impersonate or "chrome", "", cookies, proxy, timeout)
    lsd, csrf = _bootstrap(sess, timeout)
    headers = _api_headers(lsd, csrf)
    headers.update({
        "X-Requested-With": "XMLHttpRequest",
        "Referer": f"https://www.instagram.com/{username}/",
    })
    url = f"https://www.instagram.com/api/v1/feed/user/{user_id}/"
    cursor = start_cursor or first_cursor or ""
    seen_cursors: set[str] = set()
    while True:
        params: dict = {"count": page_size}
        if cursor:
            params["max_id"] = cursor
        data = _json_get(sess, url, headers=headers, params=params, timeout=timeout,
                         label=f"@{username} feed")
        raw = data.get("items") if isinstance(data, dict) else None
        if not isinstance(raw, list):
            raise RuntimeError(f"@{username}: unexpected feed payload (no items list)")
        items: list[IGMedia] = []
        for media in raw:
            if not isinstance(media, dict):
                continue
            code = str(media.get("code") or media.get("shortcode") or "")
            if code and code in seen_codes:
                continue
            if code:
                seen_codes.add(code)
            items.extend(items_from_product_info(media, code or "post", username))
        posts += len(raw)
        nxt = str(data.get("next_max_id") or "")
        more = bool(data.get("more_available")) and bool(nxt) and nxt not in seen_cursors
        if not more:
            nxt = ""
        yield ProfilePage(items=items, cursor=nxt, posts=len(raw), more=more)
        if not more:
            return
        if limit and posts >= limit:
            return
        seen_cursors.add(nxt)
        cursor = nxt
        if delay and delay > 0:
            time.sleep(delay)
