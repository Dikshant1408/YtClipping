"""Configuration loading and validation for the YtClipping watcher."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List


@dataclass
class Channel:
    id: str
    name: str = ""
    enabled: bool = True
    platforms: List[str] = field(default_factory=list)
    max_clips: int = 0  # 0 -> use global default


@dataclass
class Config:
    channels: List[Channel] = field(default_factory=list)
    platform: str = "youtube_shorts"
    portrait_style: str = "podcast"
    auto_publish: bool = True
    max_clips_per_video: int = 2
    poll_interval_minutes: int = 30
    max_videos_per_channel: int = 5
    autoclip_bin: str = "autoclip"
    produce_timeout_sec: int = 3600
    publish_timeout_sec: int = 120   # FIX #5: separate timeout for publish (was reusing produce's 4h)
    lookback_hours: int = 0
    extra_produce_args: List[str] = field(default_factory=list)
    max_attempts_per_video: int = 3
    split_threshold_sec: int = 6 * 3600   # split streams longer than this (README: 6h default)
    split_part_sec: int = 3 * 3600        # ~3 hours per part (README: ~3h default)
    game: str = ""                        # game/topic for upload metadata (empty = generic mode)

    def channel_platforms(self, ch: Channel) -> List[str]:
        return ch.platforms or [self.platform]

    def channel_max_clips(self, ch: Channel) -> int:
        # FIX #9: 0 means "use global default"; clamp to >= 1 so publish never silently no-ops
        return max(1, ch.max_clips or self.max_clips_per_video)


def _default_config() -> Config:
    return Config(channels=[])


def load_config(path: str | Path) -> Config:
    p = Path(path)
    if not p.exists():
        raise FileNotFoundError(
            f"Config not found: {p}. Copy config.example.json to config.json and edit it."
        )
    data: Dict[str, Any] = json.loads(p.read_text(encoding="utf-8"))

    cfg = _default_config()
    cfg.platform = data.get("platform", cfg.platform)
    cfg.portrait_style = data.get("portrait_style", cfg.portrait_style)
    cfg.auto_publish = bool(data.get("auto_publish", cfg.auto_publish))
    # FIX #9: clamp to >= 1 on load as well
    cfg.max_clips_per_video = max(1, int(data.get("max_clips_per_video", cfg.max_clips_per_video)))
    cfg.poll_interval_minutes = int(data.get("poll_interval_minutes", cfg.poll_interval_minutes))
    cfg.max_videos_per_channel = int(data.get("max_videos_per_channel", cfg.max_videos_per_channel))
    # FIX #14: resolve the bin via shutil.which at runtime in runner.py;
    # still store whatever is in config (could be "autoclip" or a full path),
    # but runner.py's ensure_autoclip uses shutil.which so a bare name works fine.
    cfg.autoclip_bin = data.get("autoclip_bin", cfg.autoclip_bin)
    cfg.produce_timeout_sec = int(data.get("produce_timeout_sec", cfg.produce_timeout_sec))
    cfg.publish_timeout_sec = int(data.get("publish_timeout_sec", cfg.publish_timeout_sec))
    cfg.lookback_hours = int(data.get("lookback_hours", cfg.lookback_hours))
    cfg.extra_produce_args = list(data.get("extra_produce_args", []))
    cfg.max_attempts_per_video = int(data.get("max_attempts_per_video", cfg.max_attempts_per_video))
    cfg.split_threshold_sec = int(data.get("split_threshold_sec", cfg.split_threshold_sec))
    cfg.split_part_sec = int(data.get("split_part_sec", cfg.split_part_sec))
    cfg.game = str(data.get("game", cfg.game))

    channels: List[Channel] = []
    for raw in data.get("channels", []):
        cid = (raw.get("id") or raw.get("url") or "").strip()
        if not cid:
            continue
        platforms = raw.get("platforms") or []
        if isinstance(platforms, str):
            platforms = [platforms]
        channels.append(
            Channel(
                id=cid,
                name=raw.get("name", ""),
                enabled=bool(raw.get("enabled", True)),
                platforms=[str(x) for x in platforms],
                max_clips=int(raw.get("max_clips", 0)),
            )
        )
    cfg.channels = channels
    return cfg


def save_config(cfg: Config, path: str | Path) -> None:
    data = {
        "platform": cfg.platform,
        "portrait_style": cfg.portrait_style,
        "auto_publish": cfg.auto_publish,
        "max_clips_per_video": cfg.max_clips_per_video,
        "poll_interval_minutes": cfg.poll_interval_minutes,
        "max_videos_per_channel": cfg.max_videos_per_channel,
        "autoclip_bin": cfg.autoclip_bin,
        "produce_timeout_sec": cfg.produce_timeout_sec,
        "publish_timeout_sec": cfg.publish_timeout_sec,
        "lookback_hours": cfg.lookback_hours,
        "extra_produce_args": cfg.extra_produce_args,
        "max_attempts_per_video": cfg.max_attempts_per_video,
        "split_threshold_sec": cfg.split_threshold_sec,
        "split_part_sec": cfg.split_part_sec,
        "game": cfg.game,
        "channels": [
            {
                "id": c.id,
                "name": c.name,
                "enabled": c.enabled,
                "platforms": c.platforms,
                "max_clips": c.max_clips,
            }
            for c in cfg.channels
        ],
    }
    Path(path).write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
