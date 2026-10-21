# Vox

> Convert videos to HEVC (H.265) or AV1, and images to AVIF, WebP or JPEG, with ffmpeg — in the folder you're already standing in.

![Python](https://img.shields.io/badge/Python-161B22?style=for-the-badge&logo=python&logoColor=white)
![Linux](https://img.shields.io/badge/Linux-161B22?style=for-the-badge&logo=linux&logoColor=white)
![ffmpeg](https://img.shields.io/badge/ffmpeg-161B22?style=for-the-badge&logo=ffmpeg&logoColor=white)
![x265](https://img.shields.io/badge/x265-161B22?style=for-the-badge&logoColor=white)
![SVT-AV1](https://img.shields.io/badge/SVT--AV1-161B22?style=for-the-badge&logoColor=white)

## Install

```bash
git clone <your-remote> && cd ArchUtils/Vox
```

Python 3.8+, stdlib only — nothing to `pip install`. The one external requirement is `ffmpeg` + `ffprobe` on `PATH`:

```bash
ffmpeg -encoders | grep -E 'x265|svt|aom'   # check what your build supports
```

HEVC needs `libx265`; AV1/AVIF need `libsvtav1` or `libaom-av1`, plus the AVIF muxer for images; WebP needs `libwebp`. Vox probes all of this at startup and refuses with install hints rather than crashing mid-batch.

## Usage

```bash
python3 vox.py                                      # interactive TUI
python3 vox.py --all --codec hevc                   # every video → HEVC, no prompts
python3 vox.py movie.mkv --codec av1 --preset 8 --crf 30
python3 vox.py --all --images --crf 34              # every image → AVIF
python3 vox.py --all --images --resize 1920x-       # …and shrink to 1920px wide first
python3 vox.py --all --images --resize 50%          # …or halve the dimensions
python3 vox.py --all --images --format webp --crf 80 # every image → WebP
python3 vox.py --all --images --format jpeg --crf 3 # every image → JPEG
python3 vox.py clip.mp4 --copy-audio --force        # keep original audio, overwrite output

python3 vox.py mk web                               # build a global named preset
python3 vox.py --use web                            # convert the whole folder with it
python3 vox.py ls                                   # list global presets
python3 vox.py rm web                               # delete one
```

Results always land in a `Vox Output/` subfolder. Originals are never modified.

## API Reference

| Flag | Type | Default | Description |
|------|------|---------|-------------|
| `files` | positional | interactive | Specific files to convert |
| `--codec` | `hevc` \| `av1` | asks | Target video codec |
| `--preset` | str \| int | `medium` / `8` | x265 name (`ultrafast`→`veryslow`) or AV1 number (SVT 0–13, AOM 0–11) |
| `--crf` | int | HEVC `24`, AV1/AVIF `30` | Quality — lower is better and bigger; clamped per codec |
| `--all` | flag | — | Convert every matching file in the folder without asking |
| `--images` | flag | — | Compress images instead of videos |
| `--format` | `avif` \| `webp` \| `jpeg` | asks (AVIF if available) | Image output format |
| `--resize` | spec | asks (original size) | Resize images before encoding: `50%`, `1920x1080` (fit inside box), `1920x-`, `1920`, or `x1080` for height only (`--resize=-x1080` if you want the dashes). Append `!` to allow upscaling |
| `--force` | flag | — | Overwrite files already present in `Vox Output` |
| `--copy-audio` | flag | — | Stream-copy audio instead of re-encoding to AAC |
| `--use` | `NAME` | — | Apply a saved preset to the whole folder |
| `mk` | subcommand | — | Interactively create/update a preset (`mk [name]`) |
| `ls` | subcommand | — | List global presets |
| `rm` | subcommand | — | Delete a global preset (`rm <name>`) |

## Presets

A preset bundles the target and quality settings you use often, stored **globally** as `~/.config/vox/presets.json` (or `$XDG_CONFIG_HOME/vox/presets.json`) — so it works from any folder and never clutters your media directories.

```bash
python3 vox.py mk web          # answer a few prompts, saved as "web"
python3 vox.py --use web       # convert every video/image here with it
python3 vox.py ls              # web  video-av1 (preset=8, CRF=30)
python3 vox.py rm web
```

A preset stores the target (`video-hevc`, `video-av1`, `image-avif`, `image-webp`, `image-jpeg`), the encoder preset, the CRF/quality value, optionally `resize` for image targets, and optionally `copy_audio` / `force`. It never stores file lists — `--use` always processes the whole folder.

Explicit command-line flags always win over the preset, so `vox --use web --crf 34` uses the preset's codec/preset but your CRF. The file is plain JSON and safe to hand-edit; unknown keys and invalid targets are warned about and skipped rather than crashing a run.

```json
{
  "web":   { "target": "video-av1",  "preset": "8", "crf": 30 },
  "thumbs":{ "target": "image-webp", "crf": 80, "resize": "25%", "force": true }
}
```

## Features

- **Batch video conversion** — discovers videos by extension and confirms with ffprobe, then converts the whole selection in one run.
- **HEVC and AV1** — roughly 50% smaller than H.264 for HEVC; AV1 compresses further for newer players.
- **Image → AVIF, WebP or JPEG** — PNG, JPG, BMP, TIFF and WebP inputs. AVIF (AV1 still-image) gives the best ratio; WebP needs the `libwebp` encoder, keeps transparency natively and uses libwebp's 0–100 quality; JPEG uses ffmpeg's built-in `mjpeg` encoder and works on every build, quality set with `-q:v` (2–31).
- **Image resize** — `--resize 50%`, `1920x1080` (fit inside a box, aspect kept), `1920x-`, `x1080`, or a bare `1920` for width only. Shrink-only by default: `min(iw,…)` keeps small sources at their own size, and a trailing `!` is required to upscale. The wizard's image screen is **format only by default**; `r` toggles a Resize row on and off (hidden means off, so what you see is what runs). `←/→` changes the focused row, `↑/↓` moves between rows. Enter on a format starts the normal run (preset → quality); Enter on Resize opens the size list, and **picking a size resizes and starts the conversion immediately** — no return to the menu, no extra confirm. `c` opens **Custom resolution** from anywhere on that screen (`1920x1080`, `1920x-`, `-x1080`, `240x`, `50%`), and confirming it converts straight away. Interactive image runs always overwrite the previous `Vox Output` file (CLI still skips unless `--force`). Format and size are independent: changing one never touches the other, and quality/preset are remembered per format, so AVIF → WebP → AVIF comes back to the AVIF value you set. Presets can store a size.
- **One quality dialect** — x265 named presets, SVT-AV1 integers and AOM `cpu-used` values are all normalized to the same two prompts, with per-encoder range validation.
- **Runtime capability detection** — queries `ffmpeg -encoders` / `-muxers` at startup, prefers SVT-AV1 over the far slower libaom, and only offers what can actually run.
- **Loss-prevention guards** — skips sources already in the target codec and outputs that already exist, unless `--force`.
- **Transparency flattening** — the AVIF muxer and JPEG can't store alpha, so transparent images are composited onto white instead of encoding as garbage; WebP keeps its alpha, so no flattening is applied there.
- **Clean failure semantics** — a partial output is unlinked on any non-ok status, so `Vox Output` never accumulates corrupt files.
- **Named presets** — `vox mk` saves a reusable target+quality bundle to the global `~/.config/vox/presets.json`; `vox --use <name>` runs it over the folder, and CLI flags override individual fields.
- **Full-screen TUI** — arrow-key wizard (mode → files → codec/format → preset → CRF) with live per-file progress from ffmpeg's `-progress pipe:1`.
- **Interrupt-safe** — Ctrl-C terminates ffmpeg, cleans up and stops the batch, leaving a countable state.

## Configuration

| Option | Type | Default | Description |
|--------|------|---------|-------------|
| Output folder | path | `./Vox Output` | Created next to the source folder |
| HEVC preset | str | `medium` | x265 speed/quality trade-off |
| HEVC CRF | int | `24` | 0–51 |
| AV1 preset | int | `8` (SVT) / `6` (AOM) | 0–13 / 0–11 |
| AV1 + AVIF CRF | int | `30` | 0–63 |
| WebP quality (`-q:v`) | int | `75` | 0–100 (higher = better) |
| JPEG quality (`-q:v`) | int | `3` | 2–31 (lower = better) |
| Image resize | spec | none | `50%`, `1920x1080`, `1920x-`, `x1080`; `!` allows upscaling |
| Preset file | path | `~/.config/vox/presets.json` | Global named presets (`vox mk`) |

## Environment Variables

None. The only state is the optional global `~/.config/vox/presets.json` written by `vox mk`; delete it and Vox is back to pure flags-and-prompts. A legacy per-folder `.vox.json` is still read once if no global file exists, to ease migration.

## Development

```bash
python3 -m py_compile vox.py                    # syntax check
python3 vox.py tiny.mp4 --codec hevc --preset ultrafast --crf 40   # fast end-to-end run
```

Single file, stdlib only — keep it that way. `parse_resize`, `validate_preset`, `_sanitize_preset`, the CRF clamps, `apply_preset` (CLI-over-preset precedence) and the encoder-preference logic are pure and are the natural first `pytest` targets.

## Architecture

```mermaid
flowchart TD
    P[--use name<br/>~/.config/vox/presets.json] -.-> C
    A[Scan folder<br/>extensions + ffprobe] --> B[Pick files<br/>TUI · args · --all]
    B --> C[Codec/format · preset · CRF<br/>image: format + resize<br/>validated per encoder]
    C --> D[Probe source<br/>codec · duration · pix_fmt]
    D --> E{Guard<br/>already target codec? output exists?}
    E -- skip --> B
    E -- pass --> F[ffmpeg encode<br/>x265 / SVT-AV1 / AOM / libwebp / mjpeg]
    F --> G[Vox Output/*.mp4 · *.avif · *.webp · *.jpg]
    F -- failure --> H[Unlink partial output]
```

Startup probes encoder and muxer availability, then each file goes scan → guard → probe → encode → verify. Images take one extra filter step: `scale` (if a resize was chosen) followed by the alpha flatten, in a single `-vf` graph. A saved preset (`vox mk`, dashed edge) feeds straight into the settings stage, with explicit CLI flags taking precedence. Key decisions: never write in place, so originals are untouchable by construction; ask ffmpeg what it can do instead of assuming a build; flatten alpha rather than emit corrupt AVIF/JPEG; delete partials on failure; keep preset state global in `~/.config/vox/presets.json`.

## Contributing

PRs welcome. Open an issue first for major changes.
