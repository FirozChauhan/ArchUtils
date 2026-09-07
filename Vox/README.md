# Vox

> Convert videos to HEVC (H.265) or AV1, and images to AVIF, with ffmpeg — in the folder you're already standing in.

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

HEVC needs `libx265`; AV1/AVIF need `libsvtav1` or `libaom-av1`, plus the AVIF muxer for images. Vox probes all of this at startup and refuses with install hints rather than crashing mid-batch.

## Usage

```bash
python3 vox.py                                      # interactive TUI
python3 vox.py --all --codec hevc                   # every video → HEVC, no prompts
python3 vox.py movie.mkv --codec av1 --preset 8 --crf 30
python3 vox.py --all --images --crf 34              # every image → AVIF
python3 vox.py clip.mp4 --copy-audio --force        # keep original audio, overwrite output
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
| `--images` | flag | — | Compress images to AVIF instead of videos |
| `--force` | flag | — | Overwrite files already present in `Vox Output` |
| `--copy-audio` | flag | — | Stream-copy audio instead of re-encoding to AAC |

## Features

- **Batch video conversion** — discovers videos by extension and confirms with ffprobe, then converts the whole selection in one run.
- **HEVC and AV1** — roughly 50% smaller than H.264 for HEVC; AV1 compresses further for newer players.
- **Image → AVIF** — PNG, JPG, BMP, TIFF and WebP through the same preset/CRF flow.
- **One quality dialect** — x265 named presets, SVT-AV1 integers and AOM `cpu-used` values are all normalized to the same two prompts, with per-encoder range validation.
- **Runtime capability detection** — queries `ffmpeg -encoders` / `-muxers` at startup, prefers SVT-AV1 over the far slower libaom, and only offers what can actually run.
- **Loss-prevention guards** — skips sources already in the target codec and outputs that already exist, unless `--force`.
- **Transparency flattening** — the AVIF muxer can't store alpha, so transparent images are composited onto white instead of encoding as garbage.
- **Clean failure semantics** — a partial output is unlinked on any non-ok status, so `Vox Output` never accumulates corrupt files.
- **Full-screen TUI** — arrow-key wizard (mode → files → codec → preset → CRF) with live per-file progress from ffmpeg's `-progress pipe:1`.
- **Interrupt-safe** — Ctrl-C terminates ffmpeg, cleans up and stops the batch, leaving a countable state.

## Configuration

| Option | Type | Default | Description |
|--------|------|---------|-------------|
| Output folder | path | `./Vox Output` | Created next to the source folder |
| HEVC preset | str | `medium` | x265 speed/quality trade-off |
| HEVC CRF | int | `24` | 0–51 |
| AV1 preset | int | `8` (SVT) / `6` (AOM) | 0–13 / 0–11 |
| AV1 + AVIF CRF | int | `30` | 0–63 |

## Environment Variables

None by design — no config files, no env vars. Every choice is a flag or a prompt.

## Development

```bash
python3 -m py_compile vox.py                    # syntax check
python3 vox.py tiny.mp4 --codec hevc --preset ultrafast --crf 40   # fast end-to-end run
```

Single file, ~1400 lines, stdlib only — keep it that way. `validate_preset`, the CRF clamps and the encoder-preference logic are pure and are the natural first `pytest` targets.

## Architecture

```mermaid
flowchart TD
    A[Scan folder<br/>extensions + ffprobe] --> B[Pick files<br/>TUI · args · --all]
    B --> C[Codec · preset · CRF<br/>validated per encoder]
    C --> D[Probe source<br/>codec · duration · pix_fmt]
    D --> E{Guard<br/>already target codec? output exists?}
    E -- skip --> B
    E -- pass --> F[ffmpeg encode<br/>x265 / SVT-AV1 / AOM]
    F --> G[Vox Output/*.mp4 · *.avif]
    F -- failure --> H[Unlink partial output]
```

Startup probes encoder and muxer availability, then each file goes scan → guard → probe → encode → verify. Key decisions: never write in place, so originals are untouchable by construction; ask ffmpeg what it can do instead of assuming a build; flatten alpha rather than emit corrupt AVIF; delete partials on failure.

## Contributing

PRs welcome. Open an issue first for major changes.
