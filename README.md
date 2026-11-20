# YtClipping — AutoClip Watcher

An automation layer on top of [AutoClip](https://github.com/zhouxiaoka/autoclip) that
watches selected YouTube channels, clips their new uploads with AutoClip, and
optionally publishes the clips to your own YouTube channel.

```
monitors ──► detects new upload ──► autoclip produce ──► autoclip publish
(RSS)         (state.json)            (clips + kit)        (your channel)
```

It does **not** modify AutoClip. It calls the `autoclip` CLI (`produce` +
`publish`) and keeps its own `state.json` so the same video is never processed
twice.

## Requirements

1. **Python 3.10+** (no third-party packages needed for the watcher itself).
2. **AutoClip installed** and on `PATH`:
   ```bash
   uvx autoclip-mcp        # or:  pip install autoclip-mcp
   autoclip --version
   ```
3. **AutoClip model config** saved (desktop app settings or `AUTOCLIP_API_KEY`).
4. **For publishing to YouTube:** either connect your channel through this
   project's built-in **direct YouTube uploader** (recommended — see below) or an
   [Upload-Post](https://www.upload-post.com) account. Direct upload uses your
   own Google OAuth, so there's no shared app rate limit.
5. Optional: `yt-dlp` (`pip install yt-dlp`) as a channel-id resolution fallback.

## Model / provider config (`.env`)

AutoClip needs a model to analyze videos. The watcher loads a gitignored `.env`
and passes it to the AutoClip CLI. Copy `.env.example` → `.env` and fill in one
provider. Example (Google Gemini):

```ini
LLM_PROVIDER=gemini
API_GEMINI_API_KEY=your_key
API_MODEL_NAME=gemini-3.8-flash
API_MAX_TOKENS=4096
```

> **Windows data-dir fix (important).** On this setup the default
> `%APPDATA%\AutoClip` data dir breaks — AutoClip never places the input file in
> `projects/<id>/raw/`, so `produce` fails with "input not found". Point it at a
> plain local dir instead:
> ```ini
> AUTOCLIP_DATA_DIR=D:\acdata
> ```
> (Any writable path without spaces works. This is set in the shipped `.env`.)

Verify the whole chain before watching channels:

```bash
python -c "from autoclip_watch import load_dotenv; load_dotenv()"
autoclip doctor          # check ffmpeg / model
```

Note: `autoclip doctor` can show a false "model connection failed" for
"thinking" Gemini models (its probe uses only 10 output tokens). Real `produce`
calls use `API_MAX_TOKENS` (4096) and work fine.

## Quickest: paste a link, get Shorts on your channel

```bash
python autoclip_watch.py paste "https://www.youtube.com/watch?v=VIDEO_ID"
```

That's the whole flow: it downloads the video, AutoClip finds the best moments,
renders vertical Shorts, and uploads the top clips to your connected YouTube
channel. Options:

```bash
python autoclip_watch.py paste "<url>" --platform youtube_shorts --clips 3
python autoclip_watch.py paste "<url>" --privacy unlisted   # private | unlisted | public
python autoclip_watch.py paste "<url>" --no-publish         # produce only, no upload
```

### One-time: connect your YouTube channel (direct upload)

This project uploads straight to YouTube with the official API — no Upload-Post,
no shared rate limit. One-time setup:

1. Go to the [Google Cloud Console](https://console.cloud.google.com/) → create
   or select a project.
2. **APIs & Services → Library** → enable **YouTube Data API v3**.
3. **OAuth consent screen** → External → add your Google account as a **Test
   user** (so it won't be blocked while unverified).
4. **Credentials → Create Credentials → OAuth client ID → Desktop app** →
   download the JSON.
5. Save it as **`client_secret.json`** in this folder (next to `autoclip_watch.py`).
6. Connect once (opens a browser):
   ```bash
   python autoclip_watch.py youtube-login
   ```
   Approve access → a `token.json` is saved. Verify it shows your channel name.

Now `paste` uploads directly to that channel. Until step 6 is done, use
`--no-publish` to build Shorts locally without uploading. Uploads default to
**private**.

> Upload-Post is still supported as a fallback (AutoClip's own publish path). If
> `client_secret.json` is absent, `paste` falls back to Upload-Post using
> `UPLOAD_POST_API_KEY` / `UPLOAD_POST_USER` from `.env`.

## Setup

```bash
# 1. Create your config from the example
copy config.example.json config.json      # Windows
# cp config.example.json config.json      # macOS / Linux

# 2. (optional) Add channels for the watcher mode (run/watch)
python autoclip_watch.py add-channel "@SomeChannel"
```

## Usage

```bash
python autoclip_watch.py paste "<url>"   # paste a link -> shorts -> publish
python autoclip_watch.py run       # one monitoring cycle
python autoclip_watch.py watch     # loop forever (uses poll_interval_minutes)
python autoclip_watch.py list      # show processed videos + status
python autoclip_watch.py reset     # clear processed-video state
```

Add `--dry-run` to any command to log what it *would* do without calling
AutoClip (no model usage, no uploads):

```bash
python autoclip_watch.py run --dry-run
```

## Configuration reference (`config.json`)

| Key | Default | Meaning |
|-----|---------|---------|
| `platform` | `youtube_shorts` | Default produce platform (`youtube_shorts`, `tiktok`, `instagram_reels`, `douyin`, `xiaohongshu`, `bilibili`, `youtube_long`). |
| `portrait_style` | `podcast` | Vertical layout: `auto`, `interview`, `podcast`. |
| `auto_publish` | `true` | Publish produced clips to your connected platforms. |
| `max_clips_per_video` | `2` | How many top-scoring clips to publish per video. |
| `poll_interval_minutes` | `30` | Loop interval for `watch`. |
| `max_videos_per_channel` | `5` | Cap new uploads processed per channel per cycle. |
| `autoclip_bin` | `autoclip` | Path/name of the AutoClip CLI. |
| `produce_timeout_sec` | `3600` | Timeout for a single produce/publish call. |
| `lookback_hours` | `0` | If >0, only process uploads published within the last N hours (0 = no time filter). |
| `max_attempts_per_video` | `3` | Retries for a failed/stuck video before giving up. |
| `extra_produce_args` | `[]` | Extra flags passed to `autoclip produce` (e.g. `["--template","editorial"]`). |
| `channels[]` | — | List of watched channels (see below). |

Per-channel overrides:

```json
{
  "id": "@SomeChannel",
  "name": "Some Channel",
  "enabled": true,
  "platforms": ["youtube_shorts", "tiktok"],
  "max_clips": 3
}
```

## How it works

1. **Monitor** — each channel's public RSS feed
   (`youtube.com/feeds/videos.xml?channel_id=...`) is polled. No API key.
   Handles/urls are resolved to a `UCxxxx` id on first use.
2. **State** — `state.json` records each seen video (`pending` → `processing`
   → `done`/`failed`), so re-runs never reprocess a video.
3. **Produce** — `autoclip produce <url> --platform ... --json` creates clips
   and a publishing kit.
4. **Publish** — the top *N* clips by score are sent with
   `autoclip publish <project_id> --clip <id> --platform <p> --wait`
   (YouTube posts default to private).

## What's left for production use

The watcher + AutoClip + Gemini pipeline is verified working end-to-end (channel
monitor → YouTube download → Gemini analysis → render). To go from "0 clips on a
test clip" to real output you still need:

- **Real source footage with speech/motion** (the synthetic test clip is a blank
  screen, so nothing scores). The Indian gaming channels you mentioned have this.
- **Subtitles, or Whisper for videos without them.** Videos with no `.srt` and no
  captions need local transcription: `pip install faster-whisper` (downloads a
  model on first use), or configure a cloud ASR provider.
- **A reliable YouTube extractor.** Install a JS runtime for yt-dlp
  (`winget install DenoLand.Deno` or `pip install yt-dlp[default]`), otherwise
  some formats are missing and downloads can fail.
- **Publishing (optional).** Set `UPLOAD_POST_API_KEY` / `UPLOAD_POST_USER` in
  `.env` and connect your YouTube channel in Upload-Post. Without it, clips are
  produced locally but not posted.

## Important: only clip content you have the rights to

AutoClip can clip and post any video it can fetch, but **re-uploading another
creator's videos to your own channel without permission** will trigger YouTube
**Content ID claims** and **reused/inauthentic content** policy (demonetization,
blocking, or strikes). Only point this at:

- your own videos, or
- creators who have **given you permission**, or
- content you add **genuine transformative value** to (your own commentary,
  analysis, editing).

This tool automates the mechanics; it does not grant rights to the source.

## Files

| File | Purpose |
|------|---------|
| `autoclip_watch.py` | CLI entry point |
| `watcher/config.py` | Config load/save + validation |
| `watcher/state.py` | Persistent processed-video state |
| `watcher/channels.py` | YouTube RSS monitoring + channel-id resolver |
| `watcher/runner.py` | AutoClip wrappers + produce/publish pipeline |
| `config.example.json` | Starter config |