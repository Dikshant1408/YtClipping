#!/usr/bin/env python3
"""YtClipping watcher - CLI entry point.

Watches selected YouTube channels, clips new uploads with AutoClip, and
optionally publishes the clips to your own YouTube channel.

Usage:
    python autoclip_watch.py paste <url>         # paste a link -> shorts -> publish
    python autoclip_watch.py run                 # one monitoring cycle
    python autoclip_watch.py watch               # loop forever (poll interval)
    python autoclip_watch.py list                # show processed videos
    python autoclip_watch.py resolve <url>       # resolve a channel to its UC id
    python autoclip_watch.py add-channel <url>   # add a channel to config.json
    python autoclip_watch.py reset               # clear processed-video state

Global options:
    --config PATH     path to config.json (default: ./config.json)
    --state PATH      path to state.json  (default: ./state.json)
    --dry-run         plan/log only; do not call autoclip produce or publish
"""

from __future__ import annotations

import argparse
import math
import os
import sys
import tempfile
import time
from pathlib import Path
from typing import Optional

from watcher import channels as ch_mod
from watcher import transcribe as tr
from watcher import youtube as yt
from watcher import __version__
from watcher.config import Config, load_config, save_config
from watcher.runner import (
    cleanup_all,
    cleanup_project,
    extract_clips,
    log,
    produce,
    publish,
    publish_platform,
    run_once,
    top_clip_ids,
)
from watcher.state import State

DEFAULT_CONFIG = "config.json"
DEFAULT_STATE = "state.json"


def load_dotenv(path: str | Path = ".env") -> None:
    """Load KEY=VALUE pairs from a gitignored .env into os.environ.

    Existing environment variables win (so a real env var overrides the file).
    This lets you keep model keys out of shell history and out of config.json.
    """
    p = Path(path)
    if not p.exists():
        return
    for raw in p.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, val = line.partition("=")
        key = key.strip()
        val = val.strip().strip('"').strip("'")
        if key and key not in os.environ:
            os.environ[key] = val


def _paths(args: argparse.Namespace) -> tuple[Path, Path]:
    return Path(args.config), Path(args.state)


def cmd_run(args: argparse.Namespace) -> int:
    cfg = load_config(args.config)
    state = State(args.state)
    run_once(cfg, state, dry_run=args.dry_run)
    return 0


SPLIT_THRESHOLD_SEC = 6 * 3600   # only split streams longer than this
SPLIT_PART_SEC = 3 * 3600        # ~3 hours per part


def _paste_split(args, cfg, duration, platforms, portrait_style, credit) -> int:
    """Process a very long stream in ~3h sections: download part -> produce -> upload -> cleanup."""
    parts = max(1, math.ceil(duration / SPLIT_PART_SEC))
    log(f"[split] {duration/3600:.2f}h stream -> {parts} parts of ~3h")
    total = 0
    for i in range(parts):
        start = i * SPLIT_PART_SEC
        end = min((i + 1) * SPLIT_PART_SEC, duration)
        out = os.path.join(tempfile.gettempdir(), f"ac_part_{i}.mp4")
        log(f"[split] part {i+1}/{parts}: {start/3600:.2f}h - {end/3600:.2f}h")
        try:
            ch_mod.download_section(args.url, start, end, out)
        except Exception as exc:  # noqa: BLE001
            log(f"[split] download part {i+1} failed: {exc}")
            continue
        srt = _cloud_srt(out, f"part {i+1}")
        try:
            data = produce(cfg, out, platforms, portrait_style, srt_path=srt)
        except Exception as exc:  # noqa: BLE001
            log(f"[split] produce part {i+1} failed: {exc}")
            _safe_remove(out)
            continue
        pid = data.get("project_id") or ""
        if data.get("status") not in ("completed", "partial"):
            log(f"[split] part {i+1}: no clips ({data.get('error')})")
            cleanup_project(pid)
            _safe_remove(out)
            continue
        videos = yt.rendered_videos(data)[: max(1, args.clips)]
        for v in videos:
            if args.no_publish:
                log(f"[split] (no-publish) rendered {Path(v['path']).name}")
                continue
            try:
                res = yt.upload_video(
                    v["path"], title=v["title"], description=v["description"],
                    privacy=args.privacy, is_short=True, credit=credit,
                )
                total += 1
                log(f"[split] uploaded {res['url']}")
            except Exception as exc:  # noqa: BLE001
                log(f"[split] upload failed: {exc}")
        cleanup_project(pid)
        _safe_remove(out)
    log(f"[split] done. uploaded {total} Shorts from {parts} part(s)")
    return 0


def _safe_remove(path: str) -> None:
    try:
        os.remove(path)
    except OSError:
        pass


def _use_cloud_whisper() -> bool:
    """Use Groq cloud Whisper when configured (disable with USE_CLOUD_WHISPER=0)."""
    if os.environ.get("USE_CLOUD_WHISPER", "").lower() in ("0", "off", "false", "no"):
        return False
    return tr.groq_available()


def _cloud_srt(source: str, log_label: str) -> Optional[str]:
    """Transcribe `source` (URL or file) with Groq Whisper; return SRT path or None."""
    if not _use_cloud_whisper():
        return None
    try:
        log(f"       {log_label}: transcribing with Groq Whisper...")
        if source.startswith("http"):
            srt = tr.transcribe_url(source)
        else:
            srt = tr.transcribe(source)
        log(f"       {log_label}: transcript ready -> {Path(srt).name}")
        return srt
    except Exception as exc:  # noqa: BLE001
        log(f"       {log_label}: cloud transcription failed ({exc}); AutoClip will transcribe")
        return None


def cmd_paste(args: argparse.Namespace) -> int:
    """Paste a video link -> produce Shorts -> publish to your channel."""
    cfg = load_config(args.config)
    platforms = [args.platform] if args.platform else [cfg.platform]
    portrait_style = args.portrait_style or cfg.portrait_style

    log(f"[paste] {args.url}")
    log(f"        platforms={platforms} style={portrait_style} publish={not args.no_publish}")

    # Long streams (>6h) are split into ~3h sections and processed part by part.
    if not args.no_publish:
        duration = ch_mod.video_duration(args.url)
        if duration and duration > SPLIT_THRESHOLD_SEC:
            credit = ch_mod.video_uploader(args.url)
            return _paste_split(args, cfg, duration, platforms, portrait_style, credit)

    srt_path = _cloud_srt(args.url, "paste")
    try:
        data = produce(cfg, args.url, platforms, portrait_style, srt_path=srt_path)
    except Exception as exc:  # noqa: BLE001
        log(f"ERROR: produce failed: {exc}")
        return 1

    project_id = data.get("project_id") or ""
    clips = extract_clips(data)
    if not clips:
        log("No clips produced (video may be too short or have no highlight-worthy segments).")
        log(f"project_id={project_id}")
        return 0

    log(f"Produced {len(clips)} clip(s) (project {project_id}):")
    for c in clips:
        log(f"  - [{c.get('id')}] score={c.get('score', c.get('score_100'))} {c.get('title', '')}")

    if args.no_publish:
        log("Skipping publish (--no-publish). Download the kit with: autoclip outputs "
            f"{project_id} --export-kits")
        return 0

    # Prefer direct YouTube upload when configured; otherwise fall back to Upload-Post.
    if yt.CLIENT_SECRET.exists():
        credit = ch_mod.video_uploader(args.url)
        if credit:
            log(f"        crediting creator: {credit}")
        videos = yt.rendered_videos(data)
        if not videos:
            log("No rendered video files found to upload (project produced no outputs).")
            return 0
        videos = videos[: max(1, args.clips)]
        ok = 0
        for v in videos:
            try:
                res = yt.upload_video(
                    v["path"],
                    title=v["title"],
                    description=v["description"],
                    privacy=args.privacy,
                    is_short=True,
                    credit=credit,
                )
                ok += 1
                log(f"Uploaded -> {res['url']} ({res['privacy']})")
            except Exception as exc:  # noqa: BLE001
                log(f"Upload failed for {Path(v['path']).name}: {exc}")
        log(f"Done. Uploaded {ok}/{len(videos)} video(s) to YouTube. project_id={project_id}")
        if ok == len(videos):
            cleanup_project(project_id)  # free disk once everything is safely uploaded
        else:
            log("Some uploads failed; keeping project data for retry.")
        return 0

    clip_ids = top_clip_ids(clips, args.clips)
    if not clip_ids:
        log("No publishable clip ids found.")
        return 0

    published = 0
    for cid in clip_ids:
        for plat in platforms:
            pub_plat = publish_platform(plat)
            try:
                publish(cfg, project_id, cid, pub_plat)
                published += 1
                log(f"Published clip {cid} -> {pub_plat}")
            except Exception as exc:  # noqa: BLE001
                log(f"Publish failed clip {cid} -> {pub_plat}: {exc}")
    log(f"Done. Published {published} clip(s). project_id={project_id}")
    return 0


def cmd_youtube_login(args: argparse.Namespace) -> int:
    """One-time browser OAuth to connect the YouTube channel."""
    log("Starting YouTube OAuth. A browser window will open; approve access.")
    try:
        creds = yt.get_credentials()
    except FileNotFoundError as exc:
        log(f"ERROR: {exc}")
        return 2
    except Exception as exc:  # noqa: BLE001
        log(f"ERROR: YouTube login failed: {exc}")
        return 1
    name = yt.channel_name()
    log(f"Connected as: {name or '(unknown channel)'}")
    log("Token saved to token.json. You can now run: python autoclip_watch.py paste <url>")
    return 0


def cmd_upload(args: argparse.Namespace) -> int:
    """Upload a local video file to YouTube directly."""
    try:
        res = yt.upload_video(
            args.file,
            title=args.title or Path(args.file).stem,
            description=args.description,
            privacy=args.privacy,
            is_short=not args.long,
        )
    except FileNotFoundError as exc:
        log(f"ERROR: {exc}")
        return 2
    except Exception as exc:  # noqa: BLE001
        log(f"ERROR: upload failed: {exc}")
        return 1
    log(f"Uploaded -> {res['url']} ({res['privacy']})")
    return 0


def cmd_watch(args: argparse.Namespace) -> int:
    cfg = load_config(args.config)
    state = State(args.state)
    interval = max(1, cfg.poll_interval_minutes) * 60
    log(f"Watching every {cfg.poll_interval_minutes} min. Ctrl+C to stop.")
    try:
        while True:
            run_once(cfg, state, dry_run=args.dry_run)
            log(f"Sleeping {cfg.poll_interval_minutes} min...")
            time.sleep(interval)
    except KeyboardInterrupt:
        log("Stopped.")
        return 0


def cmd_list(args: argparse.Namespace) -> int:
    state = State(args.state)
    vids = state.videos()
    if not vids:
        print("No videos processed yet.")
        return 0
    vids.sort(key=lambda v: v.get("first_seen", ""), reverse=True)
    for v in vids:
        status = v.get("status", "?")
        print(f"[{status:9}] {v.get('channel_name', '')} :: {v.get('title', v.get('video_id'))}")
        print(f"            {v.get('url', '')}")
        if v.get("project_id"):
            print(f"            project={v['project_id']} clips={len(v.get('clips', []))} published={len(v.get('published', []))}")
    return 0


def cmd_resolve(args: argparse.Namespace) -> int:
    cid = ch_mod.resolve_channel_id(args.target)
    print(cid)
    return 0


def cmd_add_channel(args: argparse.Namespace) -> int:
    cfg = load_config(args.config)
    cid = ch_mod.resolve_channel_id(args.target)
    name = args.name or cid
    # Avoid duplicates.
    for c in cfg.channels:
        if c.id == cid:
            print(f"Channel already in config: {name} ({cid})")
            return 0
    from watcher.config import Channel

    cfg.channels.append(Channel(id=cid, name=name, enabled=True))
    save_config(cfg, args.config)
    print(f"Added channel: {name} ({cid}) -> {args.config}")
    return 0


def cmd_reset(args: argparse.Namespace) -> int:
    state = State(args.state)
    state.reset()
    state.save()
    print(f"Cleared state: {args.state}")
    return 0


def cmd_cleanup(args: argparse.Namespace) -> int:
    n = cleanup_all()
    print(f"Removed {n} project data dir(s).")
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="autoclip_watch",
        description="Clip new uploads from selected YouTube channels via AutoClip.",
    )
    p.add_argument("--config", default=DEFAULT_CONFIG, help="path to config.json")
    p.add_argument("--state", default=DEFAULT_STATE, help="path to state.json")
    p.add_argument("--dry-run", action="store_true", help="log only; do not run autoclip")
    p.add_argument("--version", action="version", version=f"YtClipping {__version__}")

    sub = p.add_subparsers(dest="command", required=True)
    run_p = sub.add_parser("run", help="run one monitoring cycle")
    run_p.add_argument("--dry-run", action="store_true", help="log only; do not run autoclip")
    watch_p = sub.add_parser("watch", help="loop forever at the poll interval")
    watch_p.add_argument("--dry-run", action="store_true", help="log only; do not run autoclip")
    sub.add_parser("list", help="list processed videos")
    r = sub.add_parser("resolve", help="resolve a channel URL/handle to its UC id")
    r.add_argument("target")
    a = sub.add_parser("add-channel", help="resolve and add a channel to config.json")
    a.add_argument("target")
    a.add_argument("--name", default="", help="display name for the channel")
    sub.add_parser("reset", help="clear processed-video state")
    sub.add_parser("cleanup", help="delete all AutoClip project data to free disk space")
    paste_p = sub.add_parser("paste", help="paste a video link -> shorts -> publish to your channel")
    paste_p.add_argument("url", help="YouTube video URL")
    paste_p.add_argument("--platform", default="", help="produce platform (default from config)")
    paste_p.add_argument("--portrait-style", default="", help="auto / interview / podcast")
    paste_p.add_argument("--clips", type=int, default=10, help="how many top clips to publish (default 10)")
    paste_p.add_argument("--no-publish", action="store_true", help="produce only, do not upload")
    paste_p.add_argument("--privacy", default="private", choices=["private", "unlisted", "public"],
                         help="YouTube upload privacy (default private)")

    sub.add_parser("youtube-login", help="connect your YouTube channel (one-time browser OAuth)")

    up = sub.add_parser("upload", help="upload a local video file straight to YouTube")
    up.add_argument("file", help="path to the video file")
    up.add_argument("--title", default="", help="video title (default: file name)")
    up.add_argument("--description", default="", help="video description")
    up.add_argument("--privacy", default="private", choices=["private", "unlisted", "public"])
    up.add_argument("--long", action="store_true", help="upload as a normal (non-Short) video")
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    load_dotenv()
    handlers = {
        "run": cmd_run,
        "watch": cmd_watch,
        "list": cmd_list,
        "resolve": cmd_resolve,
        "add-channel": cmd_add_channel,
        "reset": cmd_reset,
        "cleanup": cmd_cleanup,
        "paste": cmd_paste,
        "youtube-login": cmd_youtube_login,
        "upload": cmd_upload,
    }
    try:
        return handlers[args.command](args)
    except FileNotFoundError as exc:
        log(f"ERROR: {exc}")
        return 2
    except RuntimeError as exc:
        log(f"ERROR: {exc}")
        return 1
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    sys.exit(main())