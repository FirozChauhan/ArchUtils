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
