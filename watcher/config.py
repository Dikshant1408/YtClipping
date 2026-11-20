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
    lookback_hours: int = 0  # 0 -> only videos newer than last run (first run: newest only)
    extra_produce_args: List[str] = field(default_factory=list)
    max_attempts_per_video: int = 3  # retries for failed/pending before giving up

    def channel_platforms(self, ch: Channel) -> List[str]:
        return ch.platforms or [self.platform]

    def channel_max_clips(self, ch: Channel) -> int:
        return ch.max_clips or self.max_clips_per_video


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
    cfg.max_clips_per_video = int(data.get("max_clips_per_video", cfg.max_clips_per_video))
    cfg.poll_interval_minutes = int(data.get("poll_interval_minutes", cfg.poll_interval_minutes))
    cfg.max_videos_per_channel = int(data.get("max_videos_per_channel", cfg.max_videos_per_channel))
    cfg.autoclip_bin = data.get("autoclip_bin", cfg.autoclip_bin)
    cfg.produce_timeout_sec = int(data.get("produce_timeout_sec", cfg.produce_timeout_sec))
    cfg.lookback_hours = int(data.get("lookback_hours", cfg.lookback_hours))
    cfg.extra_produce_args = list(data.get("extra_produce_args", []))
    cfg.max_attempts_per_video = int(data.get("max_attempts_per_video", cfg.max_attempts_per_video))

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
        "lookback_hours": cfg.lookback_hours,
        "extra_produce_args": cfg.extra_produce_args,
        "max_attempts_per_video": cfg.max_attempts_per_video,
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