tdm(1) -- robust CLI download manager

# NAME

tdm - download files with browser impersonation, resume and segmentation

# SYNOPSIS

*tdm* [_OPTIONS_] _URL_ [_URL_ ...] [_._]

# OPTIONS

*-o*, *--output* _FILE_
	Output file. Default: auto from Content-Disposition / URL.

*-d*, *--dest* _DIR_
	Destination directory. Default ~/Downloads. A trailing *.* positional
	argument is shorthand for *-d .* (current directory).

*tdm https://speed.hetzner.de/100MB.bin .*

*-x*, *--max-connections* _N_
	Max parallel Range connections (default 8, cap 16).

*--min-split-size* _SIZE_
	Minimum chunk size, e.g. 1M (default 1M).

*--max-items* _N_
	Cap posts for a whole-profile pull (0 = all).

*--no-browser*
	Skip the browser and use Instagram's HTTP profile API. That path is gated
	(429 or an app shell) for most sessions; it stays available for machines
	without chromium and for debugging.

*--headed*
	Show the chromium window. Useful for the first run (log in once — the
	session is kept in _~/.local/share/tdm/browser_) and for debugging.

*--page-delay* _S_
	Seconds to wait between profile feed pages on the HTTP path
	(default 1.0). The browser path pages by scrolling instead.

*--skip-existing*, *--no-skip-existing*
	Keep files already on disk with a matching size. On by default for
	whole-profile pulls, off for single URLs. *--no-skip-existing* forces a
	re-download.

*--limit-rate* _SIZE_
	Global cap, e.g. 500K, 4.2M.

*--impersonate* _PROFILE_
	TLS profile: chrome|safari|firefox|"" (default chrome).

*--user-agent* _UA_, *--referer* _URL_, *--cookies* _FILE_, *--cookies-from-browser* _SPEC_, *--proxy* _URL_

*--timeout* _S_ (30), *--retries* _N_ (5), *--retry-delay* _S_ (1.0)

*--checksum* _SPEC_
	Verify, e.g. sha256:<hex>.

*--dry-run*
	Probe and print plan as JSON, download nothing.

*--no-extract*
	Skip yt-dlp resolver.

*--notify*
	notify-send on done/fail.

*--json*, *--progress* bar|plain|none, *--no-progress*

*--config* _FILE_, *--dump-config*, *--init*

*-v*, *--verbose*, *-q*, *--quiet*

# FILES

_~/.config/tdm/config.toml_, _~/.local/state/tdm/tdm.log_, _*.part_ + _*.part.json_ next to dest.

A whole-profile pull writes into _DEST/@username/_. It opens
_instagram.com/username/_ in headless chromium, injects the stored cookies,
scrolls the grid to the end and downloads each post it finds with the normal
resolver, so carousels expand to several files. Re-running skips files already
on disk. If the browser shows a login wall, run once with *--headed* and log in;
the session persists in _~/.local/share/tdm/browser/_. The HTTP path
(*--no-browser*) keeps its paging cursor in _DEST/@username/.tdm-profile.json_.

# EXAMPLES

*tdm --dry-run https://speed.hetzner.de/100MB.bin*
*tdm -x 8 -o 100M.bin https://speed.hetzner.de/100MB.bin*
*tdm --impersonate chrome --cookies ~/cookies.txt "https://site/page"*
