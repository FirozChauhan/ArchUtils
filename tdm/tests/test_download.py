"""Resume bookkeeping: ranges, not chunk indices."""
import http.server
import json
import re
import tempfile
import threading
from pathlib import Path

from tdm.download import (
    DownloadError,
    _merge_ranges,
    _missing_ranges,
    _plan_chunks,
    segmented_download,
)

SIZE = 1024 * 1024
HALF = SIZE // 2
BODY = bytes((i * 7 + 11) % 256 for i in range(SIZE))


def test_merge_ranges():
    assert _merge_ranges([[5, 9], [0, 3], [4, 4]]) == [[0, 9]]
    assert _merge_ranges([[0, 1], [3, 4]]) == [[0, 1], [3, 4]]
    assert _merge_ranges([]) == []
    assert _merge_ranges([[0, 0], [1, 1]]) == [[0, 1]]


def test_missing_ranges():
    assert _missing_ranges(10, []) == [(0, 9)]
    assert _missing_ranges(10, [[0, 3], [6, 9]]) == [(4, 5)]
    assert _missing_ranges(10, [[0, 9]]) == []
    assert _missing_ranges(10, [[0, 4], [5, 9]]) == []
    assert _missing_ranges(10, [[4, 5]]) == [(0, 3), (6, 9)]


def test_plan_chunks():
    chunks = _plan_chunks([(0, 8 * 1024 * 1024 - 1)], 4, 1024 * 1024)
    assert len(chunks) == 4
    assert chunks[0][0] == 0
    assert chunks[-1][1] == 8 * 1024 * 1024 - 1
    # contiguous, no gaps or overlaps
    for a, b in zip(chunks, chunks[1:], strict=False):
        assert b[0] == a[1] + 1
    # leftovers below min_split stay one request
    assert _plan_chunks([(0, 100)], 4, 1024 * 1024) == [(0, 100)]
    assert _plan_chunks([], 4, 1024) == []


class _Handler(http.server.BaseHTTPRequestHandler):
    ranges: list[str] = []
    fail_at: int = -1
    failing: bool = False

    def log_message(self, *a):
        pass

    def do_GET(self):
        rng = self.headers.get("Range", "")
        type(self).ranges.append(rng)
        m = re.match(r"bytes=(\d+)-(\d+)", rng or "")
        start, end = (int(m.group(1)), int(m.group(2))) if m else (0, SIZE - 1)
        if type(self).failing and start == type(self).fail_at:
            self.send_response(500)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        data = BODY[start:end + 1]
        self.send_response(206 if m else 200)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("ETag", '"e1"')
        if m:
            self.send_header("Content-Range", f"bytes {start}-{end}/{SIZE}")
        self.end_headers()
        self.wfile.write(data)


class _Server:
    def __init__(self):
        self.srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
        self.thread = threading.Thread(target=self.srv.serve_forever, daemon=True)
        self.thread.start()
        self.url = f"http://127.0.0.1:{self.srv.server_address[1]}/f.bin"

    def __enter__(self):
        return self

    def __exit__(self, *a):
        self.srv.shutdown()
        self.srv.server_close()


def _fetched_bytes(ranges):
    total = 0
    starts = []
    for r in ranges:
        m = re.match(r"bytes=(\d+)-(\d+)", r or "")
        if m:
            starts.append(int(m.group(1)))
            total += int(m.group(2)) - int(m.group(1)) + 1
    return total, starts


def test_resume_only_fetches_missing_bytes():
    tmp = Path(tempfile.mkdtemp())
    dest = tmp / "f.bin"
    (tmp / "f.bin.part").write_bytes(BODY[:HALF] + b"\0" * (SIZE - HALF))
    (tmp / "f.bin.part.json").write_text(json.dumps({
        "v": 2, "final_url": "x", "total": SIZE, "etag": '"e1"',
        "done": [[0, HALF - 1]],
    }))
    _Handler.ranges = []
    _Handler.failing = False
    with _Server() as srv:
        segmented_download(srv.url, dest, total=SIZE, resumable=True, etag='"e1"',
                           connections=2, min_split="64K", progress_mode="none",
                           quiet=True, retries=1, retry_delay=0.01)
    assert dest.read_bytes() == BODY
    total, starts = _fetched_bytes(_Handler.ranges)
    assert total == SIZE - HALF, _Handler.ranges       # only the missing half
    assert starts and min(starts) >= HALF, starts      # nothing below the mark
    assert not (tmp / "f.bin.part").exists()
    assert not (tmp / "f.bin.part.json").exists()


def test_failed_chunk_is_not_recorded_then_resumed():
    tmp = Path(tempfile.mkdtemp())
    dest = tmp / "f.bin"
    state = tmp / "f.bin.part.json"
    _Handler.ranges = []
    _Handler.fail_at = HALF
    _Handler.failing = True
    with _Server() as srv:
        try:
            segmented_download(srv.url, dest, total=SIZE, resumable=True, etag='"e1"',
                               connections=2, min_split="64K", progress_mode="none",
                               quiet=True, retries=0, retry_delay=0.01)
        except DownloadError:
            pass
        else:
            raise AssertionError("expected the failing chunk to raise")
        # the finished range is on disk, the failed one is not
        done = json.loads(state.read_text())["done"]
        assert done == [[0, HALF - 1]], done

        _Handler.failing = False
        _Handler.ranges = []
        segmented_download(srv.url, dest, total=SIZE, resumable=True, etag='"e1"',
                           connections=2, min_split="64K", progress_mode="none",
                           quiet=True, retries=1, retry_delay=0.01)
    assert dest.read_bytes() == BODY
    total, starts = _fetched_bytes(_Handler.ranges)
    assert total == HALF, _Handler.ranges              # only the failed half again
    assert min(starts) >= HALF


def test_changed_etag_forces_full_redownload():
    tmp = Path(tempfile.mkdtemp())
    dest = tmp / "f.bin"
    (tmp / "f.bin.part").write_bytes(BODY[:HALF] + b"\0" * (SIZE - HALF))
    (tmp / "f.bin.part.json").write_text(json.dumps({
        "v": 2, "final_url": "x", "total": SIZE, "etag": '"OLD"',
        "done": [[0, HALF - 1]],
    }))
    _Handler.ranges = []
    _Handler.failing = False
    with _Server() as srv:
        segmented_download(srv.url, dest, total=SIZE, resumable=True, etag='"e1"',
                           connections=2, min_split="64K", progress_mode="none",
                           quiet=True, retries=1, retry_delay=0.01)
    assert dest.read_bytes() == BODY
    total, _ = _fetched_bytes(_Handler.ranges)
    assert total == SIZE, _Handler.ranges              # stale sidecar ignored


def test_old_index_sidecar_is_ignored():
    tmp = Path(tempfile.mkdtemp())
    dest = tmp / "f.bin"
    (tmp / "f.bin.part").write_bytes(b"\0" * SIZE)
    (tmp / "f.bin.part.json").write_text(json.dumps({
        "final_url": "x", "total": SIZE, "etag": '"e1"', "done": [0, 1],
    }))
    _Handler.ranges = []
    _Handler.failing = False
    with _Server() as srv:
        segmented_download(srv.url, dest, total=SIZE, resumable=True, etag='"e1"',
                           connections=2, min_split="64K", progress_mode="none",
                           quiet=True, retries=1, retry_delay=0.01)
    assert dest.read_bytes() == BODY
    total, _ = _fetched_bytes(_Handler.ranges)
    assert total == SIZE
