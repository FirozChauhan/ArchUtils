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
