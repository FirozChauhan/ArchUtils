"""XDG-aware TOML config for tdm."""
from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

APP = "tdm"

DEFAULTS: dict = {
    "general": {"dest": "~/Downloads", "config": "", "notify": False},
    "network": {
        "impersonate": "chrome",
        "user_agent": "",
        "referer": "",
        "cookies": "",
        "cookies_from_browser": "",
        "timeout": 30,
        "retries": 5,
        "retry_delay": 1.0,
        "max_connections": 8,
        "min_split_size": "1M",
        "limit_rate": "",
        "proxy": "",
    },
    "output": {"progress": "bar", "quiet": False, "verbose": 0, "json": False, "log_file": ""},
}


def xdg_config_file() -> Path:
    base = os.environ.get("XDG_CONFIG_HOME", str(Path.home() / ".config"))
    return Path(base).expanduser() / APP / "config.toml"


def xdg_state_log() -> Path:
    base = os.environ.get("XDG_STATE_HOME", str(Path.home() / ".local" / "state"))
    return Path(base).expanduser() / APP / "tdm.log"


def default_dest() -> Path:
    # honor user-dirs.dirs if present
    try:
        ud = Path.home() / ".config" / "user-dirs.dirs"
        if ud.exists():
            for line in ud.read_text().splitlines():
                if line.startswith("XDG_DOWNLOAD_DIR"):
                    p = line.split("=", 1)[1].strip().strip('"').replace("$HOME", str(Path.home()))
                    return Path(p).expanduser()
    except Exception:
        pass
    return Path(DEFAULTS["general"]["dest"]).expanduser()


def load_toml(path: Path) -> dict:
    try:
        with open(path, "rb") as f:
            return tomllib.load(f)
    except FileNotFoundError:
        return {}
    except Exception:
        return {}


def deep_merge(base: dict, over: dict) -> dict:
    out = {k: (dict(v) if isinstance(v, dict) else v) for k, v in base.items()}
    for k, v in over.items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = deep_merge(out[k], v)
        else:
            out[k] = v
    return out


def load_config(cli_config: str | None = None) -> dict:
    cfg = {k: dict(v) for k, v in DEFAULTS.items()}
    for cand in [Path("/etc/xdg") / APP / "config.toml", xdg_config_file()]:
        cfg = deep_merge(cfg, load_toml(cand))
    if cli_config:
        cfg = deep_merge(cfg, load_toml(Path(cli_config).expanduser()))
    return cfg


SAMPLE = """\
# tdm config (~/.config/tdm/config.toml)
[general]
dest = "~/Downloads"
notify = false

[network]
impersonate = "chrome"   # chrome | safari | firefox | "" to disable
#user_agent = ""
#referer = ""
#cookies = "~/cookies.txt"
#cookies_from_browser = "firefox"
timeout = 30
retries = 5
retry_delay = 1.0
max_connections = 8
min_split_size = "1M"
#limit_rate = "0"         # e.g. 500K, 4.2M, 0 = unlimited
#proxy = "socks5://127.0.0.1:1080"

[output]
progress = "bar"         # bar | plain | none
"""
