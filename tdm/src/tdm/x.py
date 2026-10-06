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


def x_filename(user: str, tid: str, ext: str, idx: int | None = None,
               text: str = "") -> str:
    """Human filenames: `@user --- some words.mp4`, id only when textless.

    Text is cleaned (links/entities/extra whitespace out) and truncated to
    fit the 200-char filename cap. Multi-media posts keep a `_N` slot.
    Textless posts fall back to `@user --- 123[_N].mp4` so names stay unique.
    """
    who = f"@{user}" if user else "@unknown"
    slot = f"_{idx}" if idx else ""
    fixed = len(who) + len(" --- ") + len(slot) + len(f".{ext}")
    snippet = clean_text(text, budget=max(0, 200 - fixed))
    stem = f"{who} --- {snippet}{slot}" if snippet else f"{who} --- {tid}{slot}"
    name = f"{stem}.{ext}"
    while len(name.encode("utf-8")) > 240 and snippet:
        snippet = snippet[:-1].rstrip(" -_.,!?:;")
        stem = f"{who} --- {snippet}{slot}" if snippet else f"{who} --- {tid}{slot}"
        name = f"{stem}.{ext}"
    return name


def clean_text(text: str, budget: int) -> str:
    """Collapse a post's text to a filename-safe snippet within budget chars."""
    import html as _h

    t = _h.unescape(text or "")
    t = re.sub(r"https?://\S+", "", t)
    t = t.replace("\n", " ").replace("\r", " ")
    t = re.sub(r'[<>:"|?*]', "", t)
    t = re.sub(r"\s+", " ", t).strip(" -_.,!?:;")
    if budget <= 0:
        return ""
    if len(t) > budget:
        cut = t[:budget].rsplit(" ", 1)
        t = cut[0] if len(cut) > 1 and len(cut[0]) >= budget // 2 else t[:budget]
        t = t.rstrip(" -_.,!?:;")
    return t


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


def items_from_api(payload: dict, tid: str) -> tuple[str, str, str, list[dict]]:
    """Parse vxTwitter JSON -> (screen_name, text, lang, [{url, ext}]). Empty
    media list when the tweet has no downloadable media."""
    user = str(payload.get("user_screen_name") or "")
    text = str(payload.get("text") or "")
    lang = str(payload.get("lang") or "")
    explicit = payload.get("translation")
    if isinstance(explicit, dict):
        cand = explicit.get("text") or explicit.get("translatedText") or ""
        if isinstance(cand, str) and cand.strip():
            text = cand.strip()
            lang = "en"
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
    return user, text, lang, out


_TRANSLATE_CACHE: dict[str, str] = {}


def translate_en(text: str, lang: str = "", timeout: int = 10) -> str:
    """English filename text via MyMemory (free, no key). Never raises:
    returns the original text when already English, on quota errors, or
    on any failure."""
    import urllib.request

    t = (text or "").strip()
    if not t or (lang or "").lower().startswith("en"):
        return t
    if t in _TRANSLATE_CACHE:
        return _TRANSLATE_CACHE[t]
    out = t
    try:
        src = (lang or "").split("-")[0].split("_")[0].lower() or "autodetect"
        url = ("https://api.mymemory.translated.net/get?"
               + _up.urlencode({"q": t[:450], "langpair": f"{src}|en"}))
        req = urllib.request.Request(url, headers={"User-Agent": "tdm/1.0"})
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = json.loads(resp.read().decode("utf-8", "ignore"))
        if isinstance(data, dict) and data.get("responseStatus") == 200:
            cand = ((data.get("responseData") or {}).get("translatedText") or "").strip()
            bad = cand.upper().startswith(("MYMEMORY WARNING", "QUERY LENGTH", "INVALID"))
            if cand and not bad and cand.lower() != t.lower():
                out = cand
    except Exception as e:
        LOG.debug("translate failed, keeping original: %s", e)
    _TRANSLATE_CACHE[t] = out
    return out


def _api_lookup(tid: str, impersonate: str, cookies: str, proxy: str,
                timeout: int) -> tuple[str, str, str, list[dict]]:
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
                return "", "", "", []
            payload = json.loads(resp.read().decode("utf-8", "ignore"))
    except Exception as e:
        LOG.debug("x api failed for %s: %s", tid, e)
        return "", "", "", []
    if not isinstance(payload, dict):
        return "", "", "", []
    return items_from_api(payload, tid)


def resolve_x_media(page_url: str, impersonate: str = "chrome", cookies: str = "",
                    proxy: str = "", timeout: int = 30, translate: bool = True) -> list:
    """Return [Resolved-like dicts] for an X status URL (may be empty)."""
    from .extract import Resolved

    tid = status_id_from_url(page_url)
    if not tid:
        return []
    user, text, lang, items = _api_lookup(tid, impersonate, cookies, proxy, timeout)
    if not items:
        return []
    user = user or user_from_url(page_url)
    text = translate_en(text, lang) if translate else text.strip()
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
        filename=x_filename(user, tid, m["ext"], i, text),
        headers={},
        page_url=page_url,
    ) for i, m in numbered]
