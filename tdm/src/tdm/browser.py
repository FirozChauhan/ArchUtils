"""Drive a real Chromium over CDP to enumerate a profile grid.

Instagram serves profile listings only to clients it trusts: the plain
HTTP endpoints (`/api/v1/users/*`, `/api/graphql`) answer 429 or the web
app shell to curl_cffi, while the *same cookies in a real browser* show
the grid. So the profile list is collected from the rendered DOM — the
browser does the talking, and tdm downloads the posts it finds through
the normal per-post path.

No third-party deps: Chromium's `--remote-debugging-pipe` speaks
NUL-delimited JSON on fd 3/4, which stdlib can drive directly.
"""
from __future__ import annotations

import http.cookiejar
import json
import logging
import os
import select
import shutil
import subprocess
import time
from pathlib import Path

from .probe import CHROME_UA

LOG = logging.getLogger("tdm")

_EXE_CANDIDATES = (
    "chromium", "chromium-browser", "google-chrome-stable", "google-chrome",
    "brave", "brave-browser", "microsoft-edge-stable", "microsoft-edge",
)

# Every grid tile is a link to /p/<code>/ or /reel/<code>/; the DOM keeps
# them unique per post, so de-duping by href is enough.
GRID_JS = r"""(() => {
  const out = [];
  for (const a of document.querySelectorAll('a[href]')) {
    let p;
    try { p = new URL(a.href, location.origin).pathname; } catch (e) { continue; }
    const m = p.match(/^\/(?:[^/]+\/)?(?:p|reel|reels)\/([A-Za-z0-9_-]{5,})/);
    if (m) out.push(m[1]);
  }
  return [...new Set(out)];
})()"""

LOGIN_JS = r"""(() => {
  const t = (document.body && document.body.innerText) || '';
  return !!document.querySelector('input[name="username"]')
      || /log in|sign up|Create new account/i.test(t.slice(0, 4000));
})()"""


class BrowserError(RuntimeError):
    pass


def find_browser() -> str | None:
    for name in _EXE_CANDIDATES:
        p = shutil.which(name)
        if p:
            return p
    return None


def browser_profile_dir() -> Path:
    base = os.environ.get("XDG_DATA_HOME", str(Path.home() / ".local" / "share"))
    return Path(base).expanduser() / "tdm" / "browser"


class CDP:
    """Minimal Chrome DevTools Protocol client over the pipe transport."""

    def __init__(self, proc: subprocess.Popen, write_fd: int, read_fd: int):
        self.proc = proc
        self.write_fd = write_fd
        self.read_fd = read_fd
        self._next_id = 0
        self._buf = b""

    def _write(self, msg: dict) -> None:
        os.write(self.write_fd, json.dumps(msg).encode() + b"\0")

    def _read(self, deadline: float) -> dict:
        while b"\0" not in self._buf:
            left = deadline - time.time()
            if left <= 0:
                raise BrowserError("browser did not answer in time")
            ready, _, _ = select.select([self.read_fd], [], [], left)
            if not ready:
                raise BrowserError("browser did not answer in time")
            chunk = os.read(self.read_fd, 1 << 16)
            if not chunk:
                raise BrowserError("browser closed the debug pipe")
            self._buf += chunk
        raw, _, self._buf = self._buf.partition(b"\0")
        return json.loads(raw)

    def call(self, method: str, params: dict | None = None, timeout: float = 30.0,
             session_id: str = "") -> dict:
        self._next_id += 1
        mid = self._next_id
        msg: dict = {"id": mid, "method": method, "params": params or {}}
        if session_id:
            msg["sessionId"] = session_id
        self._write(msg)
        deadline = time.time() + timeout
        while True:
            resp = self._read(deadline)
            if resp.get("id") != mid:
                continue  # event or unrelated reply
            if "error" in resp:
                err = resp["error"] or {}
                raise BrowserError(f"{method}: {err.get('message') or err}")
            return resp.get("result") or {}

    def eval(self, expr: str, session_id: str, timeout: float = 30.0):
        res = self.call("Runtime.evaluate",
                        {"expression": expr, "returnByValue": True, "awaitPromise": True},
                        timeout=timeout, session_id=session_id)
        if res.get("exceptionDetails"):
            raise BrowserError(f"page script failed: {res['exceptionDetails'].get('text')}")
        return (res.get("result") or {}).get("value")

    def close(self) -> None:
        try:
            self.call("Browser.close", timeout=5)
        except Exception:
            pass
        try:
            self.proc.wait(timeout=5)
        except Exception:
            self.proc.kill()
        for fd in (self.write_fd, self.read_fd):
            try:
                os.close(fd)
            except OSError:
                pass


def _launch(headless: bool, user_data_dir: Path) -> CDP:
    exe = find_browser()
    if not exe:
        raise BrowserError(
            "no Chromium/Chrome found — install chromium (pacman -S chromium) "
            "or pass --no-browser to use the HTTP path"
        )
    user_data_dir.mkdir(parents=True, exist_ok=True)
    child_in, parent_out = os.pipe()   # parent writes -> chromium fd 3
    parent_in, child_out = os.pipe()   # chromium fd 4 -> parent reads

    argv = [
        exe,
        "--remote-debugging-pipe",
        f"--user-data-dir={user_data_dir}",
        "--no-first-run", "--no-default-browser-check",
        "--disable-background-networking", "--disable-component-update",
        "--disable-sync", "--disable-extensions", "--mute-audio",
        "--window-size=1280,1700",
    ]
    if headless:
        argv += ["--headless=new", "--disable-gpu"]

    def _wire() -> None:
        # os.pipe() fds are O_CLOEXEC; dup2 onto the same number is a no-op
        # that does NOT clear the flag, so mark 3/4 inheritable explicitly or
        # Chromium sees "Remote debugging pipe file descriptors are not open".
        os.dup2(child_in, 3)
        os.dup2(child_out, 4)
        os.set_inheritable(3, True)
        os.set_inheritable(4, True)

    try:
        proc = subprocess.Popen(
            argv + ["about:blank"],
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            close_fds=False, preexec_fn=_wire,
        )
    finally:
        os.close(child_in)
        os.close(child_out)
    return CDP(proc, parent_out, parent_in)


def _load_cookies(path: str) -> list[dict]:
    jar = http.cookiejar.MozillaCookieJar(str(Path(path).expanduser()))
    try:
        jar.load(ignore_discard=True, ignore_expires=True)
    except FileNotFoundError as e:
        raise BrowserError(f"cookies file not found: {path}") from e
    out = []
    for c in jar:
        if not c.domain or "instagram" not in c.domain:
            continue
        item = {"name": c.name, "value": c.value or "",
                "domain": c.domain, "path": c.path or "/",
                "secure": bool(c.secure)}
        if c.expires:
            item["expires"] = int(c.expires)
        out.append(item)
    return out


def collect_profile_posts(username: str, cookies: str = "", limit: int = 0,
                          headed: bool = False, settle: float = 1.2,
                          stale_rounds: int = 3, wait: float = 25.0,
                          log=None) -> list[str]:
    """Return post URLs for @username's grid, newest first.

    ``limit`` caps posts (0 = every post the grid will render). Raises
    BrowserError when the browser cannot start or the grid stays empty.
    """
    cdp = _launch(headless=not headed, user_data_dir=browser_profile_dir())
    try:
        target = cdp.call("Target.createTarget", {"url": "about:blank"})
        sid = cdp.call("Target.attachToTarget",
                       {"targetId": target["targetId"], "flatten": True})["sessionId"]
        cdp.call("Page.enable", session_id=sid)
        cdp.call("Runtime.enable", session_id=sid)
        if cookies:
            cdp.call("Network.enable", session_id=sid)
            kept = 0
            for c in _load_cookies(cookies):
                try:
                    cdp.call("Network.setCookie", c, session_id=sid)
                    kept += 1
                except BrowserError as e:
                    LOG.debug("cookie %s rejected: %s", c.get("name"), e)
            LOG.debug("injected %d cookies", kept)
        try:
            cdp.call("Network.setUserAgentOverride",
                     {"userAgent": CHROME_UA, "acceptLanguage": "en-US,en;q=0.9"},
                     session_id=sid)
        except BrowserError:
            pass

        url = f"https://www.instagram.com/{username}/"
        cdp.call("Page.navigate", {"url": url}, session_id=sid, timeout=60)

        codes = _wait_for_grid(cdp, sid, wait, log)
        if not codes:
            if cdp.eval(LOGIN_JS, sid):
                raise BrowserError(
                    "Instagram shows the logged-in wall in the browser. Run once with\n"
                    "  tdm --headed <profile-url>\n"
                    "and log in in the window that opens (the session is kept in\n"
                    f"  {browser_profile_dir()}\n"
                    "for later runs. `tdm cookies <cookies.txt>` is also injected automatically."
                )
            raise BrowserError(
                f"@{username}: the grid rendered no posts (private, empty, or Instagram "
                "wants a login — retry with --headed to log in once)"
            )

        codes = _scroll_grid(cdp, sid, codes, limit, settle, stale_rounds, log)
        if limit:
            codes = codes[:limit]
        return [f"https://www.instagram.com/p/{c}/" for c in codes]
    finally:
        cdp.close()


def _wait_for_grid(cdp: CDP, sid: str, wait: float, log=None) -> list[str]:
    deadline = time.time() + max(wait, 1)
    codes: list[str] = []
    while time.time() < deadline:
        try:
            codes = cdp.eval(GRID_JS, sid) or []
        except BrowserError as e:
            LOG.debug("grid probe: %s", e)
            codes = []
        if codes:
            return codes
        if cdp.eval(LOGIN_JS, sid):
            # a login wall can still settle into a grid after the session is
            # restored from the profile dir; keep waiting until the deadline
            pass
        time.sleep(0.5)
    return codes


def _scroll_grid(cdp: CDP, sid: str, codes: list[str], limit: int, settle: float,
                 stale_rounds: int, log=None) -> list[str]:
    seen = list(codes)
    known = set(seen)
    stale = 0
    while True:
        if limit and len(seen) >= limit:
            return seen
        try:
            cdp.eval("window.scrollTo(0, document.body.scrollHeight); true", sid)
        except BrowserError as e:
            LOG.debug("scroll failed: %s", e)
            return seen
        time.sleep(max(settle, 0.2))
        try:
            fresh = cdp.eval(GRID_JS, sid) or []
        except BrowserError:
            break
        added = [c for c in fresh if c not in known]
        if added:
            stale = 0
            seen.extend(added)
            known.update(added)
            if log:
                log(len(seen))
        else:
            stale += 1
            if stale >= stale_rounds:
                break
    return seen


def is_available() -> bool:
    return find_browser() is not None
