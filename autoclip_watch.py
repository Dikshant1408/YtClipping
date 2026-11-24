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
import shutil
import sys
import tempfile
import time
from pathlib import Path
from typing import Any, Dict, Optional

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


def _paste_split(args, cfg, state, video_id, duration, platforms, portrait_style, credit) -> int:
    """Process a very long stream in sections: download part -> produce -> upload -> cleanup.

    Uses a unique temp directory per job (Fix #5) and tracks per-clip
    upload success/failure (Fix #4). Section progress is persisted to state
    so interrupted jobs can resume.
    """
    threshold = cfg.split_threshold_sec
    part_sec = cfg.split_part_sec
    if duration <= threshold:
        return 0  # handled by the non-split path
    parts = max(1, math.ceil(duration / part_sec))
    log(f"[split] {duration/3600:.2f}h stream -> {parts} parts of ~{part_sec/3600:.0f}h")

    # Unique temp directory per job (Fix #5: no filename collisions).
    job_tmp = Path(tempfile.mkdtemp(prefix=f"ac_split_{video_id}_"))

    # Load prior section progress from state (Fix #5: resume support).
    rec = state.get(video_id, {})
    section_progress = rec.get("section_progress", {})

    total = 0
    failed_parts = []
    for i in range(parts):
        part_key = str(i)
        # Skip already-successful sections (Fix #5: resume).
        if section_progress.get(part_key, {}).get("status") == "done":
            log(f"[split] part {i+1}/{parts}: already done, skipping")
            total += section_progress[part_key].get("uploaded", 0)
            continue

        start = i * part_sec
        end = min((i + 1) * part_sec, duration)
        out = job_tmp / f"part_{i}.mp4"
        log(f"[split] part {i+1}/{parts}: {start/3600:.2f}h - {end/3600:.2f}h")
        try:
            ch_mod.download_section(args.url, start, end, str(out))
        except Exception as exc:  # noqa: BLE001
            log(f"[split] download part {i+1} failed: {exc}")
            failed_parts.append(i + 1)
            section_progress[part_key] = {"status": "failed", "error": str(exc), "uploaded": 0}
            continue
        srt = _cloud_srt(str(out), f"part {i+1}")
        try:
            data = produce(cfg, str(out), platforms, portrait_style, srt_path=srt)
        except Exception as exc:  # noqa: BLE001
            _safe_remove(srt)
            _safe_remove(str(out))
            log(f"[split] produce part {i+1} failed: {exc}")
            failed_parts.append(i + 1)
            section_progress[part_key] = {"status": "failed", "error": str(exc), "uploaded": 0}
            continue
        _safe_remove(srt)
        pid = data.get("project_id") or ""
        if data.get("status") not in ("completed", "partial"):
            log(f"[split] part {i+1}: no clips ({data.get('error')})")
            cleanup_project(pid)
            _safe_remove(str(out))
            failed_parts.append(i + 1)
            section_progress[part_key] = {"status": "failed", "error": data.get("error", "no clips"), "uploaded": 0}
            continue
        videos = yt.rendered_videos(data)[: max(1, args.clips)]
        part_uploaded = 0
        part_failed = False
        for v in videos:
            if args.no_publish:
                log(f"[split] (no-publish) rendered {Path(v['path']).name}")
                part_uploaded += 1
                continue
            try:
                res = yt.upload_video(
                    v["path"],
                    title=v["title"],
                    description=v["description"],
                    privacy=args.privacy,
                    is_short=True,
                    credit=credit,
                    game=cfg.game,
                    category_id=cfg.category_id,
                    language=cfg.language,
                    made_for_kids=cfg.made_for_kids,
                )
                total += 1
                part_uploaded += 1
                log(f"[split] uploaded {res['url']}")
            except Exception as exc:  # noqa: BLE001
                part_failed = True
                log(f"[split] upload failed for {Path(v['path']).name}: {exc}")
        cleanup_project(pid)
        _safe_remove(str(out))

        # Persist section progress (Fix #5).
        section_progress[part_key] = {
            "status": "failed" if part_failed and part_uploaded == 0 else "done",
            "uploaded": part_uploaded,
            "error": None if not part_failed else "some uploads failed",
        }
        state.put(video_id, {
            "status": "processing", "project_id": "", "platforms": platforms,
            "section_progress": section_progress,
        })
        state.save()

        # Track failures per clip, not just per section (Fix #4).
        if part_failed:
            failed_parts.append(i + 1)

    # Clean up job temp directory.
    shutil.rmtree(job_tmp, ignore_errors=True)

    # Determine overall split status.
    all_done = all(section_progress.get(str(i), {}).get("status") == "done" for i in range(parts))
    if failed_parts and not all_done:
        log(f"[split] WARNING: {len(failed_parts)} part(s) failed: {failed_parts}")
        return 1
    log(f"[split] done. uploaded {total} Shorts from {parts} part(s)")
    return 0


def _safe_remove(path: str | None) -> None:
    if path is None:
        return
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
    state = State(args.state)
    platforms = [args.platform] if args.platform else [cfg.platform]
    portrait_style = args.portrait_style or cfg.portrait_style

    # Extract video id from URL for state tracking.
    import re as _re
    _m = _re.search(r"[?&]v=([^&]+)", args.url)
    video_id = _m.group(1) if _m else args.url

    log(f"[paste] {args.url}")
    log(f"        platforms={platforms} style={portrait_style} publish={not args.no_publish}")

    # Long streams are split into sections and processed part by part.
    if not args.no_publish:
        duration = ch_mod.video_duration(args.url)
        if duration and duration > cfg.split_threshold_sec:
            credit = ch_mod.video_uploader(args.url)
            return _paste_split(args, cfg, state, video_id, duration, platforms, portrait_style, credit)

    srt_path = _cloud_srt(args.url, "paste")
    try:
        data = produce(cfg, args.url, platforms, portrait_style, srt_path=srt_path)
    except Exception as exc:  # noqa: BLE001
        _safe_remove(srt_path)
        log(f"ERROR: produce failed: {exc}")
        return 1
    _safe_remove(srt_path)

    project_id = data.get("project_id") or ""
    clips = extract_clips(data)
    if not clips:
        log("No clips produced (video may be too short or have no highlight-worthy segments).")
        log(f"project_id={project_id}")
        state.put(video_id, {
            "status": "failed", "error": "no clips produced",
            "project_id": project_id, "platforms": platforms,
        })
        state.save()
        return 0

    log(f"Produced {len(clips)} clip(s) (project {project_id}):")
    for c in clips:
        log(f"  - [{c.get('id')}] score={c.get('score', c.get('score_100'))} {c.get('title', '')}")

    if args.no_publish:
        log("Skipping publish (--no-publish). Download the kit with: autoclip outputs "
            f"{project_id} --export-kits")
        state.put(video_id, {
            "status": "produced", "project_id": project_id, "platforms": platforms,
            "clips": clips,
            "selected_clip_ids": [str(c.get("id") or c.get("clip_id")) for c in clips],
        })
        state.save()
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

        # Persistent per-clip tracking for manual paste (Fix #3).
        clip_ids = [str(v.get("id", i)) for i, v in enumerate(videos)]
        clips_upload: Dict[str, Any] = {}
        for cid in clip_ids:
            clips_upload[cid] = {
                "status": "pending",
                "platforms": {
                    publish_platform(p): {"status": "pending", "youtube_id": None,
                                          "youtube_url": None, "error": None, "attempts": 0}
                    for p in platforms
                },
            }
        state.put(video_id, {
            "status": "publishing", "project_id": project_id, "platforms": platforms,
            "clips": clips, "selected_clip_ids": clip_ids, "clips_upload": clips_upload,
            "published": [],
        })
        state.save()

        ok = 0
        failed = []
        for idx, v in enumerate(videos):
            cid = clip_ids[idx]
            pub_plat = publish_platform(platforms[0])
            plat_track = clips_upload[cid]["platforms"].get(pub_plat, {"status": "pending", "attempts": 0})
            if plat_track["status"] == "published":
                ok += 1
                continue
            if plat_track["attempts"] >= max(1, cfg.max_attempts_per_video):
                log(f"       clip {cid} -> {pub_plat}: max attempts reached, skipping")
                failed.append(Path(v['path']).name)
                continue
            plat_track["attempts"] += 1
            try:
                res = yt.upload_video(
                    v["path"],
                    title=v["title"],
                    description=v["description"],
                    privacy=args.privacy,
                    is_short=True,
                    credit=credit,
                    game=cfg.game,
                    category_id=cfg.category_id,
                    language=cfg.language,
                    made_for_kids=cfg.made_for_kids,
                )
                plat_track["status"] = "published"
                plat_track["youtube_id"] = res.get("id")
                plat_track["youtube_url"] = res.get("url")
                plat_track["error"] = None
                ok += 1
                log(f"Uploaded -> {res['url']} ({res['privacy']})")
            except Exception as exc:  # noqa: BLE001
                plat_track["status"] = "failed"
                plat_track["error"] = str(exc)
                failed.append(Path(v['path']).name)
                log(f"Upload failed for {Path(v['path']).name}: {exc}")

            # Persist after EVERY upload attempt (Fix #2).
            clips_upload[cid]["platforms"][pub_plat] = plat_track
            all_statuses = [c2["platforms"].get(pub_plat, {}).get("status", "pending")
                            for c2 in clips_upload.values()]
            final_status = "published" if all(s == "published" for s in all_statuses) else \
                ("partially_published" if any(s == "published" for s in all_statuses) else "failed")
            published_list = [c2["platforms"].get(pub_plat, {}) for c2 in clips_upload.values()
                              if c2["platforms"].get(pub_plat, {}).get("status") == "published"]
            state.put(video_id, {
                "status": final_status, "project_id": project_id, "platforms": platforms,
                "clips": clips, "selected_clip_ids": clip_ids, "clips_upload": clips_upload,
                "published": published_list,
            })
            state.save()

        log(f"Done. Uploaded {ok}/{len(videos)} video(s) to YouTube. project_id={project_id}")
        if failed:
            log(f"WARNING: {len(failed)} upload(s) failed: {failed}")
            return 1
        cleanup_project(project_id)  # free disk once everything is safely uploaded
        return 0

    clip_ids = top_clip_ids(clips, args.clips)
    if not clip_ids:
        log("No publishable clip ids found.")
        return 0

    published = 0
    failed_uploads = []
    for cid in clip_ids:
        for plat in platforms:
            pub_plat = publish_platform(plat)
            try:
                publish(cfg, project_id, cid, pub_plat)
                published += 1
                log(f"Published clip {cid} -> {pub_plat}")
            except Exception as exc:  # noqa: BLE001
                failed_uploads.append((cid, pub_plat))
                log(f"Publish failed clip {cid} -> {pub_plat}: {exc}")
    log(f"Done. Published {published} clip(s). project_id={project_id}")
    if failed_uploads:
        log(f"WARNING: {len(failed_uploads)} publish(s) failed: {failed_uploads}")
        return 1
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
