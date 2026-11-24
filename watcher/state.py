"""Persistent state for processed videos.

State is a single JSON file mapping video_id -> record. It lets the watcher
know which uploads have already been clipped/published so they are not
processed twice.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class State:
    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.data: Dict[str, Any] = {"videos": {}}
        if self.path.exists():
            try:
                self.data = json.loads(self.path.read_text(encoding="utf-8"))
                self.data.setdefault("videos", {})
            except json.JSONDecodeError:
                # Corrupt state: back it up and start fresh rather than crash.
                backup = self.path.with_suffix(".corrupt.json")
                try:
                    backup.write_text(self.path.read_text(encoding="utf-8"), encoding="utf-8")
                except OSError:
                    pass
                self.data = {"videos": {}}

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.data, indent=2, ensure_ascii=False), encoding="utf-8")
        tmp.replace(self.path)

    def has(self, video_id: str) -> bool:
        return video_id in self.data["videos"]

    def get(self, video_id: str, default: Any = None) -> Any:
        return self.data["videos"].get(video_id, default)

    def put(self, video_id: str, record: Dict[str, Any]) -> None:
        record.setdefault("first_seen", _now())
        record["updated"] = _now()
        self.data["videos"][video_id] = record

    def update(self, video_id: str, **fields: Any) -> None:
        rec = self.data["videos"].setdefault(video_id, {})
        rec.setdefault("first_seen", _now())
        rec.update(fields)
        rec["updated"] = _now()

    def videos(self) -> List[Dict[str, Any]]:
        return list(self.data["videos"].values())

    def pending(self) -> List[Dict[str, Any]]:
        return [v for v in self.data["videos"].values() if v.get("status") == "pending"]

    def reset(self) -> None:
        self.data = {"videos": {}}
