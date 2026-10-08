from tdm.util import filename_from_cd, parse_retry_after, parse_size, sanitize_filename


def test_parse_size():
    assert parse_size("1M") == 1024**2
    assert parse_size("500K") == 500 * 1024
    assert parse_size("") == 0


def test_cd_filename():
    cd = 'attachment; filename="EURO rates"; filename*=utf-8\'\'%e2%82%ac%20rates'
    assert filename_from_cd(cd, "http://x/y") is not None


def test_retry_after():
    assert parse_retry_after("3", 1.0) == 3.0
    assert parse_retry_after(None, 1.5) == 1.5


def test_sanitize():
    assert sanitize_filename("../../etc/passwd") == "passwd"


def test_token_ips():
    from tdm.util import token_ips
    locked = ("https://cdn.example.com/remote_control.php?file=abc.mp4&acctoken="
              "YzBiMTAzZjFhMmE4ZDdmNWE4NDk2NzA0NGNjMzcxMzNiOWQwNGY4NjUyMDdkYmJiOTc3"
              "MTFhYjk0NWJmMTIyMnwxNzkxNDM4NTk4fDEzNjg3NXx4LXZpZGVvLnR1YmV8MHwyNDA1"
              "OjIwMTo2ODEyOmI5OTA6MzVmZDo1OTRmOmM3YjY6ODk4ZnwwMTliYjM1NmRjNjQ2MjJl"
              "NzZhM2MyZTYxYzIxYWJlMA")
    assert token_ips(locked) == ["2405:201:6812:b990:35fd:594f:c7b6:898f"]
    assert token_ips("https://example.com/f.mp4?x=12345") == []
    assert token_ips("not a url") == []


def test_ip_lock_hint():
    import base64
    import socket
    from unittest.mock import patch
    from tdm.util import ip_lock_hint, token_ips
    blob = base64.b64encode(b"x|1274|2405:201:6812:b990:35fd:594f:c7b6:898f|").decode()
    v6token = f"https://cdn.example.com/f.mp4?tok={blob}"
    assert token_ips(v6token) == ["2405:201:6812:b990:35fd:594f:c7b6:898f"]
    v4only = [(socket.AF_INET, None, None, None, ("1.2.3.4", 0))]
    dual = v4only + [(socket.AF_INET6, None, None, None, ("::1", 0, 0, 0))]
    with patch("socket.getaddrinfo", return_value=v4only):
        hint = ip_lock_hint(v6token)
        assert hint and "IPv6" in hint and "IPv4" in hint
    with patch("socket.getaddrinfo", return_value=dual):
        assert ip_lock_hint(v6token) is None
    with patch("socket.getaddrinfo", side_effect=Exception("dns")):
        assert ip_lock_hint(v6token) is None
    assert ip_lock_hint("https://example.com/f.mp4") is None
