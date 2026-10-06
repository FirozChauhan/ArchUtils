from tdm import x as X


def test_url_detection():
    assert X.is_x_url("https://x.com/NASA/status/1234567890123456789")
    assert X.is_x_url("https://twitter.com/NASA/status/1234567890123456789")
    assert X.is_x_url("https://mobile.twitter.com/user/status/123/photo/2")
    assert X.status_id_from_url("https://x.com/u/status/999/?s=20") == "999"
    assert not X.is_x_url("https://www.instagram.com/p/ABC/")


def test_index_selector():
    assert X.x_index_from_url("https://x.com/u/status/1/photo/2") == ("photo", 2)
    assert X.x_index_from_url("https://x.com/u/status/1") is None


def test_photo_upgrade():
    bare = "https://pbs.twimg.com/media/FeWrKrMaYAELAxK.jpg"
    up = X.upgrade_photo_url(bare)
    assert "name=orig" in up and "format=jpg" in up
    small = "https://pbs.twimg.com/media/AbC.jpg?format=jpg&name=small"
    assert "name=orig" in X.upgrade_photo_url(small) and "name=small" not in X.upgrade_photo_url(small)
    assert X.upgrade_photo_url("https://video.twimg.com/x.mp4") == "https://video.twimg.com/x.mp4"


def test_api_parsing_mixed():
    payload = {
        "user_screen_name": "oshtru",
        "media_extended": [
            {"type": "image", "url": "https://pbs.twimg.com/media/FeWrKrMaYAELAxK.jpg"},
            {"type": "video", "url": "https://video.twimg.com/ext_tw_video/1/pu/vid/720x900/a.mp4"},
            {"type": "poll", "url": "https://x.com/i/poll/1"},
        ],
    }
    user, text, items = X.items_from_api(payload, "1577855540407197696")
    assert text == ""
    assert user == "oshtru"
    assert [(m["ext"]) for m in items] == ["jpg", "mp4"]
    assert "name=orig" in items[0]["url"]


def test_api_parsing_gif():
    payload = {"user_screen_name": "Rizdraws", "text": "lol",
               "media_extended": [{"type": "gif",
                                   "url": "https://video.twimg.com/tweet_video/Fdw7SzOXwAQQ-fA.mp4"}]}
    user, text, items = X.items_from_api(payload, "1")
    assert text == "lol"
    assert items == [{"url": "https://video.twimg.com/tweet_video/Fdw7SzOXwAQQ-fA.mp4", "ext": "mp4"}]


def test_api_empty_and_missing():
    assert X.items_from_api({"user_screen_name": "u"}, "1") == ("u", "", [])
    assert X.items_from_api({}, "1") == ("", "", [])


def test_filenames():
    assert X.x_filename("NASA", "123", "mp4") == "@NASA --- 123.mp4"
    assert X.x_filename("NASA", "123", "jpg", 2) == "@NASA --- 123_2.jpg"
    assert X.x_filename("", "123", "jpg", 1) == "@unknown --- 123_1.jpg"
    assert X.x_filename("u", "1", "mp4", text="hello world") == "@u --- hello world.mp4"
    assert X.x_filename("u", "1", "jpg", 2, text="hi") == "@u --- hi_2.jpg"
    assert X.x_filename("u", "1" * 19, "mp4") == "@u --- " + "1" * 19 + ".mp4"


def test_clean_text():
    assert X.clean_text("a  b\nc https://t.co/xyz #tag", 100) == "a b c #tag"
    assert X.clean_text("x" * 300, 10) == "x" * 10
    got = X.clean_text(("word " * 100).strip(), 20)
    assert len(got) <= 20 and not got.endswith(" ")
    assert X.clean_text("", 50) == ""
    full = X.x_filename("u", "1" * 19, "mp4", text="w" * 500)
    assert len(full) <= 200 and full.endswith(".mp4")


def test_index_filtering(monkeypatch=None):
    import io
    import json
    from unittest.mock import patch

    payload = {"user_screen_name": "u", "media_extended": [
        {"type": "image", "url": "https://pbs.twimg.com/media/A.jpg"},
        {"type": "image", "url": "https://pbs.twimg.com/media/B.jpg"},
    ]}

    class R:
        status = 200

        def read(self):
            return json.dumps(payload).encode()

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    with patch("urllib.request.urlopen", return_value=R()):
        out = X.resolve_x_media("https://x.com/u/status/1/photo/2")
    assert len(out) == 1 and out[0].filename == "@u --- 1_2.jpg"
