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
```

Features: `curl_cffi` TLS/JA3 + HTTP/2 impersonation, Sec-Fetch headers,
Referer fallback, yt-dlp resolver, Range segmented resume (`.part`+`.part.json`),
exp backoff + `Retry-After`, atomic `os.replace`, disk checks, rich progress,
XDG config, `notify-send`, JSON output.
