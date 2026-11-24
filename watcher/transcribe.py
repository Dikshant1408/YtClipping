"""Groq cloud Whisper transcription (whisper-large-v3).

Transcribes long/multilingual audio accurately via Groq's OpenAI-compatible
audio endpoint, chunking as needed, and returns an SRT file. This fixes the
poor local-Whisper transcription that breaks clip validation on Hindi/English
streams.

Enable: set OPENAI_BASE_URL to a Groq URL and API_OPENAI_API_KEY (as in
.env.example). `groq_available()` gates it.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import List, Optional

GROQ_TRANSCRIBE_URL = "https://api.groq.com/openai/v1/audio/transcriptions"
WHISPER_MODEL = "whisper-large-v3"
MAX_CHUNK_SEC = 600  # ~10 min per chunk; keeps each request small


def _ff() -> str:
    exe = shutil.which("ffmpeg")
    if not exe:
        raise RuntimeError("ffmpeg not found")
    return exe


def _fp() -> str:
    exe = shutil.which("ffprobe")
    if not exe:
        raise RuntimeError("ffprobe not found")
    return exe


def _groq_key() -> Optional[str]:
    base = os.environ.get("OPENAI_BASE_URL", "")
    if "groq" not in base.lower():
        return None
    return os.environ.get("API_OPENAI_API_KEY") or os.environ.get("GROQ_API_KEY")


def groq_available() -> bool:
    return bool(_groq_key())


def _duration(path: Path) -> float:
    out = subprocess.run(
        [_fp(), "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", str(path)],
        capture_output=True, text=True,
    )
    return float(out.stdout.strip() or 0.0)


def _extract_audio(src: Path, audio: Path) -> None:
    subprocess.run(
        [_ff(), "-y", "-i", str(src), "-vn", "-ac", "1", "-ar", "16000",
         "-b:a", "32k", str(audio)],
        check=True, capture_output=True,
    )


def _split(audio: Path, out_dir: Path) -> List[Path]:
    subprocess.run(
        [_ff(), "-y", "-i", str(audio), "-f", "segment", "-segment_time", str(MAX_CHUNK_SEC),
         "-c", "copy", str(out_dir / "chunk_%03d.mp3")],
        check=True, capture_output=True,
    )
    return sorted(out_dir.glob("chunk_*.mp3"))


def _transcribe_chunk(key: str, path: Path) -> List[dict]:
    import requests

    with open(path, "rb") as fh:
        resp = requests.post(
            GROQ_TRANSCRIBE_URL,
            headers={"Authorization": f"Bearer {key}"},
            files={"file": (path.name, fh, "audio/mpeg")},
            data={"model": WHISPER_MODEL, "response_format": "verbose_json",
                  "timestamp_granularities[]": "segment"},
            timeout=300,
        )
    resp.raise_for_status()
    data = resp.json()
    return data.get("segments") or []


def _fmt(t: float) -> str:
    h = int(t // 3600)
    m = int((t % 3600) // 60)
    s = int(t % 60)
    ms = int(round((t - int(t)) * 1000))
    if ms == 1000:
        s += 1
        ms = 0
    return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"


_DEV_RE = re.compile(r"[\u0900-\u097f]")


def _romanize(text: str) -> str:
    """Convert Devanagari words to natural Roman (Hinglish); keep Latin words as-is.

    Uses the HK scheme then cleans it (anusvara M->n, chandrabindu ~->n,
    nukta 2->z, lowercased). Words without Devanagari (e.g. "GTA") are untouched.
    """
    try:
        from indic_transliteration import sanscript
    except Exception:
        return text
    out = []
    for tok in text.split():
        if _DEV_RE.search(tok):
            s = sanscript.transliterate(tok, sanscript.DEVANAGARI, sanscript.HK)
            # Drop any Devanagari the scheme missed (avoids mojibake).
            s = re.sub(r"[\u0900-\u097f]", "", s)
            s = s.replace("M", "n").replace("~", "n").replace("2", "z").lower()
            out.append(s)
        else:
            out.append(tok)
    return " ".join(out)


def _to_srt(segments: List[dict]) -> str:
    out = []
    for i, seg in enumerate(segments, start=1):
        start = float(seg.get("start", 0.0))
        end = float(seg.get("end", start + 1.0))
        text = (seg.get("text") or "").strip()
        if not text:
            continue
        out.append(f"{i}\n{_fmt(start)} --> {_fmt(end)}\n{text}\n")
    return "\n".join(out)


def transcribe(src: str | Path, srt_out: Optional[str | Path] = None) -> str:
    """Transcribe a video/audio file via Groq Whisper and return the SRT path."""
    key = _groq_key()
    if not key:
        raise RuntimeError("Groq Whisper not configured (set OPENAI_BASE_URL to Groq + API_OPENAI_API_KEY)")

    src_path = Path(src).expanduser().resolve()
    if not src_path.is_file():
        raise FileNotFoundError(f"Source not found: {src_path}")

    srt_path = Path(srt_out) if srt_out else (Path(tempfile.gettempdir()) / (src_path.stem + ".groq.srt"))

    tmp = Path(tempfile.mkdtemp(prefix="ac-groq-whisper-"))
    roman = os.environ.get("TRANSCRIBE_SCRIPT", "roman").lower() != "devanagari"
    try:
        audio = tmp / "audio.mp3"
        _extract_audio(src_path, audio)
        chunks = _split(audio, tmp)

        all_segments: List[dict] = []
        offset = 0.0
        for chunk in chunks:
            segs = _transcribe_chunk(key, chunk)
            for seg in segs:
                seg["start"] = float(seg.get("start", 0.0)) + offset
                seg["end"] = float(seg.get("end", 0.0)) + offset
                if roman:
                    seg["text"] = _romanize(seg.get("text", ""))
                all_segments.append(seg)
            offset += _duration(chunk)

        srt_path.write_text(_to_srt(all_segments), encoding="utf-8")
        return str(srt_path)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def _js_runtimes() -> dict:
    """Detect an available JS runtime for yt-dlp (YouTube needs one)."""
    found = {}
    for name in ("deno", "node", "bun", "quickjs"):
        path = shutil.which(name)
        if path:
            found[name] = {"path": path}
    return found


def transcribe_url(url: str, srt_out: Optional[str | Path] = None) -> str:
    """Download audio for a URL (via yt-dlp library) and transcribe it -> SRT path."""
    import yt_dlp

    tmp = Path(tempfile.mkdtemp(prefix="ac-yt-audio-"))
    try:
        opts = {
            "format": "bestaudio/best",
            "outtmpl": str(tmp / "audio.%(ext)s"),
            "js_runtimes": _js_runtimes(),
            "quiet": True,
            "noplaylist": True,
            "noprogress": True,
        }
        with yt_dlp.YoutubeDL(opts) as ydl:
            ydl.download([url])
        files = [f for f in tmp.iterdir() if f.name.startswith("audio.")]
        if not files:
            raise RuntimeError("audio download produced no file")
        return transcribe(files[0], srt_out)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
