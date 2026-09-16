tdm(1) -- robust CLI download manager

# NAME

tdm - download files with browser impersonation, resume and segmentation

# SYNOPSIS

*tdm* [_OPTIONS_] _URL_ [_URL_ ...]

# OPTIONS

*-o*, *--output* _FILE_
	Output file. Default: auto from Content-Disposition / URL.

*-d*, *--dest* _DIR_
	Destination directory. Default ~/Downloads.

*-x*, *--max-connections* _N_
	Max parallel Range connections (default 8, cap 16).

*--min-split-size* _SIZE_
	Minimum chunk size, e.g. 1M (default 1M).

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

# EXAMPLES

*tdm --dry-run https://speed.hetzner.de/100MB.bin*
*tdm -x 8 -o 100M.bin https://speed.hetzner.de/100MB.bin*
*tdm --impersonate chrome --cookies ~/cookies.txt "https://site/page"*
