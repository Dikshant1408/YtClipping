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
    import backend.services.publish_export as pe
    import backend.services.studio.packaging_render as pr

    _patch(pe.__file__, _SUB_REPLACEMENTS)
    _patch(pr.__file__, _TITLE_REPLACEMENTS)
    print("done. subtitles=top, title=bottom")


if __name__ == "__main__":
    apply()