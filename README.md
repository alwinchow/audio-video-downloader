# audio-video-downloader

A self-hosted web app for pulling audio or video out of YouTube links,
with a built-in preview player so you can trim to exactly the part you want
before downloading.

Flask + [yt-dlp](https://github.com/yt-dlp/yt-dlp) + ffmpeg. Access is gated
behind a shared code so it isn't open to the world.

## What it does

**Audio tab**
- Paste a link, get a preview you can play and scrub
- Set start/end by dragging a two-handle slider or typing times (`1:30`, `1:02:45`)
- Download as MP3 (128 / 192 / 320 kbps) or the original audio, trimmed to your selection

**Video tab**
- Lists the resolutions the video actually offers
- Low-resolution preview for picking cut points
- Downloads a merged MP4 (H.264 + AAC) at your chosen resolution, trimmed

Previews are deliberately fetched at the *lowest* available quality so scrubbing
a long video doesn't mean downloading hundreds of megabytes. The real quality is
only fetched when you actually download, and only for the range you selected.

## Running locally

```bash
pip install -r requirements.txt   # needs ffmpeg on PATH as well
python server.py                  # http://127.0.0.1:5000
```

The access code defaults to `family123` locally. Override it with the
`ACCESS_CODE` environment variable.

Windows users can double-click `Start Downloader.bat` instead.

## Windows desktop app (no Python required)

`desktop.py` runs the Flask app in a background thread and shows it in a
native window via [pywebview](https://pywebview.flowrl.com/), using the Edge
WebView2 runtime already built into Windows 11 — a real Chromium engine, no
browser tab, no separate install.

Build a single `.exe` with PyInstaller:

```bash
pip install pywebview pyinstaller
pyinstaller ytdownloader.spec
# -> dist/YTDownloader.exe
```

The result is self-contained — Python, Flask, yt-dlp and ffmpeg are all
bundled in, so it runs on a machine with none of those installed. Downloads
and any saved YouTube cookies go in `%LOCALAPPDATA%\YTDownloader`, which
persists across runs and doesn't need Program Files write access.

Why this exists: YouTube blocks requests from datacenter IPs (Azure, AWS,
GCP) — a cloud-hosted instance of this app gets "Sign in to confirm you're
not a bot" and no cookie or client-spoofing workaround reliably clears it (see
`DEPLOY.md`). Running from a home connection has none of that problem. This
`.exe` is the easiest way to hand the app to someone without them needing
Python or a terminal.

## Deploying

See [DEPLOY.md](DEPLOY.md) for Azure App Service instructions, including the
free-tier limits worth knowing about before you deploy the video tab.

## Notes

- `yt-dlp` is intentionally unpinned in `requirements.txt`. YouTube changes
  things every few months and older versions break with
  `HTTP Error 403: Forbidden`, so each deploy should pick up the latest release.
- Downloads are cleaned up automatically: after 1 hour, past 20 files, or past
  700 MB total — whichever comes first.
- ffmpeg is required, not optional. It does every trim, builds the audio
  preview, and merges video with its sound.
