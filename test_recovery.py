"""Tests for YtClipping recovery & reliability fixes (v1.1.2).

Covers:
  - Resume failed jobs with empty clips (retry production, non-zero exit)
  - Resume produced/publishing jobs without regenerating
  - Multiple platforms in direct upload mode
  - Split bounded retries for download/production failures
  - Watcher structured results + non-zero exit on failures
  - Upload attempt persisted before API request (crash safety)
"""

import json
import os
import sys
import tempfile
from pathlib import Path
from unittest.mock import MagicMock, patch

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from autoclip_watch import cmd_paste, cmd_run  # noqa: E402
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
        data = {"videos": videos}
        p.write_text(json.dumps(data, indent=2), encoding="utf-8")
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
# Test 1: Failed job with empty clips retries production instead of returning 0
# ---------------------------------------------------------------------------
def test_failed_job_empty_clips_retries_production(tmp_path):
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

    args = Args(config=str(cfg_path), state=str(state_path), url="https://www.youtube.com/watch?v=abc123",
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
    state = State(state_path)
    rec = state.get("abc123")
    assert rec.get("resume_attempts", 0) >= 1, "resume_attempts should be incremented"


# ---------------------------------------------------------------------------
# Test 2: Produced job resumes without regenerating clips
# ---------------------------------------------------------------------------
def test_produced_job_resumes_without_reproducing(tmp_path):
    cfg_path = write_config(tmp_path)
    state_path = write_state(tmp_path, {
        "def456": {
            "status": "produced",
            "project_id": "proj-existing",
            "platforms": ["youtube"],
            "clips": [{"id": 1, "title": "Clip 1"}],
            "selected_clip_ids": ["1"],
            "resume_attempts": 0,
        }
    })

    args = Args(config=str(cfg_path), state=str(state_path), url="https://www.youtube.com/watch?v=def456",
                platform=None, no_publish=False, clips=5, privacy="unlisted")

    with patch("autoclip_watch.ch_mod.video_duration", return_value=None), \
         patch("autoclip_watch.yt.CLIENT_SECRET", Path("/nonexistent/client_secret.json")), \
         patch("autoclip_watch.publish") as mock_publish:
        rc = cmd_paste(args)

    assert mock_publish.called, "publish should be called for resumed produced job"


# ---------------------------------------------------------------------------
# Test 3: No clips produced returns non-zero
# ---------------------------------------------------------------------------
def test_no_clips_returns_nonzero(tmp_path):
    cfg_path = write_config(tmp_path)
    state_path = write_state(tmp_path)

    args = Args(config=str(cfg_path), state=str(state_path), url="https://www.youtube.com/watch?v=noclips",
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
            "selected_clip_ids": ["1"],
            "resume_attempts": 0,
        }
    })

    fake_secret = tmp_path / "client_secret.json"
    fake_secret.write_text("{}")

    args = Args(config=str(cfg_path), state=str(state_path), url="https://www.youtube.com/watch?v=ghi789",
                platform=None, no_publish=False, clips=5, privacy="unlisted")

    with patch("autoclip_watch.ch_mod.video_duration", return_value=None), \
         patch("autoclip_watch.yt.CLIENT_SECRET", fake_secret), \
         patch("autoclip_watch.ch_mod.video_uploader", return_value=None), \
         patch("autoclip_watch.yt.rendered_videos", return_value=[]), \
         patch("autoclip_watch.cleanup_project"):
        rc = cmd_paste(args)

    assert rc != 0, f"expected non-zero exit when no rendered videos, got {rc}"


# ---------------------------------------------------------------------------
# Test 5: run_once returns failed count
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
# Test 6: cmd_run returns non-zero when run_once reports failures
# ---------------------------------------------------------------------------
def test_cmd_run_nonzero_on_failures(tmp_path):
    cfg_path = write_config(tmp_path, channels=[])
    state_path = write_state(tmp_path)

    args = Args(config=str(cfg_path), state=str(state_path), dry_run=False)

    with patch("autoclip_watch.run_once", return_value={"processed": 0, "skipped": 0, "failed": 2}):
        rc = cmd_run(args)
        assert rc == 1, f"expected exit code 1 when run_once reports failures, got {rc}"


# ---------------------------------------------------------------------------
# Test 7: Upload attempt persisted before API request (crash safety)
# ---------------------------------------------------------------------------
def test_upload_attempt_persisted_before_request(tmp_path):
    cfg_path = write_config(tmp_path)
    state_path = write_state(tmp_path, {
        "jkl012": {
            "status": "produced",
            "project_id": "proj-upload",
            "platforms": ["youtube"],
            "clips": [{"id": 1, "title": "Clip 1", "path": str(tmp_path / "clip.mp4")}],
            "selected_clip_ids": ["1"],
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
    # Create the fake clip file so upload_video doesn't fail on missing file.
    (tmp_path / "clip.mp4").write_bytes(b"fake video")

    fake_secret = tmp_path / "client_secret.json"
    fake_secret.write_text("{}")

    args = Args(config=str(cfg_path), state=str(state_path), url="https://www.youtube.com/watch?v=jkl012",
                platform=None, no_publish=False, clips=5, privacy="unlisted")

    call_count = {"n": 0}

    def fake_upload_video(*a, **k):
        call_count["n"] += 1
        if call_count["n"] == 1:
            raise RuntimeError("connection lost mid-upload")
        return {"id": "vid-1", "url": "https://youtu.be/vid-1", "privacy": "unlisted"}

    fake_video = {"id": 0, "path": str(tmp_path / "clip.mp4"), "title": "Clip 1", "description": ""}

    with patch("autoclip_watch.ch_mod.video_duration", return_value=None), \
         patch("autoclip_watch.yt.CLIENT_SECRET", fake_secret), \
         patch("autoclip_watch.ch_mod.video_uploader", return_value=None), \
         patch("autoclip_watch.yt.rendered_videos", return_value=[fake_video]), \
         patch("autoclip_watch.yt.upload_video", side_effect=fake_upload_video):
        rc = cmd_paste(args)

    assert call_count["n"] >= 1, f"upload should have been attempted, called {call_count['n']} time(s)"
    state = State(state_path)
    rec = state.get("jkl012")
    # Clip IDs are realigned by rendered_videos (index-based), so look up any platform track.
    uploads = rec.get("clips_upload", {})
    any_track = None
    for cid_data in uploads.values():
        for plat_data in cid_data.get("platforms", {}).values():
            any_track = plat_data
            break
        if any_track:
            break
    assert any_track is not None, "should have at least one upload track"
    assert any_track.get("attempts", 0) >= 1, f"attempts should be >= 1 (persisted before crash), got {any_track.get('attempts')}"
    assert any_track.get("status") == "failed", f"status should be 'failed', got {any_track.get('status')}"


# ---------------------------------------------------------------------------
# Run all tests
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    import traceback

    tests = [
        ("test_failed_job_empty_clips_retries_production", test_failed_job_empty_clips_retries_production),
        ("test_produced_job_resumes_without_reproducing", test_produced_job_resumes_without_reproducing),
        ("test_no_clips_returns_nonzero", test_no_clips_returns_nonzero),
        ("test_no_rendered_videos_returns_nonzero", test_no_rendered_videos_returns_nonzero),
        ("test_run_once_structured_result", test_run_once_structured_result),
        ("test_cmd_run_nonzero_on_failures", test_cmd_run_nonzero_on_failures),
        ("test_upload_attempt_persisted_before_request", test_upload_attempt_persisted_before_request),
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
