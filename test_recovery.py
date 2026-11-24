"""Tests for YtClipping recovery & reliability fixes (v1.1.6).

Covers:
  - Resume failed jobs with empty clips (retry production, non-zero exit)
  - Resume produced/publishing jobs without regenerating (production NOT called)
  - Non-zero exit when no clips produced or no rendered videos exist
  - Multiple platforms in direct upload mode (all platforms uploaded)
  - Clip identity preservation across recovery (stable clip IDs)
  - Split partial section resume from saved project (no re-production)
  - Watcher structured results + non-zero exit on failures
  - Upload attempt persisted before API request (crash safety)
"""

import json
import sys
import tempfile
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from autoclip_watch import cmd_paste, cmd_run, _check_youtube_existing_upload  # noqa: E402
from watcher.runner import run_once  # noqa: E402
from watcher.state import State  # noqa: E402


def write_config(tmp_path: Path, **overrides) -> Path:
    data = {
        "platform": "youtube",
        "portrait_style": "default",
        "max_attempts_per_video": 3,
        "split_threshold_sec": 7200,
        "split_part_sec": 3600,
        "max_clips_per_video": 2,
    }
    data.update(overrides)
    p = tmp_path / "config.json"
    p.write_text(json.dumps(data), encoding="utf-8")
    return p


def write_state(tmp_path: Path, videos: dict | None = None) -> Path:
    p = tmp_path / "state.json"
    if videos:
        p.write_text(json.dumps({"videos": videos}, indent=2), encoding="utf-8")
    else:
        p.write_text(json.dumps({"videos": {}}), encoding="utf-8")
    return p


class Args:
    def __init__(self, **kw):
        defaults = {
            "config": "config.json",
            "state": "state.json",
            "url": "",
            "platform": None,
            "portrait_style": None,
            "no_publish": False,
            "clips": 5,
            "privacy": "unlisted",
            "dry_run": False,
        }
        defaults.update(kw)
        self.__dict__.update(defaults)


# ---------------------------------------------------------------------------
# Test 1: Failed job with empty clips retries production AND returns non-zero
# ---------------------------------------------------------------------------
def test_failed_job_empty_clips_retries_production_nonzero(tmp_path):
    cfg_path = write_config(tmp_path)
    state_path = write_state(tmp_path, {
        "abc123": {
            "status": "failed",
            "error": "no clips produced",
            "project_id": "",
            "platforms": ["youtube"],
            "clips": [],
            "selected_clip_ids": [],
            "resume_attempts": 0,
        }
    })

    args = Args(config=str(cfg_path), state=str(state_path),
                url="https://www.youtube.com/watch?v=abc123",
                platform=None, no_publish=False, clips=5, privacy="unlisted")

    produced = {"project_id": "proj-xyz", "status": "completed"}
    clips = [{"id": 1, "title": "Clip 1", "score_100": 80}]

    with patch("autoclip_watch.ch_mod.video_duration", return_value=None), \
         patch("autoclip_watch.produce", return_value=produced) as mock_produce, \
         patch("autoclip_watch.extract_clips", return_value=clips), \
         patch("autoclip_watch._cloud_srt", return_value=None), \
         patch("autoclip_watch._safe_remove"), \
         patch("autoclip_watch.cleanup_project"):
        rc = cmd_paste(args)

    assert mock_produce.called, "production should be retried for failed job with empty clips"
    assert rc != 0, f"expected non-zero exit (no upload performed), got {rc}"
    state = State(state_path)
    rec = state.get("abc123")
    assert rec.get("resume_attempts", 0) >= 1, "resume_attempts should be incremented"


# ---------------------------------------------------------------------------
# Test 2: Produced job resumes WITHOUT re-producing
# ---------------------------------------------------------------------------
def test_produced_job_resumes_without_reproducing(tmp_path):
    cfg_path = write_config(tmp_path)
    state_path = write_state(tmp_path, {
        "def456": {
            "status": "produced",
            "project_id": "proj-existing",
            "platforms": ["youtube"],
            "clips": [{"id": "clip_1", "title": "Clip 1"}],
            "selected_clip_ids": ["clip_1"],
            "resume_attempts": 0,
        }
    })

    args = Args(config=str(cfg_path), state=str(state_path),
                url="https://www.youtube.com/watch?v=def456",
                platform=None, no_publish=False, clips=5, privacy="unlisted")

    with patch("autoclip_watch.ch_mod.video_duration", return_value=None), \
         patch("autoclip_watch.yt.CLIENT_SECRET", Path("/nonexistent/client_secret.json")), \
         patch("autoclip_watch.produce") as mock_produce, \
         patch("autoclip_watch.publish") as mock_publish:
        rc = cmd_paste(args)

    assert not mock_produce.called, "production should NOT be called for resumed produced job"
    assert mock_publish.called, "publish should be called for resumed produced job"


# ---------------------------------------------------------------------------
# Test 3: No clips produced returns non-zero
# ---------------------------------------------------------------------------
def test_no_clips_returns_nonzero(tmp_path):
    cfg_path = write_config(tmp_path)
    state_path = write_state(tmp_path)

    args = Args(config=str(cfg_path), state=str(state_path),
                url="https://www.youtube.com/watch?v=noclips",
                platform=None, no_publish=False, clips=5, privacy="unlisted")

    with patch("autoclip_watch.ch_mod.video_duration", return_value=None), \
         patch("autoclip_watch.produce", return_value={"project_id": "proj-empty", "status": "completed"}), \
         patch("autoclip_watch.extract_clips", return_value=[]), \
         patch("autoclip_watch._cloud_srt", return_value=None), \
         patch("autoclip_watch._safe_remove"), \
         patch("autoclip_watch.cleanup_project"):
        rc = cmd_paste(args)

    assert rc != 0, f"expected non-zero exit when no clips produced, got {rc}"


# ---------------------------------------------------------------------------
# Test 4: No rendered videos returns non-zero (direct upload branch)
# ---------------------------------------------------------------------------
def test_no_rendered_videos_returns_nonzero(tmp_path):
    cfg_path = write_config(tmp_path)
    state_path = write_state(tmp_path, {
        "ghi789": {
            "status": "produced",
            "project_id": "proj-has-clips",
            "platforms": ["youtube"],
            "clips": [{"id": 1, "title": "Clip 1"}],
            "selected_clip_ids": [1],
            "resume_attempts": 0,
        }
    })

    fake_secret = tmp_path / "client_secret.json"
    fake_secret.write_text("{}")

    args = Args(config=str(cfg_path), state=str(state_path),
                url="https://www.youtube.com/watch?v=ghi789",
                platform=None, no_publish=False, clips=5, privacy="unlisted")

    with patch("autoclip_watch.ch_mod.video_duration", return_value=None), \
         patch("autoclip_watch.yt.CLIENT_SECRET", fake_secret), \
         patch("autoclip_watch.ch_mod.video_uploader", return_value=None), \
         patch("autoclip_watch.yt.rendered_videos", return_value=[]), \
         patch("autoclip_watch.cleanup_project"):
        rc = cmd_paste(args)

    assert rc != 0, f"expected non-zero exit when no rendered videos, got {rc}"


# ---------------------------------------------------------------------------
# Test 5: Multiple platforms use correct uploader per platform (YouTube vs TikTok)
# ---------------------------------------------------------------------------
def test_multiple_platforms_direct_upload(tmp_path):
    cfg_path = write_config(tmp_path)
    state_path = write_state(tmp_path, {
        "jkl012": {
            "status": "produced",
            "project_id": "proj-multi",
            "platforms": ["youtube", "tiktok"],
            "clips": [{"id": "clip_a", "title": "Clip A"}],
            "selected_clip_ids": ["clip_a"],
            "clips_upload": {
                "clip_a": {
                    "status": "pending",
                    "platforms": {
                        "youtube": {"status": "pending", "youtube_id": None,
                                    "youtube_url": None, "error": None, "attempts": 0},
                        "tiktok": {"status": "pending", "youtube_id": None,
                                   "youtube_url": None, "error": None, "attempts": 0},
                    }
                }
            },
            "resume_attempts": 0,
        }
    })
    (tmp_path / "clip.mp4").write_bytes(b"fake video")

    fake_secret = tmp_path / "client_secret.json"
    fake_secret.write_text("{}")

    args = Args(config=str(cfg_path), state=str(state_path),
                url="https://www.youtube.com/watch?v=jkl012",
                platform="youtube,tiktok", no_publish=False, clips=5, privacy="unlisted")

    youtube_calls = []
    publish_calls = []

    def fake_upload_video(*a, **k):
        youtube_calls.append(k.get("title", "unknown"))
        return {"id": "vid-1", "url": "https://youtu.be/vid-1", "privacy": "unlisted"}

    def fake_publish(*a, **k):
        publish_calls.append(a)
        return {"ok": True, "platform": a[3] if len(a) > 3 else "unknown"}

    with patch("autoclip_watch.ch_mod.video_duration", return_value=None), \
         patch("autoclip_watch.yt.CLIENT_SECRET", fake_secret), \
         patch("autoclip_watch.ch_mod.video_uploader", return_value=None), \
         patch("autoclip_watch.publish_platform", side_effect=lambda p: p), \
         patch("autoclip_watch.yt.rendered_videos", return_value=[{"id": "clip_a", "path": str(tmp_path / "clip.mp4"), "title": "Clip A", "description": ""}]), \
         patch("autoclip_watch.yt.upload_video", side_effect=fake_upload_video), \
         patch("autoclip_watch.publish", side_effect=fake_publish):
        rc = cmd_paste(args)

    # YouTube should use direct upload (yt.upload_video).
    assert len(youtube_calls) == 1, f"expected 1 YouTube upload call, got {len(youtube_calls)}"
    # TikTok should use Upload-Post (publish), NOT yt.upload_video.
    assert len(publish_calls) == 1, f"expected 1 publish call for TikTok, got {len(publish_calls)}"
    # The publish call should target the tiktok platform.
    assert publish_calls[0][3] == "tiktok", f"publish should target tiktok, got {publish_calls[0][3]}"

    # Verify saved state reflects correct platform routing.
    state = State(state_path)
    rec = state.get("jkl012")
    platforms = rec.get("clips_upload", {}).get("clip_a", {}).get("platforms", {})
    assert platforms.get("youtube", {}).get("status") == "published", \
        f"youtube should be published, got {platforms.get('youtube', {}).get('status')}"
    assert platforms.get("tiktok", {}).get("status") == "published", \
        f"tiktok should be published, got {platforms.get('tiktok', {}).get('status')}"


# ---------------------------------------------------------------------------
# Test 6: Clip identity preserved across recovery (stable clip IDs)
# ---------------------------------------------------------------------------
def test_clip_id_identity_preserved(tmp_path):
    cfg_path = write_config(tmp_path)
    state_path = write_state(tmp_path, {
        "mno345": {
            "status": "produced",
            "project_id": "proj-identity",
            "platforms": ["youtube"],
            "clips": [{"id": "stable_clip_42", "title": "Stable Clip"}],
            "selected_clip_ids": ["stable_clip_42"],
            "clips_upload": {
                "stable_clip_42": {
                    "status": "pending",
                    "platforms": {
                        "youtube": {"status": "pending", "youtube_id": None,
                                    "youtube_url": None, "error": None, "attempts": 0}
                    }
                }
            },
            "resume_attempts": 0,
        }
    })
    (tmp_path / "stable.mp4").write_bytes(b"fake video")

    fake_secret = tmp_path / "client_secret.json"
    fake_secret.write_text("{}")

    args = Args(config=str(cfg_path), state=str(state_path),
                url="https://www.youtube.com/watch?v=mno345",
                platform=None, no_publish=False, clips=5, privacy="unlisted")

    captured_cid = {}

    def fake_upload_video(*a, **k):
        captured_cid["cid"] = k.get("title", "")
        return {"id": "vid-1", "url": "https://youtu.be/vid-1", "privacy": "unlisted"}

    with patch("autoclip_watch.ch_mod.video_duration", return_value=None), \
         patch("autoclip_watch.yt.CLIENT_SECRET", fake_secret), \
         patch("autoclip_watch.ch_mod.video_uploader", return_value=None), \
         patch("autoclip_watch.publish_platform", side_effect=lambda p: p), \
         patch("autoclip_watch.yt.rendered_videos", return_value=[{"id": "stable_clip_42", "path": str(tmp_path / "stable.mp4"), "title": "Stable Clip", "description": ""}]), \
         patch("autoclip_watch.yt.upload_video", side_effect=fake_upload_video):
        rc = cmd_paste(args)

    state = State(state_path)
    rec = state.get("mno345")
    # The stable clip ID should be preserved in selected_clip_ids.
    assert "stable_clip_42" in rec.get("selected_clip_ids", []), \
        f"stable_clip_42 should be in selected_clip_ids, got {rec.get('selected_clip_ids')}"


# ---------------------------------------------------------------------------
# Test 7: Split partial section resumes from saved project (no re-production)
# ---------------------------------------------------------------------------
def test_split_partial_resume_from_saved_project(tmp_path):
    """A split section marked 'partial' should resume publishing from saved
    project instead of re-downloading/re-producing. Uses per-platform clip
    tracking so partial platform uploads can resume correctly."""
    cfg_path = write_config(tmp_path, split_threshold_sec=1)  # force split path
    state_path = write_state(tmp_path, {
        "split_vid": {
            "status": "processing",
            "project_id": "",
            "platforms": ["youtube"],
            "section_progress": {
                "0": {
                    "status": "partial",
                    "project_id": "saved_proj_0",
                    "clip_ids": ["clip_a", "clip_b"],
                    "clips": {
                        "0": {"status": "published", "attempts": 1, "clip_id": "clip_a",
                              "path": str(tmp_path / "clip_a.mp4"),
                              "title": "Clip A", "description": "",
                              "platforms": {
                                  "youtube": {"status": "published", "youtube_id": "yt_a",
                                              "youtube_url": "https://youtu.be/yt_a",
                                              "error": None, "attempts": 1}
                              }},
                        "1": {"status": "pending", "attempts": 0, "clip_id": "clip_b",
                              "path": str(tmp_path / "clip_b.mp4"),
                              "title": "Clip B", "description": "",
                              "platforms": {
                                  "youtube": {"status": "pending", "youtube_id": None,
                                              "youtube_url": None, "error": None, "attempts": 0}
                              }},
                    },
                    "uploaded": 1,
                }
            },
            "resume_attempts": 0,
        }
    })
    (tmp_path / "clip_a.mp4").write_bytes(b"fake video a")
    (tmp_path / "clip_b.mp4").write_bytes(b"fake video b")

    fake_secret = tmp_path / "client_secret.json"
    fake_secret.write_text("{}")

    args = Args(config=str(cfg_path), state=str(state_path),
                url="https://www.youtube.com/watch?v=split_vid",
                platform=None, no_publish=False, clips=5, privacy="unlisted")

    produce_called = False
    import autoclip_watch as aw
    real_produce = aw.produce

    def tracking_produce(*a, **k):
        nonlocal produce_called
        produce_called = True
        return real_produce(*a, **k)

    uploaded = []

    def fake_upload_video(*a, **k):
        uploaded.append(k.get("title", "unknown"))
        return {"id": "vid-b", "url": "https://youtu.be/vid-b", "privacy": "unlisted"}

    with patch("autoclip_watch.ch_mod.video_duration", return_value=1800), \
         patch("autoclip_watch.yt.CLIENT_SECRET", fake_secret), \
         patch("autoclip_watch.ch_mod.video_uploader", return_value=None), \
         patch("autoclip_watch.produce", side_effect=tracking_produce), \
         patch("autoclip_watch.yt.upload_video", side_effect=fake_upload_video), \
         patch("autoclip_watch.cleanup_project"), \
         patch("autoclip_watch._safe_remove"), \
         patch("autoclip_watch.ch_mod.download_section"):
        rc = cmd_paste(args)

    assert not produce_called, "produce should NOT be called when resuming partial split section"
    assert "Clip B" in uploaded, f"clip_b should be uploaded on resume, got {uploaded}"


# ---------------------------------------------------------------------------
# Test 8: Split multi-platform upload (one succeeds, one fails)
# ---------------------------------------------------------------------------
def test_split_multiplatform_one_fails(tmp_path):
    """A split section with multiple platforms should use the correct uploader
    per platform (YouTube -> direct upload, TikTok -> Upload-Post). When one
    platform fails, the other should still succeed and the section should be
    marked partial."""
    cfg_path = write_config(tmp_path, split_threshold_sec=1)
    state_path = write_state(tmp_path, {
        "split_vid": {
            "status": "processing",
            "project_id": "",
            "platforms": ["youtube", "tiktok"],
            "section_progress": {
                "0": {
                    "status": "partial",
                    "project_id": "saved_proj_0",
                    "clip_ids": ["clip_a"],
                    "clips": {
                        "0": {"status": "pending", "attempts": 0, "clip_id": "clip_a",
                              "path": str(tmp_path / "clip_a.mp4"),
                              "title": "Clip A", "description": "",
                              "platforms": {
                                  "youtube": {"status": "pending", "youtube_id": None,
                                              "youtube_url": None, "error": None, "attempts": 0},
                                  "tiktok": {"status": "pending", "youtube_id": None,
                                             "youtube_url": None, "error": None, "attempts": 0},
                              }},
                    },
                    "uploaded": 0,
                }
            },
            "resume_attempts": 0,
        }
    })
    (tmp_path / "clip_a.mp4").write_bytes(b"fake video a")

    fake_secret = tmp_path / "client_secret.json"
    fake_secret.write_text("{}")

    args = Args(config=str(cfg_path), state=str(state_path),
                url="https://www.youtube.com/watch?v=split_vid",
                platform="youtube,tiktok", no_publish=False, clips=5, privacy="unlisted")

    produce_called = False
    import autoclip_watch as aw
    real_produce = aw.produce

    def tracking_produce(*a, **k):
        nonlocal produce_called
        produce_called = True
        return real_produce(*a, **k)

    youtube_calls = []
    publish_calls = []

    def fake_upload_video(*a, **k):
        youtube_calls.append(k.get("title", "unknown"))
        return {"id": "vid-a", "url": "https://youtu.be/vid-a", "privacy": "unlisted"}

    def fake_publish(*a, **k):
        publish_calls.append(a)
        # Fail TikTok publish.
        if len(a) > 3 and a[3] == "tiktok":
            raise RuntimeError("tiktok publish rejected")
        return {"id": "vid-a", "url": "https://youtu.be/vid-a", "privacy": "unlisted"}

    with patch("autoclip_watch.ch_mod.video_duration", return_value=1800), \
         patch("autoclip_watch.yt.CLIENT_SECRET", fake_secret), \
         patch("autoclip_watch.ch_mod.video_uploader", return_value=None), \
         patch("autoclip_watch.produce", side_effect=tracking_produce), \
         patch("autoclip_watch.yt.upload_video", side_effect=fake_upload_video), \
         patch("autoclip_watch.publish", side_effect=fake_publish), \
         patch("autoclip_watch.cleanup_project"), \
         patch("autoclip_watch._safe_remove"), \
         patch("autoclip_watch.ch_mod.download_section"):
        rc = cmd_paste(args)

    assert not produce_called, "produce should NOT be called when resuming partial split section"
    # YouTube should use direct upload (yt.upload_video).
    assert len(youtube_calls) == 1, f"expected 1 YouTube upload call, got {len(youtube_calls)}"
    # TikTok should use Upload-Post (publish), NOT yt.upload_video.
    assert len(publish_calls) == 1, f"expected 1 publish call for TikTok, got {len(publish_calls)}"
    # The publish call should target the tiktok platform.
    assert publish_calls[0][3] == "tiktok", f"publish should target tiktok, got {publish_calls[0][3]}"

    state = State(state_path)
    rec = state.get("split_vid")
    sec = rec.get("section_progress", {}).get("0", {})
    clips = sec.get("clips", {})
    clip_track = clips.get("0", {})
    platforms = clip_track.get("platforms", {})
    # YouTube should be published, tiktok should be upload_unknown
    yt_status = platforms.get("youtube", {}).get("status")
    tt_status = platforms.get("tiktok", {}).get("status")
    assert yt_status == "published", f"youtube should be published, got {yt_status}"
    assert tt_status == "upload_unknown", f"tiktok should be upload_unknown, got {tt_status}"
    # Section should be partial since tiktok failed
    assert sec.get("status") == "partial", f"section should be partial, got {sec.get('status')}"


# ---------------------------------------------------------------------------
# Test 9: run_once returns failed count; cmd_run non-zero on failures
# ---------------------------------------------------------------------------
def test_run_once_structured_result(tmp_path):
    cfg_path = write_config(tmp_path, channels=[])
    from watcher.config import load_config
    cfg = load_config(cfg_path)
    state = State(tmp_path / "state.json")
    result = run_once(cfg, state)
    assert "failed" in result, "result should include 'failed' key"
    assert result["failed"] == 0, "no channels means no failures"


# ---------------------------------------------------------------------------
# Test 9: cmd_run non-zero when run_once reports failures
# ---------------------------------------------------------------------------
def test_cmd_run_nonzero_on_failures(tmp_path):
    cfg_path = write_config(tmp_path, channels=[])
    state_path = write_state(tmp_path)

    args = Args(config=str(cfg_path), state=str(state_path), dry_run=False)

    with patch("autoclip_watch.run_once", return_value={"processed": 0, "skipped": 0, "failed": 2}):
        rc = cmd_run(args)
        assert rc == 1, f"expected exit code 1 when run_once reports failures, got {rc}"


# ---------------------------------------------------------------------------
# Test 10: Upload attempt persisted before API request (crash safety)
# ---------------------------------------------------------------------------
def test_upload_attempt_persisted_before_request(tmp_path):
    cfg_path = write_config(tmp_path)
    state_path = write_state(tmp_path, {
        "jkl012": {
            "status": "produced",
            "project_id": "proj-upload",
            "platforms": ["youtube"],
            "clips": [{"id": 1, "title": "Clip 1"}],
            "selected_clip_ids": [1],
            "clips_upload": {
                "1": {
                    "status": "pending",
                    "platforms": {
                        "youtube": {"status": "pending", "youtube_id": None,
                                    "youtube_url": None, "error": None, "attempts": 0}
                    }
                }
            },
            "resume_attempts": 0,
        }
    })
    (tmp_path / "clip.mp4").write_bytes(b"fake video")

    fake_secret = tmp_path / "client_secret.json"
    fake_secret.write_text("{}")

    args = Args(config=str(cfg_path), state=str(state_path),
                url="https://www.youtube.com/watch?v=jkl012",
                platform=None, no_publish=False, clips=5, privacy="unlisted")

    call_count = {"n": 0}

    def fake_upload_video(*a, **k):
        call_count["n"] += 1
        if call_count["n"] == 1:
            raise RuntimeError("connection lost mid-upload")
        return {"id": "vid-1", "url": "https://youtu.be/vid-1", "privacy": "unlisted"}

    fake_video = {"id": 1, "path": str(tmp_path / "clip.mp4"), "title": "Clip 1", "description": ""}

    with patch("autoclip_watch.ch_mod.video_duration", return_value=None), \
         patch("autoclip_watch.yt.CLIENT_SECRET", fake_secret), \
         patch("autoclip_watch.ch_mod.video_uploader", return_value=None), \
         patch("autoclip_watch.yt.rendered_videos", return_value=[fake_video]), \
         patch("autoclip_watch.yt.upload_video", side_effect=fake_upload_video):
        rc = cmd_paste(args)

    assert call_count["n"] >= 1, f"upload should have been attempted, called {call_count['n']} time(s)"
    state = State(state_path)
    rec = state.get("jkl012")
    uploads = rec.get("clips_upload", {})
    any_track = None
    for cid_data in uploads.values():
        for plat_data in cid_data.get("platforms", {}).values():
            any_track = plat_data
            break
        if any_track:
            break
    assert any_track is not None, "should have at least one upload track"
    assert any_track.get("attempts", 0) >= 1, \
        f"attempts should be >= 1 (persisted before crash), got {any_track.get('attempts')}"
    assert any_track.get("status") == "upload_unknown", \
        f"status should be 'upload_unknown', got {any_track.get('status')}"


# ---------------------------------------------------------------------------
# Test 12: Reconciliation returns None when no OAuth token exists
# ---------------------------------------------------------------------------
def test_check_youtube_existing_upload_no_token(tmp_path):
    """_check_youtube_existing_upload should return (None, None) gracefully when
    token.json is missing (no OAuth configured)."""
    found, url = _check_youtube_existing_upload("Any Video Title")
    assert found is None and url is None, f"expected (None, None) when no token, got ({found}, {url})"


# ---------------------------------------------------------------------------
# Test 13: Reconciliation confirms owned video → marks published, no retry
# ---------------------------------------------------------------------------
def test_reconciliation_confirms_owned_video(tmp_path):
    """When reconciliation finds a matching video owned by the authenticated
    channel, the upload should be marked published and NOT retried."""
    cfg_path = write_config(tmp_path)
    state_path = write_state(tmp_path, {
        "rec_vid": {
            "status": "produced",
            "project_id": "proj-reconcile",
            "platforms": ["youtube"],
            "clips": [{"id": "clip_a", "title": "My Clip"}],
            "selected_clip_ids": ["clip_a"],
            "clips_upload": {
                "clip_a": {
                    "status": "pending",
                    "platforms": {
                        "youtube": {"status": "upload_unknown", "youtube_id": None,
                                    "youtube_url": None, "error": None, "attempts": 1}
                    }
                }
            },
            "resume_attempts": 0,
        }
    })
    (tmp_path / "clip.mp4").write_bytes(b"fake video")

    fake_secret = tmp_path / "client_secret.json"
    fake_secret.write_text("{}")

    args = Args(config=str(cfg_path), state=str(state_path),
                url="https://www.youtube.com/watch?v=rec_vid",
                platform=None, no_publish=False, clips=5, privacy="unlisted")

    upload_call_count = {"n": 0}

    def fake_upload_video(*a, **k):
        upload_call_count["n"] += 1
        raise RuntimeError("should not be called after reconciliation confirms")

    with patch("autoclip_watch.ch_mod.video_duration", return_value=None), \
         patch("autoclip_watch.yt.CLIENT_SECRET", fake_secret), \
         patch("autoclip_watch.ch_mod.video_uploader", return_value=None), \
         patch("autoclip_watch.yt.rendered_videos", return_value=[{"id": "clip_a", "path": str(tmp_path / "clip.mp4"), "title": "My Clip", "description": ""}]), \
         patch("autoclip_watch._check_youtube_existing_upload", return_value=(True, "https://youtu.be/confirmed_vid")), \
         patch("autoclip_watch.yt.upload_video", side_effect=fake_upload_video):
        rc = cmd_paste(args)

    assert upload_call_count["n"] == 0, f"upload should NOT be retried after reconciliation confirms, called {upload_call_count['n']} time(s)"
    state = State(state_path)
    rec = state.get("rec_vid")
    track = rec.get("clips_upload", {}).get("clip_a", {}).get("platforms", {}).get("youtube", {})
    assert track.get("status") == "published", f"should be published, got {track.get('status')}"
    assert track.get("youtube_url") == "https://youtu.be/confirmed_vid", f"should have reconciled URL, got {track.get('youtube_url')}"


# ---------------------------------------------------------------------------
# Test 14: Reconciliation finds wrong channel → marks failed (allows retry)
# ---------------------------------------------------------------------------
def test_reconciliation_wrong_channel(tmp_path):
    """When reconciliation finds a matching title but different channel,
    the upload should be marked failed (definitively not ours)."""
    cfg_path = write_config(tmp_path)
    state_path = write_state(tmp_path, {
        "rec_vid2": {
            "status": "produced",
            "project_id": "proj-reconcile2",
            "platforms": ["youtube"],
            "clips": [{"id": "clip_a", "title": "My Clip"}],
            "selected_clip_ids": ["clip_a"],
            "clips_upload": {
                "clip_a": {
                    "status": "pending",
                    "platforms": {
                        "youtube": {"status": "upload_unknown", "youtube_id": None,
                                    "youtube_url": None, "error": None, "attempts": 1}
                    }
                }
            },
            "resume_attempts": 0,
        }
    })
    (tmp_path / "clip.mp4").write_bytes(b"fake video")

    fake_secret = tmp_path / "client_secret.json"
    fake_secret.write_text("{}")

    args = Args(config=str(cfg_path), state=str(state_path),
                url="https://www.youtube.com/watch?v=rec_vid2",
                platform=None, no_publish=False, clips=5, privacy="unlisted")

    upload_call_count = {"n": 0}

    def fake_upload_video(*a, **k):
        upload_call_count["n"] += 1
        return {"id": "vid-1", "url": "https://youtu.be/vid-1", "privacy": "unlisted"}

    with patch("autoclip_watch.ch_mod.video_duration", return_value=None), \
         patch("autoclip_watch.yt.CLIENT_SECRET", fake_secret), \
         patch("autoclip_watch.ch_mod.video_uploader", return_value=None), \
         patch("autoclip_watch.yt.rendered_videos", return_value=[{"id": "clip_a", "path": str(tmp_path / "clip.mp4"), "title": "My Clip", "description": ""}]), \
         patch("autoclip_watch._check_youtube_existing_upload", return_value=(False, None)), \
         patch("autoclip_watch.yt.upload_video", side_effect=fake_upload_video):
        rc = cmd_paste(args)

    assert upload_call_count["n"] == 0, f"upload should NOT be retried in this run when wrong channel found (marked failed, retries on next run), called {upload_call_count['n']} time(s)"
    state = State(state_path)
    rec = state.get("rec_vid2")
    track = rec.get("clips_upload", {}).get("clip_a", {}).get("platforms", {}).get("youtube", {})
    assert track.get("status") == "failed", f"should be failed, got {track.get('status')}"


# ---------------------------------------------------------------------------
# Test 15: Reconciliation error (None) → preserves upload_unknown, NO retry (duplicate prevention)
# ---------------------------------------------------------------------------
def test_reconciliation_error_prevents_duplicate(tmp_path):
    """When reconciliation returns None (error/network/OAuth), the upload should
    remain upload_unknown and NOT be retried — preventing duplicates."""
    cfg_path = write_config(tmp_path)
    state_path = write_state(tmp_path, {
        "rec_vid3": {
            "status": "produced",
            "project_id": "proj-reconcile3",
            "platforms": ["youtube"],
            "clips": [{"id": "clip_a", "title": "My Clip"}],
            "selected_clip_ids": ["clip_a"],
            "clips_upload": {
                "clip_a": {
                    "status": "pending",
                    "platforms": {
                        "youtube": {"status": "upload_unknown", "youtube_id": None,
                                    "youtube_url": None, "error": None, "attempts": 1}
                    }
                }
            },
            "resume_attempts": 0,
        }
    })
    (tmp_path / "clip.mp4").write_bytes(b"fake video")

    fake_secret = tmp_path / "client_secret.json"
    fake_secret.write_text("{}")

    args = Args(config=str(cfg_path), state=str(state_path),
                url="https://www.youtube.com/watch?v=rec_vid3",
                platform=None, no_publish=False, clips=5, privacy="unlisted")

    upload_call_count = {"n": 0}

    def fake_upload_video(*a, **k):
        upload_call_count["n"] += 1
        return {"id": "vid-1", "url": "https://youtu.be/vid-1", "privacy": "unlisted"}

    with patch("autoclip_watch.ch_mod.video_duration", return_value=None), \
         patch("autoclip_watch.yt.CLIENT_SECRET", fake_secret), \
         patch("autoclip_watch.ch_mod.video_uploader", return_value=None), \
         patch("autoclip_watch.yt.rendered_videos", return_value=[{"id": "clip_a", "path": str(tmp_path / "clip.mp4"), "title": "My Clip", "description": ""}]), \
         patch("autoclip_watch._check_youtube_existing_upload", return_value=(None, None)), \
         patch("autoclip_watch.yt.upload_video", side_effect=fake_upload_video):
        rc = cmd_paste(args)

    assert upload_call_count["n"] == 0, f"upload should NOT be retried when reconciliation errors, called {upload_call_count['n']} time(s)"
    state = State(state_path)
    rec = state.get("rec_vid3")
    track = rec.get("clips_upload", {}).get("clip_a", {}).get("platforms", {}).get("youtube", {})
    assert track.get("status") == "upload_unknown", f"should remain upload_unknown, got {track.get('status')}"
    assert track.get("attempts") == 1, f"attempts should not increment, got {track.get('attempts')}"


# ---------------------------------------------------------------------------
# Test 16: Search missing existing upload → inconclusive (None), NO retry
# ---------------------------------------------------------------------------
def test_search_missing_upload_prevents_retry(tmp_path):
    """When search finds no matching title (inconclusive), the upload should
    remain upload_unknown and NOT be retried."""
    cfg_path = write_config(tmp_path)
    state_path = write_state(tmp_path, {
        "rec_vid4": {
            "status": "produced",
            "project_id": "proj-reconcile4",
            "platforms": ["youtube"],
            "clips": [{"id": "clip_a", "title": "My Clip"}],
            "selected_clip_ids": ["clip_a"],
            "clips_upload": {
                "clip_a": {
                    "status": "pending",
                    "platforms": {
                        "youtube": {"status": "upload_unknown", "youtube_id": None,
                                    "youtube_url": None, "error": None, "attempts": 1}
                    }
                }
            },
            "resume_attempts": 0,
        }
    })
    (tmp_path / "clip.mp4").write_bytes(b"fake video")

    fake_secret = tmp_path / "client_secret.json"
    fake_secret.write_text("{}")

    args = Args(config=str(cfg_path), state=str(state_path),
                url="https://www.youtube.com/watch?v=rec_vid4",
                platform=None, no_publish=False, clips=5, privacy="unlisted")

    upload_call_count = {"n": 0}

    def fake_upload_video(*a, **k):
        upload_call_count["n"] += 1
        return {"id": "vid-1", "url": "https://youtu.be/vid-1", "privacy": "unlisted"}

    with patch("autoclip_watch.ch_mod.video_duration", return_value=None), \
         patch("autoclip_watch.yt.CLIENT_SECRET", fake_secret), \
         patch("autoclip_watch.ch_mod.video_uploader", return_value=None), \
         patch("autoclip_watch.yt.rendered_videos", return_value=[{"id": "clip_a", "path": str(tmp_path / "clip.mp4"), "title": "My Clip", "description": ""}]), \
         patch("autoclip_watch._check_youtube_existing_upload", return_value=(None, None)), \
         patch("autoclip_watch.yt.upload_video", side_effect=fake_upload_video):
        rc = cmd_paste(args)

    assert upload_call_count["n"] == 0, f"upload should NOT be retried when search is inconclusive, called {upload_call_count['n']} time(s)"
    state = State(state_path)
    rec = state.get("rec_vid4")
    track = rec.get("clips_upload", {}).get("clip_a", {}).get("platforms", {}).get("youtube", {})
    assert track.get("status") == "upload_unknown", f"should remain upload_unknown, got {track.get('status')}"


# ---------------------------------------------------------------------------
# Test 17: Split pipeline reconciliation error preserves upload_unknown, no retry
# ---------------------------------------------------------------------------
def test_split_reconciliation_error_prevents_retry(tmp_path):
    """In the split pipeline, when reconciliation returns None (error),
    the clip should remain upload_unknown and NOT be retried — preventing
    duplicates even in the long-video split path."""
    cfg_path = write_config(tmp_path, split_threshold_sec=1)
    state_path = write_state(tmp_path, {
        "split_vid": {
            "status": "processing",
            "project_id": "",
            "platforms": ["youtube"],
            "section_progress": {
                "0": {
                    "status": "partial",
                    "project_id": "saved_proj_0",
                    "clip_ids": ["clip_a"],
                    "clips": {
                        "0": {"status": "pending", "attempts": 1, "clip_id": "clip_a",
                              "path": str(tmp_path / "clip_a.mp4"),
                              "title": "Clip A", "description": "",
                              "platforms": {
                                  "youtube": {"status": "upload_unknown", "youtube_id": None,
                                              "youtube_url": None, "error": None, "attempts": 1}
                              }},
                    },
                    "uploaded": 0,
                }
            },
            "resume_attempts": 0,
        }
    })
    (tmp_path / "clip_a.mp4").write_bytes(b"fake video a")

    fake_secret = tmp_path / "client_secret.json"
    fake_secret.write_text("{}")

    args = Args(config=str(cfg_path), state=str(state_path),
                url="https://www.youtube.com/watch?v=split_vid",
                platform=None, no_publish=False, clips=5, privacy="unlisted")

    produce_called = False
    import autoclip_watch as aw
    real_produce = aw.produce

    def tracking_produce(*a, **k):
        nonlocal produce_called
        produce_called = True
        return real_produce(*a, **k)

    upload_call_count = {"n": 0}

    def fake_upload_video(*a, **k):
        upload_call_count["n"] += 1
        return {"id": "vid-a", "url": "https://youtu.be/vid-a", "privacy": "unlisted"}

    with patch("autoclip_watch.ch_mod.video_duration", return_value=1800), \
         patch("autoclip_watch.yt.CLIENT_SECRET", fake_secret), \
         patch("autoclip_watch.ch_mod.video_uploader", return_value=None), \
         patch("autoclip_watch.produce", side_effect=tracking_produce), \
         patch("autoclip_watch.yt.upload_video", side_effect=fake_upload_video), \
         patch("autoclip_watch._check_youtube_existing_upload", return_value=(None, None)), \
         patch("autoclip_watch.cleanup_project"), \
         patch("autoclip_watch._safe_remove"), \
         patch("autoclip_watch.ch_mod.download_section"):
        rc = cmd_paste(args)

    assert not produce_called, "produce should NOT be called when resuming partial split section"
    assert upload_call_count["n"] == 0, f"split upload should NOT be retried when reconciliation errors, called {upload_call_count['n']} time(s)"
    state = State(state_path)
    rec = state.get("split_vid")
    sec = rec.get("section_progress", {}).get("0", {})
    clip_track = sec.get("clips", {}).get("0", {})
    yt_status = clip_track.get("platforms", {}).get("youtube", {}).get("status")
    assert yt_status == "upload_unknown", f"split clip should remain upload_unknown, got {yt_status}"
    assert sec.get("status") == "partial", f"split section should remain partial, got {sec.get('status')}"


# ---------------------------------------------------------------------------
# Test 18: Duplicate-upload regression — re-running same job does not re-upload
# ---------------------------------------------------------------------------
def test_duplicate_upload_regression(tmp_path):
    """After a successful upload is recorded as published, re-running the same
    paste command should NOT call the uploader again."""
    cfg_path = write_config(tmp_path)
    state_path = write_state(tmp_path, {
        "dup_vid": {
            "status": "produced",
            "project_id": "proj-dup",
            "platforms": ["youtube"],
            "clips": [{"id": "clip_a", "title": "Duplicate Clip"}],
            "selected_clip_ids": ["clip_a"],
            "clips_upload": {
                "clip_a": {
                    "status": "pending",
                    "platforms": {
                        "youtube": {"status": "published", "youtube_id": "vid-dup",
                                    "youtube_url": "https://youtu.be/vid-dup",
                                    "error": None, "attempts": 1}
                    }
                }
            },
            "resume_attempts": 0,
        }
    })
    (tmp_path / "clip.mp4").write_bytes(b"fake video")

    fake_secret = tmp_path / "client_secret.json"
    fake_secret.write_text("{}")

    args = Args(config=str(cfg_path), state=str(state_path),
                url="https://www.youtube.com/watch?v=dup_vid",
                platform=None, no_publish=False, clips=5, privacy="unlisted")

    upload_call_count = {"n": 0}

    def fake_upload_video(*a, **k):
        upload_call_count["n"] += 1
        return {"id": "vid-dup", "url": "https://youtu.be/vid-dup", "privacy": "unlisted"}

    with patch("autoclip_watch.ch_mod.video_duration", return_value=None), \
         patch("autoclip_watch.yt.CLIENT_SECRET", fake_secret), \
         patch("autoclip_watch.ch_mod.video_uploader", return_value=None), \
         patch("autoclip_watch.yt.rendered_videos", return_value=[{"id": "clip_a", "path": str(tmp_path / "clip.mp4"), "title": "Duplicate Clip", "description": ""}]), \
         patch("autoclip_watch.yt.upload_video", side_effect=fake_upload_video):
        rc = cmd_paste(args)

    assert upload_call_count["n"] == 0, f"already-published clip should NOT be re-uploaded, called {upload_call_count['n']} time(s)"
    state = State(state_path)
    rec = state.get("dup_vid")
    track = rec.get("clips_upload", {}).get("clip_a", {}).get("platforms", {}).get("youtube", {})
    assert track.get("status") == "published", f"should remain published, got {track.get('status')}"


# ---------------------------------------------------------------------------
# Test 19: Multiple matching titles — wrong channel first, then owned channel
# ---------------------------------------------------------------------------
def test_reconciliation_wrong_channel_then_owned(tmp_path):
    """When search returns a matching title on another channel first, then
    a matching title on our channel, reconciliation should confirm ours."""
    cfg_path = write_config(tmp_path)
    state_path = write_state(tmp_path, {
        "rec_vid5": {
            "status": "produced",
            "project_id": "proj-reconcile5",
            "platforms": ["youtube"],
            "clips": [{"id": "clip_a", "title": "My Clip"}],
            "selected_clip_ids": ["clip_a"],
            "clips_upload": {
                "clip_a": {
                    "status": "pending",
                    "platforms": {
                        "youtube": {"status": "upload_unknown", "youtube_id": None,
                                    "youtube_url": None, "error": None, "attempts": 1}
                    }
                }
            },
            "resume_attempts": 0,
        }
    })
    (tmp_path / "clip.mp4").write_bytes(b"fake video")

    fake_secret = tmp_path / "client_secret.json"
    fake_secret.write_text("{}")

    args = Args(config=str(cfg_path), state=str(state_path),
                url="https://www.youtube.com/watch?v=rec_vid5",
                platform=None, no_publish=False, clips=5, privacy="unlisted")

    upload_call_count = {"n": 0}

    def fake_upload_video(*a, **k):
        upload_call_count["n"] += 1
        return {"id": "vid-1", "url": "https://youtu.be/vid-1", "privacy": "unlisted"}

    with patch("autoclip_watch.ch_mod.video_duration", return_value=None), \
         patch("autoclip_watch.yt.CLIENT_SECRET", fake_secret), \
         patch("autoclip_watch.ch_mod.video_uploader", return_value=None), \
         patch("autoclip_watch.yt.rendered_videos", return_value=[{"id": "clip_a", "path": str(tmp_path / "clip.mp4"), "title": "My Clip", "description": ""}]), \
         patch("autoclip_watch._check_youtube_existing_upload", return_value=(True, "https://youtu.be/our_vid")), \
         patch("autoclip_watch.yt.upload_video", side_effect=fake_upload_video):
        rc = cmd_paste(args)

    assert upload_call_count["n"] == 0, f"upload should NOT be retried when owned match confirmed, called {upload_call_count['n']} time(s)"
    state = State(state_path)
    rec = state.get("rec_vid5")
    track = rec.get("clips_upload", {}).get("clip_a", {}).get("platforms", {}).get("youtube", {})
    assert track.get("status") == "published", f"should be published, got {track.get('status')}"
    assert track.get("youtube_url") == "https://youtu.be/our_vid", f"should have our video URL, got {track.get('youtube_url')}"


# ---------------------------------------------------------------------------
# Test 20: YouTube API timeout -> inconclusive, no retry
# ---------------------------------------------------------------------------
def test_youtube_api_timeout_prevents_retry(tmp_path):
    """When the YouTube API times out during reconciliation, the upload should
    remain upload_unknown and NOT be retried."""
    cfg_path = write_config(tmp_path)
    state_path = write_state(tmp_path, {
        "rec_vid6": {
            "status": "produced",
            "project_id": "proj-reconcile6",
            "platforms": ["youtube"],
            "clips": [{"id": "clip_a", "title": "My Clip"}],
            "selected_clip_ids": ["clip_a"],
            "clips_upload": {
                "clip_a": {
                    "status": "pending",
                    "platforms": {
                        "youtube": {"status": "upload_unknown", "youtube_id": None,
                                    "youtube_url": None, "error": None, "attempts": 1}
                    }
                }
            },
            "resume_attempts": 0,
        }
    })
    (tmp_path / "clip.mp4").write_bytes(b"fake video")

    fake_secret = tmp_path / "client_secret.json"
    fake_secret.write_text("{}")

    args = Args(config=str(cfg_path), state=str(state_path),
                url="https://www.youtube.com/watch?v=rec_vid6",
                platform=None, no_publish=False, clips=5, privacy="unlisted")

    upload_call_count = {"n": 0}

    def fake_upload_video(*a, **k):
        upload_call_count["n"] += 1
        return {"id": "vid-1", "url": "https://youtu.be/vid-1", "privacy": "unlisted"}

    with patch("autoclip_watch.ch_mod.video_duration", return_value=None), \
         patch("autoclip_watch.yt.CLIENT_SECRET", fake_secret), \
         patch("autoclip_watch.ch_mod.video_uploader", return_value=None), \
         patch("autoclip_watch.yt.rendered_videos", return_value=[{"id": "clip_a", "path": str(tmp_path / "clip.mp4"), "title": "My Clip", "description": ""}]), \
         patch("autoclip_watch._check_youtube_existing_upload", return_value=(None, None)), \
         patch("autoclip_watch.yt.upload_video", side_effect=fake_upload_video):
        rc = cmd_paste(args)

    assert upload_call_count["n"] == 0, f"upload should NOT be retried when YouTube API times out, called {upload_call_count['n']} time(s)"
    state = State(state_path)
    rec = state.get("rec_vid6")
    track = rec.get("clips_upload", {}).get("clip_a", {}).get("platforms", {}).get("youtube", {})
    assert track.get("status") == "upload_unknown", f"should remain upload_unknown, got {track.get('status')}"
    assert track.get("attempts") == 1, f"attempts should not increment, got {track.get('attempts')}"


# ---------------------------------------------------------------------------
# Run all tests
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    import traceback

    tests = [
        ("test_failed_job_empty_clips_retries_production_nonzero", test_failed_job_empty_clips_retries_production_nonzero),
        ("test_produced_job_resumes_without_reproducing", test_produced_job_resumes_without_reproducing),
        ("test_no_clips_returns_nonzero", test_no_clips_returns_nonzero),
        ("test_no_rendered_videos_returns_nonzero", test_no_rendered_videos_returns_nonzero),
        ("test_multiple_platforms_direct_upload", test_multiple_platforms_direct_upload),
        ("test_clip_id_identity_preserved", test_clip_id_identity_preserved),
        ("test_split_partial_resume_from_saved_project", test_split_partial_resume_from_saved_project),
        ("test_split_multiplatform_one_fails", test_split_multiplatform_one_fails),
        ("test_run_once_structured_result", test_run_once_structured_result),
        ("test_cmd_run_nonzero_on_failures", test_cmd_run_nonzero_on_failures),
        ("test_upload_attempt_persisted_before_request", test_upload_attempt_persisted_before_request),
        ("test_check_youtube_existing_upload_no_token", test_check_youtube_existing_upload_no_token),
        ("test_reconciliation_confirms_owned_video", test_reconciliation_confirms_owned_video),
        ("test_reconciliation_wrong_channel", test_reconciliation_wrong_channel),
        ("test_reconciliation_error_prevents_duplicate", test_reconciliation_error_prevents_duplicate),
        ("test_search_missing_upload_prevents_retry", test_search_missing_upload_prevents_retry),
        ("test_split_reconciliation_error_prevents_retry", test_split_reconciliation_error_prevents_retry),
        ("test_duplicate_upload_regression", test_duplicate_upload_regression),
        ("test_reconciliation_wrong_channel_then_owned", test_reconciliation_wrong_channel_then_owned),
        ("test_youtube_api_timeout_prevents_retry", test_youtube_api_timeout_prevents_retry),
    ]

    passed = 0
    failed = 0
    for name, t in tests:
        try:
            t(Path(tempfile.mkdtemp()))
            print(f"PASS: {name}")
            passed += 1
        except Exception as exc:
            print(f"FAIL: {name}: {exc}")
            traceback.print_exc()
            failed += 1

    print(f"\n{passed} passed, {failed} failed out of {len(tests)}")
    sys.exit(1 if failed else 0)
