# tdm — robust CLI download manager

Browser-impersonated, resumable, segmented downloader for Arch Linux.

```bash
pip install -e .
tdm --dry-run https://speed.hetzner.de/100MB.bin
tdm -x 8 https://speed.hetzner.de/100MB.bin
tdm --impersonate chrome --cookies ~/cookies.txt "https://site/video-page"
tdm --checksum sha256:<hex> -o out.iso https://example/file.iso
# instagram (native anonymous GraphQL, carousel-safe; use cookies when gated)
tdm --dry-run -y "https://www.instagram.com/reel/<shortcode>/"
tdm -y "https://www.instagram.com/p/<shortcode>/"
# store login cookies once (copied to ~/.config/tdm/cookies.txt, mode 600)
tdm cookies ~/cookies.txt
# then no --cookies flag needed:
tdm -y "https://www.instagram.com/p/<shortcode>/"
# X posts (photos/GIFs/videos, original quality; no login needed)
tdm -y "https://x.com/<user>/status/<id>"
tdm -y "https://twitter.com/<user>/status/<id>/photo/2"  # 2nd photo only
# entire Saved collection (needs login cookies; --max-items caps file count,
# 3 files download in parallel, tune with --parallel N)
tdm -y --cookies ~/cookies.txt "https://www.instagram.com/<you>/saved/"
# entire profile -> ~/Downloads/@<user>/ (drives chromium: Instagram only serves
# profile listings to a trusted client, so a real browser pages the grid and each
# post then goes through the normal resolver; cookies.txt is injected, re-runs
# skip files already on disk)
tdm -y "https://www.instagram.com/<user>/"
tdm -y --max-items 50 "https://www.instagram.com/<user>/"
tdm --dry-run "https://www.instagram.com/<user>/"       # list posts, download nothing
tdm -y --no-browser "https://www.instagram.com/<user>/"  # HTTP path instead (often 429s)
```

Features: `curl_cffi` TLS/JA3 + HTTP/2 impersonation, Sec-Fetch headers,
Referer fallback, yt-dlp resolver, Range segmented resume (`.part`+`.part.json`
records finished byte ranges, so an interrupted run only refetches what is missing),
exp backoff + `Retry-After`, atomic `os.replace`, disk checks, rich progress,
XDG config, `notify-send`, JSON output.
