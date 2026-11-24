# YtClipping — Turn long videos into Shorts, auto-post to your channel

Paste a YouTube link to one of **your own** long videos, streams, podcasts, or
lectures and get ready-to-post vertical **Shorts** on your own channel — in one
command.

YtClipping is an automation layer on top of
[AutoClip](https://github.com/zhouxiaoka/autoclip) (clipping + rendering) that
adds a **direct YouTube uploader**, topic-aware metadata, creator credits,
long-stream auto-splitting, upload retry/recovery, and per-clip publishing
tracking.

```
paste URL ──► download ──► AutoClip (transcribe + analyze + render)
          ──► upload Shorts to your channel ──► delete local files
```

**Version:** 1.1.6 · **License:** MIT

## Quick start (under 2 minutes)

```bash
git clone https://github.com/Dikshant1408/YtClipping.git
cd YtClipping
pip install -r requirements.txt
copy .env.example .env          # add your model API key (Gemini/Groq/OpenAI)
python autoclip_watch.py youtube-login
python autoclip_watch.py paste "https://www.youtube.com/watch?v=YOUR_VIDEO"
```

## Why use this over vanilla AutoClip?

| Feature | Original AutoClip | YtClipping |
|---------|-------------------|------------|
| **Uploads to your channel** | Manual export or Upload-Post setup | One command, direct YouTube Data API (no third-party) |
| **Upload retry & recovery** | None | Per-clip upload tracking; failed uploads auto-retry |
| **Topic-aware metadata** | Generic title/description | Auto title, description, tags, category (configurable game/topic) |
| **Creator credit** | Not included | Auto-fetches the source channel + `Credit:` line |
| **AutoClip outro** | Forced 1.8s end-card | Removed |
| **Long videos** | Single huge download | Auto-splits long streams into sections, one at a time |
| **Disk usage** | Keeps all project data | Auto-deletes local files after upload |
| **Clips per video** | Fixed default | Up to 10, above a quality threshold |
| **Model** | Gemini (rate-limited free tier) | Gemini / Groq / OpenAI / local Ollama |
| **OAuth scopes** | N/A | `youtube.upload` + `youtube.readonly` |
| **Windows quirks** | Empty-stdout aborts, `%APPDATA%` bugs | Patched (fallback parse + plain data dir) |

## Features

- **One command:** `python autoclip_watch.py paste "<url>"` → Shorts on your channel.
- **Direct YouTube upload** via the official YouTube Data API (your own Google
  OAuth — no Upload-Post, no shared rate limit). Falls back to Upload-Post if
  `client_secret.json` is absent.
- **Upload retry & recovery** — per-clip upload status tracking
  (`pending` / `published` / `failed`). Failed uploads retry automatically on the
  next cycle; successfully published clips are never re-uploaded.
- **Accurate cloud transcription** — Groq Whisper (`whisper-large-v3`) transcribes
  multilingual audio (Hindi/English) cleanly. Falls back to AutoClip's local
  transcription if Groq isn't configured (`USE_CLOUD_WHISPER=0` disables it).
- **Hinglish / Roman subtitles** — Hindi speech is transliterated to Latin script
  (e.g. `aja main GTA 6 khela raha hun`) so English/Hinglish viewers can read it,
  instead of Devanagari. Set `TRANSCRIBE_SCRIPT=devanagari` in `.env` for Hindi
  script, or `roman` (default) for Hinglish.
- **Configurable metadata** — game/topic, tags, hashtags, category, language,
  made-for-kids are all configurable via `config.json` and `.env`. No hardcoded
  GTA 6 defaults.
- **Creator credit** — fetches the source channel via yt-dlp and adds
  `Credit: <creator>`.
- **No AutoClip outro** — branding end-card disabled.
- **Up to 10 clips per video** (configurable), only above a quality threshold.
- **Long-video auto-splitter** — videos longer than the split threshold
  (default 6h) are split into sections (default ~3h each), processed and uploaded
  part-by-part; you never download the whole file at once. Failed sections are
  retried and the job returns a non-zero exit code if incomplete.
- **Auto-cleanup** — local project data (video + renders) is deleted after a
  successful upload to save disk.
- **Flexible model** — Groq (free/fast), Gemini, OpenAI-compatible, or local Ollama.
- **Watcher mode** — optionally monitor channels and auto-clip new uploads.

## What each upload includes

For every Short, the tool sets:

| Field | Value |
|-------|-------|
| Title | `<game/topic>: <clip hook>` (game prefix only when configured) |
| Description | original + topic context + `Credit: <creator>` + hashtags |
| Tags | game/topic + auto-extracted words (≤500 chars total) |
| Category | configurable (Gaming / 20 by default) |
| Language | English (configurable) |
| License / embedding | Standard YouTube License / allowed |
| Made for kids | configurable (declare per your content) |
| Outro | none (AutoClip branding disabled) |

> **Made for kids:** This is not automatically set. Declare it correctly for
> each video in `config.json` or per-upload.

A few Studio-only fields (age restriction, paid promotion, AI-use disclosure,
comments, Shorts remixing) can't be set via the API and keep YouTube defaults.

## Long videos (auto-split)

Videos longer than `split_threshold_sec` (default: 6 hours) are split into
sections of `split_part_sec` (default: ~3 hours) automatically. Each section is
downloaded, processed, uploaded, and cleaned up independently:

```bash
python autoclip_watch.py paste "https://www.youtube.com/watch?v=LONG_STREAM"
# -> downloads 0-3h, 3-6h, ... one at a time; uploads Shorts from all parts
```

Videos at or below the threshold are processed as a single file. Failed sections
are retried; if any section fails after max retries, the command returns a
non-zero exit code.

**Typical end-to-end time:** ~1–1.75h for a 3h video, ~1.5–2.5h for a 4h video
(transcription is the bottleneck; local Whisper or Groq cloud Whisper).

## One-time: connect your YouTube channel

This project uploads straight to YouTube with the official API — no Upload-Post,
no shared rate limit. OAuth requests `youtube.upload` plus `youtube.readonly` (for channel name lookup).

1. [Google Cloud Console](https://console.cloud.google.com/) → create/select a project.
2. **APIs & Services → Library** → enable **YouTube Data API v3**.
3. **OAuth consent screen** → External → add your Google account as a **Test user**.
4. **Credentials → Create Credentials → OAuth client ID → Desktop app** → download JSON.
5. Save it as **`client_secret.json`** in this folder.
6. Connect once (opens a browser):
   ```bash
   python autoclip_watch.py youtube-login
   ```
   Approve → `token.json` is saved. It prints your channel name.

> Upload-Post is still supported as a fallback: if `client_secret.json` is absent,
> `paste` uses AutoClip's publish path with `UPLOAD_POST_API_KEY` /
> `UPLOAD_POST_USER`.

## Model config (`.env`)

Copy `.env.example` → `.env`. Gemini (recommended, 1M context, high free tier):

```ini
LLM_PROVIDER=openai
OPENAI_BASE_URL=https://googleapis.com
API_OPENAI_API_KEY=AIzaSy_YOUR_GEMINI_KEY
API_MODEL_NAME=gemini-2.5-flash
API_MAX_TOKENS=8192
USE_CLOUD_WHISPER=0
```

Other providers: Groq, OpenAI, DeepSeek, Kimi, GLM, or local Ollama/LM Studio
(see `.env.example`).

> **Windows data-dir note.** Point AutoClip at a plain local dir without spaces:
> `AUTOCLIP_DATA_DIR=D:\acdata`. The default `%APPDATA%\AutoClip` can fail to
> place the input file, causing "input not found".

## Watcher mode (optional)

Monitor channels and auto-clip new uploads:

```bash
python autoclip_watch.py add-channel "@YourChannel"
python autoclip_watch.py watch      # polls every poll_interval_minutes
python autoclip_watch.py run        # one cycle
python autoclip_watch.py list       # processed videos + status
```

## All commands

```bash
python autoclip_watch.py paste "<url>"   # paste a link -> Shorts -> upload
python autoclip_watch.py run             # one monitoring cycle
python autoclip_watch.py watch           # loop forever
python autoclip_watch.py list            # show processed videos
python autoclip_watch.py cleanup         # delete all AutoClip project data
python autoclip_watch.py reset           # clear processed-video state
python autoclip_watch.py youtube-login   # one-time YouTube OAuth
python autoclip_watch.py resolve <url>   # channel URL/handle -> UC id
python autoclip_watch.py add-channel <url>
python autoclip_watch.py upload <file>   # upload a local video file
```

`paste` options: `--clips N` (default 10), `--privacy private|unlisted|public`,
`--platform`, `--portrait-style`, `--no-publish`.

## Configuration (`config.json`)

| Key | Default | Meaning |
|-----|---------|---------|
| `platform` | `youtube_shorts` | Default produce platform. |
| `portrait_style` | `podcast` | `auto` / `interview` / `podcast`. |
| `auto_publish` | `true` | Upload produced clips. |
| `max_clips_per_video` | `10` | Max clips to upload per video. |
| `poll_interval_minutes` | `30` | Loop interval for `watch`. |
| `max_videos_per_channel` | `5` | Cap new uploads per channel per cycle. |
| `autoclip_bin` | `autoclip` | Path/name of the AutoClip CLI. |
| `produce_timeout_sec` | `14400` | Timeout per produce call (4h; covers long videos). |
| `publish_timeout_sec` | `120` | Timeout per publish call. |
| `lookback_hours` | `0` | Only process uploads from the last N hours. |
| `extra_produce_args` | `[]` | Extra flags passed to `autoclip produce`. |
| `max_attempts_per_video` | `3` | Retries before giving up (produce + publish). |
| `split_threshold_sec` | `21600` | Split streams longer than this (6h default). |
| `split_part_sec` | `10800` | Section length for split streams (3h default). |
| `game` | `""` | Game/topic for upload metadata (empty = generic mode). |

Per-channel overrides:
`{ "id": "@Handle", "name": "...", "enabled": true, "platforms": ["youtube_shorts"], "max_clips": 0 }`.

## How it works

1. **Resolve** — handle/URL → channel id (yt-dlp).
2. **Download** — yt-dlp (whole video, or sections for long streams).
3. **Produce** — `autoclip produce` transcribes, analyzes with your model, renders Shorts.
4. **Enrich** — topic-aware title/description/tags, creator credit.
5. **Upload** — YouTube Data API (direct) or Upload-Post, with per-clip retry.
6. **Cleanup** — delete local project data.

## Troubleshooting

| Problem | Likely cause | Fix |
|---------|-------------|-----|
| `input not found` | AutoClip default `%APPDATA%\AutoClip` path | Set `AUTOCLIP_DATA_DIR=D:\acdata` (no spaces) |
| Produce fails with model errors | Invalid model name or missing API key | Check `API_MODEL_NAME` and `API_OPENAI_API_KEY` in `.env` |
| Upload fails silently | OAuth token missing/expired | Run `python autoclip_watch.py youtube-login` |
| Long video never finishes | YouTube throttling download | Split threshold handles this; sections download independently |
| Clips produced but not published | Previous run marked `done` incorrectly | Fixed: statuses are now `produced`/`publishing`/`published`/`failed`; re-run retries failed uploads |

## Use it on content you own (or have permission for)

YtClipping is built to help creators **repurpose their own long videos, streams,
podcasts, and lectures** into Shorts. Only point it at content you created or
have explicit permission to reuse. Re-uploading another creator's videos without
permission can trigger YouTube **Content ID claims** and **reused/inauthentic
content** policy (demonetization, blocking, or strikes). This tool automates the
mechanics; it does not grant rights to the source.

## Files

| File | Purpose |
|------|---------|
| `autoclip_watch.py` | CLI entry point |
| `watcher/config.py` | Config load/save + validation |
| `watcher/state.py` | Persistent processed-video state |
| `watcher/channels.py` | RSS monitoring, channel-id resolver, duration/section download |
| `watcher/runner.py` | AutoClip produce/publish wrappers, split pipeline, cleanup |
| `watcher/youtube.py` | Direct YouTube upload + metadata + update |
| `config.example.json` | Starter config |
| `.env.example` | Model/provider template |
| `LICENSE` | MIT |
