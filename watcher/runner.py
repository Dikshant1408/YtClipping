"""Pipeline orchestration: monitor -> produce -> publish.

Wraps the `autoclip` command-line tool. The watcher itself has no third-party
dependencies; AutoClip (and optionally yt-dlp) must be installed separately.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Tuple

from .config import Channel, Config
from .state import State


def log(msg: str) -> None:
    print(msg, file=sys.stderr, flush=True)


def autoclip_path(cfg: Config) -> Optional[str]:
    """Return the resolved autoclip executable path, or None if missing."""
    return shutil.which(cfg.autoclip_bin)


def ensure_autoclip(cfg: Config) -> str:
    exe = autoclip_path(cfg)
    if not exe:
        raise RuntimeError(
            f"'{cfg.autoclip_bin}' not found on PATH. Install AutoClip first:\n"
            "  uvx autoclip-mcp          (or)\n"
            "  pip install autoclip-mcp\n"
            "Then verify with: autoclip --version"
        )
    return exe


def _child_env() -> Dict[str, str]:
    """Environment for the autoclip child process.

    Force UTF-8 so Chinese titles / JSON never crash on Windows consoles
    (default cp1252), and so the child writes UTF-8 to stdout regardless of the
    console code page. Inherits the parent env, so model keys / provider vars
    set by the user (or loaded from .env) propagate.
    """
    env = dict(os.environ)
    env.setdefault("PYTHONUTF8", "1")
    env.setdefault("PYTHONIOENCODING", "utf-8")
    return env


def _run(cmd: List[str], timeout: int) -> subprocess.CompletedProcess:
    return subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=timeout,
        env=_child_env(),
    )


def _data_dir() -> Optional[str]:
    d = os.environ.get("AUTOCLIP_DATA_DIR")
    if d:
        return d
    appdata = os.environ.get("APPDATA")
    return os.path.join(appdata, "AutoClip") if appdata else None


def cleanup_project(project_id: str) -> bool:
    """Delete a project's data dir (raw video + renders + metadata) to free space."""
    import shutil

    base = _data_dir()
    if not base or not project_id:
        return False
    p = os.path.join(base, "projects", project_id)
    if os.path.isdir(p):
        shutil.rmtree(p, ignore_errors=True)
        log(f"  cleaned up project data: {project_id}")
        return True
    return False


def cleanup_all(keep: Optional[set] = None) -> int:
    """Delete all project data dirs except those in `keep`. Returns count removed."""
    import glob
    import shutil

    base = _data_dir()
    if not base:
        return 0
    projects_dir = os.path.join(base, "projects")
    if not os.path.isdir(projects_dir):
        return 0
    n = 0
    for d in glob.glob(os.path.join(projects_dir, "*")):
        if os.path.isdir(d) and (keep is None or os.path.basename(d) not in keep):
            shutil.rmtree(d, ignore_errors=True)
            n += 1
    return n


def _newest_project_mtime() -> float:
    import glob

    base = _data_dir()
    if not base:
        return 0.0
    projects_dir = os.path.join(base, "projects")
    if not os.path.isdir(projects_dir):
        return 0.0
    dirs = [d for d in glob.glob(os.path.join(projects_dir, "*")) if os.path.isdir(d)]
    return max((os.path.getmtime(d) for d in dirs), default=0.0)


def _newest_project_result(before_mtime: float = 0.0) -> Optional[Dict[str, Any]]:
    """Fallback: read the newest AutoClip project's studio.json.

    Used when the produce CLI exits without parseable JSON on stdout/stderr
    (observed on Windows when stdout is a pipe). Only returns a project created
    after `before_mtime`, so a stale project is never mistaken for this run.
    """
    import glob

    base = _data_dir()
    if not base:
        return None
    projects_dir = os.path.join(base, "projects")
    if not os.path.isdir(projects_dir):
        return None
    dirs = [d for d in glob.glob(os.path.join(projects_dir, "*")) if os.path.isdir(d)]
    if not dirs:
        return None
    newest = max(dirs, key=os.path.getmtime)
    if os.path.getmtime(newest) <= before_mtime:
        return None
    sj = os.path.join(newest, "metadata", "studio.json")
    if not os.path.isfile(sj):
        return None
    try:
        with open(sj, encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, json.JSONDecodeError):
        return None

    jobs = {j.get("job_id"): j for j in data.get("jobs", [])}
    outputs: List[Dict[str, Any]] = []
    for v in data.get("output_variants", []):
        vp = v.get("video_path")
        if (not vp or not os.path.isfile(vp)) and v.get("render_job_id") in jobs:
            cand = os.path.join(newest, "output", "studio",
                               jobs[v["render_job_id"]]["job_id"] + ".mp4")
            if os.path.isfile(cand):
                vp = cand
        outputs.append({
            "id": v.get("id"),
            "status": v.get("status"),
            "video_path": vp if vp and os.path.isfile(vp) else None,
            "post": v.get("post") if isinstance(v.get("post"), dict) else {},
        })

    analysis = data.get("analysis", {}) if isinstance(data.get("analysis"), dict) else {}
    status = analysis.get("status") or (data.get("generation", {}) or {}).get("status")
    return {
        "project_id": os.path.basename(newest),
        "project_dir": newest,
        "status": status,
        "outputs": outputs,
        "ok": status in ("completed", "partial"),
        "analysis": {"phase": analysis.get("phase"), "error": analysis.get("error")},
    }


# --------------------------------------------------------------------------- #
# AutoClip wrappers
# --------------------------------------------------------------------------- #
def produce(
    cfg: Config,
    url: str,
    platforms: List[str],
    portrait_style: str,
    timeout: Optional[int] = None,
    srt_path: Optional[str] = None,
) -> Dict[str, Any]:
    """Run `autoclip produce <url> --platform ... --json` and return parsed JSON.

    If srt_path is given, pass --srt to reuse an existing transcript and skip
    re-transcription (useful when retrying with a different model).
    """
    exe = ensure_autoclip(cfg)
    cmd = [exe, "produce", url]
    for p in platforms:
        cmd += ["--platform", p]
    if portrait_style:
        cmd += ["--portrait-style", portrait_style]
    if srt_path:
        cmd += ["--srt", srt_path]
    cmd += list(cfg.extra_produce_args)
    cmd += ["--json"]

    log(f"  $ {' '.join(cmd)}")
    before_mtime = _newest_project_mtime()
    proc = _run(cmd, timeout or cfg.produce_timeout_sec)
    if proc.returncode not in (0, 1):  # 0 ok, 1 partial (some versions failed)
        raise RuntimeError(
            f"autoclip produce failed (exit {proc.returncode}).\n"
            f"stderr:\n{proc.stderr.strip()[:2000]}"
        )
    data = _parse_json(proc.stdout)
    if not isinstance(data, dict):
        data = _parse_json(proc.stderr)  # some builds print JSON to stderr
    if not isinstance(data, dict):
        # Windows quirk: produce succeeds but prints nothing to a piped stdout.
        data = _newest_project_result(before_mtime)
    if not isinstance(data, dict):
        raise RuntimeError(
            "Could not parse autoclip produce JSON output.\n"
            f"stdout (first 2000 chars):\n{proc.stdout[:2000]}\n"
            f"stderr (first 2000 chars):\n{proc.stderr[:2000]}"
        )
    return data


def publish(cfg: Config, project_id: str, clip_id: str, platform: str) -> Dict[str, Any]:
    """Run `autoclip publish <project_id> --clip <id> --platform <p> --wait`."""
    exe = ensure_autoclip(cfg)
    cmd = [
        exe,
        "publish",
        project_id,
        "--clip",
        str(clip_id),
        "--platform",
        platform,
        "--wait",
    ]
    log(f"  $ {' '.join(cmd)}")
    proc = _run(cmd, cfg.produce_timeout_sec)
    if proc.returncode != 0:
        raise RuntimeError(
            f"autoclip publish failed (exit {proc.returncode}).\n"
            f"stderr:\n{proc.stderr.strip()[:2000]}"
        )
    return _parse_json(proc.stdout) or {"ok": True, "stdout": proc.stdout.strip()}


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #
def _parse_json(text: str) -> Optional[Any]:
    text = (text or "").strip()
    if not text:
        return None
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass
    # Fall back to the outermost JSON object in the text.
    start = text.find("{")
    end = text.rfind("}")
    if start != -1 and end != -1 and end > start:
        try:
            return json.loads(text[start : end + 1])
        except json.JSONDecodeError:
            return None
    return None


def _clip_score(clip: Dict[str, Any]) -> float:
    if "score" in clip and isinstance(clip["score"], (int, float)):
        return float(clip["score"])
    if "score_100" in clip and isinstance(clip["score_100"], (int, float)):
        return float(clip["score_100"]) / 100.0
    return 0.0


def extract_clips(data: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Pull the clip list from a produce result, tolerating schema variations."""
    clips = data.get("clips")
    if isinstance(clips, list):
        return [c for c in clips if isinstance(c, dict)]
    for key in ("outputs", "versions", "results", "items"):
        val = data.get(key)
        if isinstance(val, list):
            found = [c for c in val if isinstance(c, dict) and ("id" in c or "clip_id" in c)]
            if found:
                return found
        elif isinstance(val, dict):
            found = [dict(v, id=v.get("id", k)) for k, v in val.items() if isinstance(v, dict)]
            if found:
                return found
    return []


def top_clip_ids(clips: List[Dict[str, Any]], n: int) -> List[str]:
    """Return up to n clip ids with the highest score."""
    with_id = [c for c in clips if (c.get("id") or c.get("clip_id"))]
    with_id.sort(key=_clip_score, reverse=True)
    ids: List[str] = []
    for c in with_id[: max(0, n)]:
        ids.append(str(c.get("id") or c.get("clip_id")))
    return ids


def publish_platform(produce_platform: str) -> str:
    """Map a produce platform id to the publish platform id."""
    mapping = {
        "youtube_shorts": "youtube",
        "shorts": "youtube",
        "youtube_long": "youtube",
        "tiktok": "tiktok",
        "instagram_reels": "instagram",
        "reels": "instagram",
        "instagram": "instagram",
        "douyin": "douyin",
        "xiaohongshu": "xiaohongshu",
        "bilibili": "bilibili",
    }
    return mapping.get(produce_platform, produce_platform)


def _parse_iso(ts: str) -> Optional[datetime]:
    if not ts:
        return None
    try:
        s = ts.replace("Z", "+00:00")
        return datetime.fromisoformat(s)
    except ValueError:
        return None


# --------------------------------------------------------------------------- #
# Pipeline
# --------------------------------------------------------------------------- #
def _clip_upload_entry(platforms: List[str]) -> Dict[str, Any]:
    """Create a fresh per-clip upload tracking entry."""
    return {
        "status": "pending",
        "platforms": {
            p: {"status": "pending", "youtube_id": None, "youtube_url": None, "error": None, "attempts": 0}
            for p in platforms
        },
    }


def _clip_final_status(platforms_status: Dict[str, Any]) -> str:
    """Determine clip-level status from per-platform results."""
    statuses = [p.get("status", "pending") for p in platforms_status.values()]
    if not statuses:
        return "failed"
    if all(s == "published" for s in statuses):
        return "published"
    if any(s in ("published", "partially_published") for s in statuses):
        return "partially_published"
    return "failed"


def _video_final_status(clips_upload: Dict[str, Any]) -> str:
    """Determine overall video status from per-clip results."""
    statuses = [c.get("status", "pending") for c in clips_upload.values()]
    if not statuses:
        return "failed"
    if all(s == "published" for s in statuses):
        return "published"
    if any(s in ("published", "partially_published") for s in statuses):
        return "partially_published"
    return "failed"


def process_video(
    cfg: Config,
    state: State,
    channel: Channel,
    entry: Dict[str, str],
    dry_run: bool = False,
) -> Dict[str, Any]:
    """Produce (and optionally publish) clips for a single video entry.

    Status lifecycle:
      unseen        -> produce -> produced
      produced      -> publish -> publishing / partially_published / published / failed
      publishing    -> retry failed uploads -> same set of outcomes
      partially_published -> retry remaining -> same set of outcomes
      published     -> skip (done)
      failed        -> retry up to max_attempts_per_video
    """
    video_id = entry["video_id"]
    url = entry.get("url") or f"https://www.youtube.com/watch?v={video_id}"
    platforms = cfg.channel_platforms(channel)
    max_clips = cfg.channel_max_clips(channel)

    log(f"[clip] {channel.name or channel.id} :: {entry.get('title', video_id)}")
    log(f"       {url}")

    rec = state.get(video_id, {})
    status = rec.get("status")

    # Already fully published -> skip
    if status == "published":
        log("       already published, skipping")
        return rec

    # Produce clips if not already produced (or resuming mid-publish)
    if status not in ("produced", "publishing", "partially_published"):
        attempts = int(rec.get("attempts", 0))
        state.update(
            video_id,
            channel_id=channel.id,
            channel_name=channel.name,
            title=entry.get("title", ""),
            url=url,
            published=entry.get("published", ""),
            status="processing",
            attempts=attempts + 1,
        )
        state.save()

        if dry_run:
            log("       [dry-run] would run autoclip produce + publish, skipping")
            state.update(video_id, status="pending", dry_run=True)
            state.save()
            return state.get(video_id, {})

        try:
            data = produce(cfg, url, platforms, cfg.portrait_style)
        except Exception as exc:  # noqa: BLE001
            log(f"       ERROR: {exc}")
            state.update(video_id, status="failed", error=str(exc), attempts=attempts + 1)
            state.save()
            return state.get(video_id, {})

        project_id = data.get("project_id") or ""
        clips = extract_clips(data)
        clip_ids = top_clip_ids(clips, max_clips) if max_clips > 0 else []

        clips_upload: Dict[str, Any] = {}
        for cid in clip_ids:
            clips_upload[str(cid)] = _clip_upload_entry(platforms)

        state.put(
            video_id,
            {
                "status": "produced",
                "attempts": attempts + 1,
                "project_id": project_id,
                "platforms": platforms,
                "clips": clips,
                "selected_clip_ids": clip_ids,
                "clips_upload": clips_upload,
                "published": [],
                "raw_summary": {"ok": data.get("ok"), "counts": data.get("counts")},
            },
        )
        state.save()
        log(f"       produced: project={project_id} clips={len(clip_ids)}")
    else:
        # Resume: load existing record
        project_id = rec.get("project_id") or ""
        clips = rec.get("clips", [])
        clip_ids = rec.get("selected_clip_ids", [])
        clips_upload = rec.get("clips_upload", {})
        log(f"       resuming: project={project_id} clips={len(clip_ids)} status={status}")

    if not clip_ids:
        log("       no clip ids found; nothing to publish")
        state.update(video_id, status="failed", error="no clip ids")
        state.save()
        return state.get(video_id, {})

    # Publish clips (or retry failed ones)
    if cfg.auto_publish and project_id:
        state.update(video_id, status="publishing")
        state.save()

        published_count = 0
        total_clips = len(clip_ids)

        for cid in clip_ids:
            cid_str = str(cid)
            clip_track = clips_upload.get(cid_str, _clip_upload_entry(platforms))
            clip_platforms = clip_track.get("platforms", {})

            for plat in platforms:
                pub_plat = publish_platform(plat)
                plat_track = clip_platforms.get(
                    pub_plat, {"status": "pending", "youtube_id": None, "youtube_url": None, "error": None, "attempts": 0}
                )

                # Skip if already published on this platform
                if plat_track["status"] == "published":
                    published_count += 1
                    continue

                # Check attempt limit
                if plat_track["attempts"] >= max(1, cfg.max_attempts_per_video):
                    log(f"       clip {cid} -> {pub_plat}: max attempts reached, skipping")
                    continue

                plat_track["attempts"] += 1
                try:
                    res = publish(cfg, project_id, cid, pub_plat)
                    plat_track["status"] = "published"
                    plat_track["youtube_id"] = res.get("id")
                    plat_track["youtube_url"] = res.get("url")
                    plat_track["error"] = None
                    published_count += 1
                    log(f"       published clip {cid} -> {pub_plat}: {res.get('url')}")
                except Exception as exc:  # noqa: BLE001
                    plat_track["status"] = "failed"
                    plat_track["error"] = str(exc)
                    log(f"       publish failed clip {cid} -> {pub_plat}: {exc}")

                clip_platforms[pub_plat] = plat_track

            clip_track["platforms"] = clip_platforms
            clip_track["status"] = _clip_final_status(clip_platforms)
            clips_upload[cid_str] = clip_track

        # Determine overall video status
        final_status = _video_final_status(clips_upload)

        published_list = []
        for c in clips_upload.values():
            for p in c.get("platforms", {}).values():
                if p["status"] == "published":
                    published_list.append(p)

        state.put(
            video_id,
            {
                "status": final_status,
                "project_id": project_id,
                "platforms": platforms,
                "clips": clips,
                "selected_clip_ids": clip_ids,
                "clips_upload": clips_upload,
                "published": published_list,
                "attempts": int(rec.get("attempts", 0)) + 1,
            },
        )
        state.save()
        log(f"       {final_status}: project={project_id} published={published_count}/{total_clips}")
    else:
        log(f"       auto_publish off; produced project {project_id} with {len(clip_ids)} clip(s)")
        state.update(video_id, status="produced")
        state.save()

    return state.get(video_id, {})


def _should_process(state: State, video_id: str, cfg: Config) -> bool:
    """Decide whether a video still needs processing.

    - unseen                      -> process
    - produced                    -> process (needs publishing)
    - publishing                  -> process (retry failed uploads)
    - partially_published         -> process (retry remaining uploads)
    - pending (e.g. from dry-run) -> process
    - published                   -> skip (fully done)
    - failed                      -> retry up to max_attempts_per_video
    - processing (stale/crashed)  -> retry up to max_attempts_per_video
    """
    rec = state.get(video_id)
    if rec is None:
        return True
    status = rec.get("status")
    if status == "published":
        return False
    attempts = int(rec.get("attempts", 0))
    if status in ("failed", "processing", "publishing", "partially_published") and attempts >= max(1, cfg.max_attempts_per_video):
        return False
    return True


def run_once(cfg: Config, state: State, dry_run: bool = False) -> Dict[str, Any]:
    """One monitoring cycle across all enabled channels."""
    from .channels import fetch_channel_entries, resolve_channel_id

    if not cfg.channels:
        log("No channels configured. Add some to config.json (see config.example.json).")
        return {"processed": 0, "skipped": 0}

    log(f"[{_now()}] polling {len(cfg.channels)} channel(s)")
    processed = 0
    skipped = 0

    for ch in cfg.channels:
        if not ch.enabled:
            log(f"- {ch.name or ch.id}: disabled, skipping")
            continue

        # Resolve a handle/url to a channel id once, cache it in state.
        try:
            channel_id = (
                ch.id if ch.id.startswith("UC") else resolve_channel_id(ch.id)
            )
        except Exception as exc:  # noqa: BLE001
            log(f"- {ch.name or ch.id}: resolve failed: {exc}")
            continue

        try:
            entries = fetch_channel_entries(channel_id)
        except Exception as exc:  # noqa: BLE001
            log(f"- {ch.name or ch.id}: feed fetch failed: {exc}")
            continue

        new_entries = [e for e in entries if _should_process(state, e["video_id"], cfg)]
        if cfg.lookback_hours > 0:
            cutoff = datetime.now(timezone.utc) - timedelta(hours=cfg.lookback_hours)
            new_entries = [
                e
                for e in new_entries
                if (_parse_iso(e.get("published", "")) or cutoff) >= cutoff
            ]
        if cfg.max_videos_per_channel > 0:
            new_entries = new_entries[: cfg.max_videos_per_channel]

        if not new_entries:
            log(f"- {ch.name or ch.id}: no new uploads")
            skipped += 0
            continue

        log(f"- {ch.name or ch.id}: {len(new_entries)} new upload(s)")
        for entry in new_entries:
            process_video(cfg, state, ch, entry, dry_run=dry_run)
            processed += 1

    state.save()
    log(f"[{_now()}] cycle complete: processed={processed}")
    return {"processed": processed, "skipped": skipped}


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%SZ")
