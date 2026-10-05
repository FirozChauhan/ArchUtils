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
tdm -y --cookies ~/cookies.txt "https://www.instagram.com/reel/<shortcode>/"
```

Features: `curl_cffi` TLS/JA3 + HTTP/2 impersonation, Sec-Fetch headers,
Referer fallback, yt-dlp resolver, Range segmented resume (`.part`+`.part.json`),
exp backoff + `Retry-After`, atomic `os.replace`, disk checks, rich progress,
XDG config, `notify-send`, JSON output.
