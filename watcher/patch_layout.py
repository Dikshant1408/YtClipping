"""Patch AutoClip's render so subtitles sit at the TOP and the title card at the BOTTOM.

This edits the installed AutoClip package (site-packages). Re-run it after
reinstalling/updating AutoClip. Idempotent.

Layout applied:
  - Subtitle lines: Alignment 2 (bottom) -> 8 (top), smaller top margin.
  - Hook title:     \\pos(540,250) (top)  -> \\pos(540,1640) (bottom).
"""
from __future__ import annotations

import importlib
from pathlib import Path

_SUB_REPLACEMENTS = [
    ("Alignment=2", "Alignment=8"),
    ("MarginV=48", "MarginV=36"),
    ("MarginV=56", "MarginV=36"),
    ("MarginV=52", "MarginV=36"),
]

_TITLE_REPLACEMENTS = [
    (r"\\pos(540,250)", r"\\pos(540,1640)"),
    (r"\\move(540,300,540,250,0,260)", r"\\move(540,1600,540,1640,0,260)"),
]

# Shorts-friendly clip durations: the "long" tier defaults to 90s min / 120-360s
# target, which is too long for Shorts and rejects short clips.
_QUALITY_REPLACEMENTS = [
    ("min_clip_sec=90, target_clip_sec=(120, 360), max_clip_sec=480",
     "min_clip_sec=30, target_clip_sec=(30, 90), max_clip_sec=150"),
    ("min_clip_sec=45, target_clip_sec=(60, 180), max_clip_sec=300",
     "min_clip_sec=30, target_clip_sec=(30, 90), max_clip_sec=150"),
]


def _patch(path: str, replacements) -> None:
    p = Path(path)
    src = p.read_text(encoding="utf-8")
    changed = False
    for old, new in replacements:
        if new in src and old not in src:
            continue  # already patched
        if old in src:
            src = src.replace(old, new)
            changed = True
    if changed:
        p.write_text(src, encoding="utf-8")
        print(f"patched: {p}")
    else:
        print(f"no change: {p}")


def apply() -> None:
    import backend.pipeline.quality as q
    import backend.services.publish_export as pe
    import backend.services.studio.packaging_render as pr

    _patch(pe.__file__, _SUB_REPLACEMENTS)
    _patch(pr.__file__, _TITLE_REPLACEMENTS)
    _patch(q.__file__, _QUALITY_REPLACEMENTS)
    print("done. subtitles=top, title=bottom, clips=30-90s")


if __name__ == "__main__":
    apply()