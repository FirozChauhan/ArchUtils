import types
from tdm import instagram as ig
from tdm.extract import _from_ytdlp_info


def test_shortcode_detection():
    assert ig.is_instagram_url("https://www.instagram.com/p/ABC123_-xyz/")
    assert ig.is_instagram_url("https://www.instagram.com/reel/Chunk8-jurw/")
    assert ig.is_instagram_url("https://www.instagram.com/username/reels/Cop84x6u7CP/")
    assert not ig.is_instagram_url("https://example.com/file.mp4")


def test_shortcode_roundtrip():
    assert ig.shortcode_from_url("https://www.instagram.com/reel/Chunk8-jurw/?x=1") == "Chunk8-jurw"
    pk = ig.shortcode_to_pk("Chunk8-jurw")
    assert ig.pk_to_shortcode(pk) == "Chunk8-jurw"
    # known yt-dlp vector: media_id derivation must be numeric
    assert ig.shortcode_to_pk("aye83DjauH").isdigit()


def test_product_info_video():
    items = ig.items_from_product_info(
        {"video_versions": [{"url": "https://cdn/v.mp4", "width": 720, "height": 1280}],
         "user": {"username": "someone"}}, "ABC123")
    assert len(items) == 1 and items[0].url == "https://cdn/v.mp4"
    assert items[0].filename == "@someone --- ABC123.mp4"


def test_product_info_carousel():
    items = ig.items_from_product_info(
        {"carousel_media": [
            {"video_versions": [{"url": "https://cdn/1.mp4", "width": 720}]},
            {"image_versions2": {"candidates": [{"url": "https://cdn/2.jpg", "width": 1080}]}},
        ]}, "CAR1")
    assert [m.ext for m in items] == ["mp4", "jpg"]
    assert items[0].filename == "@unknown --- CAR1_1.mp4"


def test_html_fallback():
    html = '<meta property="og:video" content="https://cdn/v.mp4"><meta property="og:image" content="https://cdn/i.jpg">'
    items = ig.items_from_html(html, "ZZZ")
    assert items and items[0].url == "https://cdn/v.mp4"
    html2 = '{"display_url":"https:\\/\\/cdn\\/i.jpg"}'
    items2 = ig.items_from_html(html2, "ZZZ")
    assert items2 and items2[0].ext == "jpg"


def test_ytdlp_playlist_expands():
    info = {"_type": "playlist", "entries": [
        {"url": "https://cdn/1.mp4", "_filename": "a.mp4"},
        {"url": "https://cdn/2.mp4", "_filename": "b.mp4"},
    ]}
    out = _from_ytdlp_info("https://www.instagram.com/p/X/", info)
    assert [r.url for r in out] == ["https://cdn/1.mp4", "https://cdn/2.mp4"]


def test_saved_url():
    assert ig.is_saved_url("https://www.instagram.com/someuser/saved/")
    assert ig.is_saved_url("https://www.instagram.com/someuser/saved/all-posts/")
    assert not ig.is_saved_url("https://www.instagram.com/p/ABC123/")


def test_best_image_skips_square_crop():
    cands = [
        {"url": "https://cdn/full.webp", "width": 1080, "height": 1440},
        {"url": "https://cdn/crop.webp", "width": 1080, "height": 1080},
        {"url": "https://cdn/small.webp", "width": 720, "height": 960},
    ]
    best = ig._best_image(cands, 1080, 1440)
    assert best and best["url"] == "https://cdn/full.webp"
    # no original dims: largest area still wins over the square crop
    best2 = ig._best_image(cands)
    assert best2 and best2["url"] == "https://cdn/full.webp"


def test_parallel_multi(monkeypatch=None):
    import threading
    import time
    import types
    from unittest.mock import patch

    import tdm.cli as cli

    cfg = {"network": {}, "general": {}, "output": {}}
    args = types.SimpleNamespace(
        impersonate=None, user_agent="", cookies="", cookies_from_browser="",
        proxy="", timeout=30, retries=1, retry_delay=0.1, max_connections=2,
        min_split_size="1M", limit_rate="", no_progress=True, progress=None,
        quiet=True, referer="", no_extract=False, output=None, dest="/tmp",
        no_prompt=False, dry_run=False, json=False, list_name="", checksum=None,
        max_items=0, parallel=3,
    )
    items = [types.SimpleNamespace(url=f"https://cdn/{i}.mp4", filename=f"f{i}.mp4",
                                   headers={}, page_url="https://www.instagram.com/p/X/")
             for i in range(6)]
    live = {"cur": 0, "max": 0}
    lock = threading.Lock()

    def fake_fetch(cfg_, args_, raw, dl, name, headers, page, prefer_resolved=False):
        with lock:
            live["cur"] += 1
            live["max"] = max(live["max"], live["cur"])
        try:
            time.sleep(0.05)
            return {"url": raw, "file": name, "done": name}
        finally:
            with lock:
                live["cur"] -= 1

    with patch.object(cli, "resolve_all", return_value=items), \
         patch.object(cli, "_fetch_single", side_effect=fake_fetch):
        plan = cli.one(cfg, args, "https://www.instagram.com/p/X/")
    assert plan["count"] == 6
    assert live["max"] > 1, f"expected concurrency, saw max {live['max']}"
    assert live["max"] <= 3


def test_saved_feed_parsing():
    # saved items wrap v1 media under "media" with code/user/carousel
    media = {"code": "ABC1", "user": {"username": "poster"},
             "carousel_media": [
                 {"video_versions": [{"url": "https://cdn/1.mp4", "width": 720}]},
                 {"image_versions2": {"candidates": [{"url": "https://cdn/2.jpg", "width": 1080}]}},
             ]}
    items = ig.items_from_product_info(media, "ABC1", "poster")
    assert [m.filename for m in items] == ["@poster --- ABC1_1.mp4", "@poster --- ABC1_2.jpg"]


# --- whole-profile downloads -------------------------------------------------

class _Resp:
    def __init__(self, payload=None, status=200, text="", ctype=""):
        self._payload = payload
        self.status_code = status
        self.text = text
        self.headers = {"content-type": ctype} if ctype else {}

    def json(self):
        if self._payload is None:
            raise ValueError("no json body")
        return self._payload


class _FakeSess:
    """Stands in for curl_cffi.Session: site root, profile info, feed pages."""

    def __init__(self, pages, profile=None):
        self.pages = list(pages)
        self.profile = profile
        self.cookies = types.SimpleNamespace(get=lambda _k: "")
        self.calls: list[tuple[str, dict]] = []

    def get(self, url, headers=None, params=None, timeout=None, **kw):
        self.calls.append((url, dict(params or {})))
        if "web_profile_info" in url:
            return _Resp(self.profile)
        if url.rstrip("/") == "https://www.instagram.com":
            return _Resp(text='["LSD",[],{"token":"tok"}]')
        return _Resp(self.pages.pop(0) if self.pages
                     else {"items": [], "more_available": False})


def test_profile_url_detection():
    assert ig.is_profile_url("https://www.instagram.com/nasa/")
    assert ig.is_profile_url("https://instagram.com/nasa")
    assert ig.is_profile_url("https://www.instagram.com/some.user_name/?hl=en")
    assert ig.profile_username_from_url("https://www.instagram.com/nasa/") == "nasa"
    for not_profile in (
        "https://www.instagram.com/p/ABC123/",
        "https://www.instagram.com/reel/Chunk8-jurw/",
        "https://www.instagram.com/someuser/saved/",
        "https://www.instagram.com/explore/tags/cats/",
        "https://www.instagram.com/nasa/reels/",
        "https://example.com/nasa",
    ):
        assert not ig.is_profile_url(not_profile), not_profile


def test_profile_info_and_paging():
    profile = {"data": {"user": {
        "id": "123", "username": "nasa", "is_private": False,
        "edge_owner_to_timeline_media": {"count": 5}}}}
    pages = [
        {"items": [
            {"code": "A1", "video_versions": [{"url": "https://cdn/1.mp4", "width": 720}]},
            {"code": "A2", "carousel_media": [
                {"image_versions2": {"candidates": [{"url": "https://cdn/2.jpg", "width": 1080}]}},
                {"video_versions": [{"url": "https://cdn/3.mp4", "width": 720}]}]},
        ], "more_available": True, "next_max_id": "CUR1"},
        # pinned post repeats on page 2 -> must not be downloaded twice
        {"items": [{"code": "A1", "video_versions": [{"url": "https://cdn/1.mp4"}]}],
         "more_available": False, "next_max_id": ""},
    ]
    sess = _FakeSess(pages, profile)
    info = ig.profile_info("nasa", sess=sess)
    assert (info["id"], info["username"], info["count"], info["private"]) == (
        "123", "nasa", 5, False)
    assert info["first_count"] == 0 and info["end_cursor"] == ""
    out = list(ig.iter_profile_media("nasa", "123", sess=sess, delay=0))
    names = [m.filename for p in out for m in p.items]
    assert names == ["@nasa --- A1.mp4", "@nasa --- A2_1.jpg", "@nasa --- A2_2.mp4"]
    assert [p.cursor for p in out] == ["CUR1", ""]
    assert out[1].items == [] and out[1].more is False
    # the second page must carry the cursor from the first
    assert sess.calls[-1][1].get("max_id") == "CUR1"


def test_profile_rate_limit_and_auth_wall():
    from unittest.mock import patch

    profile = {"data": {"user": {"id": "1", "username": "x"}}}

    class Flaky(_FakeSess):
        def get(self, url, headers=None, params=None, timeout=None, **kw):
            if "web_profile_info" in url:
                self.calls.append((url, {}))
                if len(self.calls) < 2:  # profile_info retries once, then gives up
                    return _Resp(None, 429)
                return _Resp(profile)
            return _Resp(text='["LSD",[],{"token":"t"}]')

    with patch("time.sleep"):
        assert ig.profile_info("x", sess=Flaky([]))["id"] == "1"

    class Wall(_FakeSess):
        def get(self, url, headers=None, params=None, timeout=None, **kw):
            if "web_profile_info" in url:
                return _Resp({"message": "login_required"}, 401)
            return _Resp(text="")

    try:
        ig.profile_info("x", sess=Wall([]))
    except RuntimeError as e:
        assert "logged-in session" in str(e)
    else:
        raise AssertionError("expected RuntimeError on 401")


def _args(**over):
    base = dict(
        impersonate=None, user_agent="", cookies="", cookies_from_browser="",
        proxy="", timeout=30, retries=1, retry_delay=0.1, max_connections=2,
        min_split_size="1M", limit_rate="", no_progress=True, progress=None,
        quiet=True, referer="", no_extract=False, output=None, dest="",
        no_prompt=True, dry_run=False, json=False, list_name="", checksum=None,
        max_items=0, parallel=3, page_delay=0, skip_existing=None,
        profile_dir="", no_resume=False, no_browser=False, headed=False,
    )
    base.update(over)
    return types.SimpleNamespace(**base)


def test_fetch_single_skips_existing_file():
    import tempfile
    from pathlib import Path
    from unittest.mock import patch

    import tdm.cli as cli

    tmp = Path(tempfile.mkdtemp())
    name = "@nasa --- A1.mp4"
    (tmp / name).write_bytes(b"x" * 10)
    probe = types.SimpleNamespace(
        total=10, resumable=False, final_url="https://cdn/1.mp4", content_type="video/mp4",
        etag="", status=200, filename=name, referer="")
    args = _args(dest=str(tmp), skip_existing=True)
    with patch.object(cli, "make_session", return_value=None), \
         patch.object(cli, "probe_url", return_value=probe), \
         patch.object(cli, "segmented_download",
                      side_effect=AssertionError("must not download")):
        plan = cli._fetch_single(cfg={"network": {}, "general": {}, "output": {}},
                                 args=args, raw_url="https://cdn/1.mp4",
                                 dl_url="https://cdn/1.mp4", resolved_name=name,
                                 headers_extra={},
                                 page_url="https://www.instagram.com/p/X/",
                                 prefer_resolved=True)
    assert plan["skipped"] is True
    assert plan["done"] == str(tmp / name)

    # size mismatch means a truncated file: re-download, do not skip
    (tmp / name).write_bytes(b"x" * 4)
    with patch.object(cli, "make_session", return_value=None), \
         patch.object(cli, "probe_url", return_value=probe), \
         patch.object(cli, "segmented_download",
                      side_effect=AssertionError("must not download")):
        try:
            cli._fetch_single(cfg={"network": {}, "general": {}, "output": {}},
                              args=args, raw_url="https://cdn/1.mp4",
                              dl_url="https://cdn/1.mp4", resolved_name=name,
                              headers_extra={},
                              page_url="https://www.instagram.com/p/X/",
                              prefer_resolved=True)
        except AssertionError as e:
            assert "must not download" in str(e)
        else:
            raise AssertionError("expected a download for a size mismatch")


def test_profile_info_inline_first_page():
    profile = {"data": {"user": {
        "id": "9", "username": "u", "is_private": False,
        "edge_owner_to_timeline_media": {
            "count": 40,
            "edges": [
                {"node": {"shortcode": "S1", "is_video": True, "video_url": "https://cdn/a.mp4"}},
                {"node": {"shortcode": "S2", "display_url": "https://cdn/b.jpg"}},
                {"node": {"shortcode": "S3", "edge_sidecar_to_children": {"edges": [
                    {"node": {"display_url": "https://cdn/c1.jpg"}},
                    {"node": {"is_video": True, "video_url": "https://cdn/c2.mp4"}}]}}},
            ],
            "page_info": {"end_cursor": "EC", "has_next_page": True}}}}}
    sess = _FakeSess([], profile)
    info = ig.profile_info("u", sess=sess)
    assert [m.filename for m in info["first_page"]] == [
        "@u --- S1.mp4", "@u --- S2.jpg", "@u --- S3_1.jpg", "@u --- S3_2.mp4"]
    assert info["end_cursor"] == "EC" and info["has_next"] is True
    assert info["first_count"] == 3

    before = len(sess.calls)
    page = next(ig.iter_profile_media("u", "9", sess=sess, delay=0,
                                      first_page=info["first_page"],
                                      first_cursor=info["end_cursor"],
                                      first_count=info["first_count"]))
    assert len(sess.calls) == before, "page 1 must not trigger a request"
    assert page.items == info["first_page"] and page.cursor == "EC"
    assert page.posts == 3 and page.more is True


def test_paging_shell_reports_html_not_json():
    profile = {"data": {"user": {"id": "9", "username": "u", "is_private": False,
                                 "edge_owner_to_timeline_media": {
                                     "count": 40, "edges": [],
                                     "page_info": {"end_cursor": "EC", "has_next_page": True}}}}}

    class Shell(_FakeSess):
        def get(self, url, headers=None, params=None, timeout=None, **kw):
            self.calls.append((url, {}))
            if "web_profile_info" in url:
                return _Resp(profile)
            if url.rstrip("/") == "https://www.instagram.com":
                return _Resp(text='["LSD",[],{"token":"t"}]')
            return _Resp(text="<html>app shell</html>", ctype="text/html")

    try:
        list(ig.iter_profile_media("u", "9", sess=Shell([]), delay=0))
    except RuntimeError as e:
        assert "HTML shell" in str(e)
    else:
        raise AssertionError("expected a clear paging error")


def test_one_profile_end_to_end_wiring():
    import tempfile
    from pathlib import Path
    from unittest.mock import patch

    import tdm.cli as cli

    tmp = Path(tempfile.mkdtemp())
    media = [ig.IGMedia(url="https://cdn/1.mp4", ext="mp4", filename="@u --- A1.mp4"),
             ig.IGMedia(url="https://cdn/2.jpg", ext="jpg", filename="@u --- A2.jpg")]
    info = {"id": "9", "username": "u", "count": 2, "private": False,
            "first_page": media, "first_count": 2, "end_cursor": "", "has_next": False}
    args = _args(dest=str(tmp), cookies="~/.x", no_browser=True)

    def fake_many(cfg_, args_, raw, items, prefer):
        assert args_.profile_dir.endswith("@u"), args_.profile_dir
        assert args_.skip_existing is True, "profile pulls must skip existing files"
        return {"files": [m.filename for m in items], "count": len(items), "skipped": 0}

    with patch.object(cli, "make_session", return_value=None), \
         patch("tdm.instagram.profile_info", return_value=info), \
         patch.object(cli, "_fetch_many", side_effect=fake_many):
        plan = cli.one_profile({"network": {}, "general": {}, "output": {}}, args,
                               "https://www.instagram.com/u/")
    assert plan["count"] == 2 and plan["profile"] == "@u"
    assert plan["dir"] == str(tmp / "@u") and (tmp / "@u").is_dir()
    # exhausted feed -> resume cursor removed
    assert not (tmp / "@u" / ".tdm-profile.json").exists()


def test_one_profile_marks_partial_when_paging_blocked():
    import tempfile
    from unittest.mock import patch

    import tdm.cli as cli

    tmp = tempfile.mkdtemp()
    media = [ig.IGMedia(url="https://cdn/1.mp4", ext="mp4", filename="@u --- A1.mp4")]
    info = {"id": "9", "username": "u", "count": 99, "private": False,
            "first_page": media, "first_count": 1, "end_cursor": "EC", "has_next": True}
    args = _args(dest=tmp, cookies="~/.x", no_browser=True)

    def blocked(*a, **k):
        yield ig.ProfilePage(items=media, cursor="EC", posts=1, more=True)
        raise RuntimeError("@u feed: Instagram answered with the web-app HTML shell instead of JSON")

    with patch.object(cli, "make_session", return_value=None), \
         patch("tdm.instagram.profile_info", return_value=info), \
         patch("tdm.instagram.iter_profile_media", side_effect=blocked), \
         patch.object(cli, "_fetch_many",
                      return_value={"files": ["@" + "u --- A1.mp4"], "count": 1, "skipped": 0}):
        plan = cli.one_profile({"network": {}, "general": {}, "output": {}}, args,
                               "https://www.instagram.com/u/")
    assert plan["partial"] is True and "HTML shell" in plan["paging_error"]
    assert plan["count"] == 1
    # cursor kept so a later run can continue
    assert (cli.Path(tmp) / "@u" / ".tdm-profile.json").exists()


# --- browser-driven profile enumeration --------------------------------------

def test_cookie_loader_keeps_only_instagram():
    import tempfile
    from pathlib import Path

    from tdm.browser import _load_cookies

    p = Path(tempfile.mkdtemp()) / "cookies.txt"
    p.write_text(
        "# Netscape HTTP Cookie File\n"
        ".instagram.com\tTRUE\t/\tTRUE\t0\tsessionid\tSID123\n"
        ".instagram.com\tTRUE\t/\tFALSE\t0\tcsrftoken\tCSRF\n"
        ".example.com\tTRUE\t/\tFALSE\t0\tother\tnope\n"
    )
    got = _load_cookies(str(p))
    assert {c["name"] for c in got} == {"sessionid", "csrftoken"}
    assert all("instagram" in c["domain"] for c in got)
    assert got[0]["secure"] in (True, False)


def test_one_profile_uses_browser_grid():
    import tempfile
    from unittest.mock import patch

    import tdm.cli as cli

    args = _args(dest=tempfile.mkdtemp(), cookies="~/.x")
    posts = ["https://www.instagram.com/p/AAA111/",
             "https://www.instagram.com/p/BBB222/"]
    with patch("tdm.browser.is_available", return_value=True), \
         patch("tdm.browser.collect_profile_posts", return_value=posts), \
         patch.object(cli, "one", return_value={"done": "f.jpg", "count": 1}) as dl:
        plan = cli.one_profile({"network": {}, "general": {}, "output": {}}, args,
                               "https://www.instagram.com/u/")
    assert plan["mode"] == "browser" and plan["count"] == 2
    assert plan["posts_total"] == 2 and dl.call_count == 2
    # browser runs get the per-user dir and skip-existing by default
    assert args.skip_existing is True and args.profile_dir.endswith("@u")


def test_one_profile_browser_dry_run_lists_posts():
    import tempfile
    from unittest.mock import patch

    import tdm.cli as cli

    args = _args(dest=tempfile.mkdtemp(), cookies="", dry_run=True)
    posts = ["https://www.instagram.com/p/AAA111/"]
    with patch("tdm.browser.is_available", return_value=True), \
         patch("tdm.browser.collect_profile_posts", return_value=posts), \
         patch.object(cli, "one", side_effect=AssertionError("must not download")):
        plan = cli.one_profile({"network": {}, "general": {}, "output": {}}, args,
                               "https://www.instagram.com/u/")
    assert plan["posts"] == posts and plan["count"] == 0


def test_no_browser_flag_uses_http_path():
    import tempfile
    from unittest.mock import patch

    import tdm.cli as cli

    args = _args(dest=tempfile.mkdtemp(), cookies="", no_browser=True)
    with patch.object(cli, "make_session", return_value=None), \
         patch("tdm.browser.collect_profile_posts",
               side_effect=AssertionError("browser must not be used")):
        try:
            cli.one_profile({"network": {}, "general": {}, "output": {}}, args,
                            "https://www.instagram.com/u/")
        except Exception as e:
            assert "logged-in session" in str(e), e
