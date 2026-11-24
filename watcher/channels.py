"""YouTube channel monitoring.

Uses the public per-channel RSS feed
(https://www.youtube.com/feeds/videos.xml?channel_id=UCxxxx). No API key is
required. The feed lists the most recent uploads (typically up to 15).
"""

from __future__ import annotations

import re
import urllib.error
import urllib.request
from typing import Dict, List, Optional  # FIX #3: Optional was missing
from xml.etree import ElementTree as ET

FEED_URL = "https://www.youtube.com/feeds/videos.xml?channel_id={channel_id}"

NS = {
    "atom": "http://www.w3.org/2005/Atom",
    "yt": "http://www.youtube.com/xml/schemas/2015",
    "media": "http://search.yahoo.com/mrss/",
}

_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
)

_CHANNEL_ID_RES = [
    re.compile(r'"externalId":"(UC[\w-]{22})"'),
    re.compile(r'"browseId":"(UC[\w-]{22})"'),
    re.compile(r'"channelId":"(UC[\w-]{22})"'),
    re.compile(r'"(?:channelId|externalId)":"(UC[\w-]{22})"'),
]


def _http_get(url: str, timeout: int = 30) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": _UA})
    with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310
        return resp.read()


def resolve_channel_id(url_or_handle: str) -> str:
    """Resolve a channel URL / @handle / channel_id to a UCxxxx channel id."""
    s = url_or_handle.strip()
    if re.fullmatch(r"UC[\w-]{22}", s):
        return s

    if s.startswith("@"):
        url = f"https://www.youtube.com/{s}"
    elif s.startswith("http://") or s.startswith("https://"):
        url = s
    elif "youtube.com" in s or "youtu.be" in s:
        url = s if s.startswith("http") else "https://" + s
    else:
        url = f"https://www.youtube.com/@{s.lstrip('@')}"

    try:
        html = _http_get(url).decode("utf-8", errors="ignore")
        for pat in _CHANNEL_ID_RES:
            m = pat.search(html)
            if m:
                return m.group(1)
    except (urllib.error.URLError, TimeoutError, OSError):
        pass

    yt_id = _resolve_with_ytdlp(url)
    if yt_id:
        return yt_id

    raise RuntimeError(
        f"Could not resolve channel id for '{url_or_handle}'. "
        "Open the channel in a browser and copy its URL, or install yt-dlp "
        "(pip install yt-dlp) and retry."
    )


def _resolve_with_ytdlp(url: str) -> Optional[str]:
    import shutil
    import subprocess

    exe = shutil.which("yt-dlp")
    if not exe:
        return None
    try:
        out = subprocess.run(
            [exe, "--no-warnings", "--flat-playlist", "--print", "channel_id", url],
            capture_output=True,
            text=True,
            timeout=90,
        )
        for line in out.stdout.splitlines():
            line = line.strip()
            if re.fullmatch(r"UC[\w-]{22}", line):
                return line
    except (subprocess.SubprocessError, OSError):
        return None
    return None


def _ytdlp_exe() -> Optional[str]:
    import shutil
    return shutil.which("yt-dlp")


def video_uploader(url: str) -> Optional[str]:
    """Return the source video's uploader/channel name via yt-dlp (best effort)."""
    import subprocess

    exe = _ytdlp_exe()
    if not exe:
        return None
    try:
        out = subprocess.run(
            [exe, "--no-warnings", "--skip-download", "--print", "%(uploader)s", url],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=60,
        )
        for line in out.stdout.splitlines():
            line = line.strip()
            if line and line != "NA":
                return line
    except (subprocess.SubprocessError, OSError):
        return None
    return None


def video_duration(url: str) -> Optional[float]:
    """Return the source video duration in seconds via yt-dlp (best effort)."""
    import subprocess

    exe = _ytdlp_exe()
    if not exe:
        return None
    try:
        out = subprocess.run(
            [exe, "--no-warnings", "--skip-download", "--print", "%(duration)s", url],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=60,
        )
        for line in out.stdout.splitlines():
            line = line.strip()
            if line.replace(".", "", 1).isdigit():
                return float(line)
    except (subprocess.SubprocessError, OSError):
        return None
    return None


def download_section(url: str, start: float, end: float, out_path: str) -> None:
    """Download only a time range [start, end) seconds of a video via yt-dlp."""
    import yt_dlp

    from watcher.transcribe import _js_runtimes

    opts = {
        "format": "bestvideo[height<=1080]+bestaudio/best[height<=1080]",
        "outtmpl": out_path,
        "download_ranges": yt_dlp.utils.download_range_func(None, [(start, end)]),
        "force_keyframes_at_cuts": False,
        "merge_output_format": "mp4",
        "js_runtimes": _js_runtimes(),
        "quiet": True,
        "noplaylist": True,
        "noprogress": True,
    }
    with yt_dlp.YoutubeDL(opts) as ydl:
        ydl.download([url])


# FIX #2: fetch_channel_entries was unreachable dead code buried inside
# download_section. It is now a proper top-level function.
def fetch_channel_entries(channel_id: str) -> List[Dict[str, str]]:
    """Return recent uploads for a channel from its RSS feed.

    Each entry: {video_id, title, published (ISO 8601), url}.
    """
    raw = _http_get(FEED_URL.format(channel_id=channel_id))
    root = ET.fromstring(raw)

    entries: List[Dict[str, str]] = []
    for entry in root.findall("atom:entry", NS):
        video_id_el = entry.find("yt:videoId", NS)
        if video_id_el is None or not video_id_el.text:
            continue
        video_id = video_id_el.text.strip()

        title_el = entry.find("atom:title", NS)
        title = title_el.text.strip() if title_el is not None and title_el.text else ""

        published_el = entry.find("atom:published", NS)
        published = (
            published_el.text.strip()
            if published_el is not None and published_el.text
            else ""
        )

        entries.append(
            {
                "video_id": video_id,
                "title": title,
                "published": published,
                "url": f"https://www.youtube.com/watch?v={video_id}",
            }
        )
    return entries
