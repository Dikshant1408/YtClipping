"""Direct YouTube upload via the official YouTube Data API.

Bypasses Upload-Post entirely. Uses your own Google OAuth credentials
(client_secret.json) so there is no shared app rate limit.

First-time setup (one-time):
  1. Google Cloud Console -> create/select a project.
  2. Enable "YouTube Data API v3".
  3. OAuth consent screen -> External -> add your account as a Test user.
  4. Credentials -> Create OAuth client ID -> Desktop app -> download JSON,
     save it as client_secret.json in this folder.
  5. Run:  python autoclip_watch.py youtube-login   (opens browser once)
     -> creates token.json. Done.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, Dict, List, Optional

SCOPES = [
    "https://www.googleapis.com/auth/youtube.upload",
    "https://www.googleapis.com/auth/youtube",  # full read/update (metadata edits, channel info)
]
# Public API name for the quota-enabled endpoint.
API_SERVICE_NAME = "youtube"
API_VERSION = "v3"

VALID_PRIVACY = {"private", "unlisted", "public"}

_HERE = Path(__file__).resolve().parent.parent  # project root (parent of watcher/)
CLIENT_SECRET = _HERE / "client_secret.json"
TOKEN = _HERE / "token.json"


def _load_client_config() -> Dict[str, Any]:
    if not CLIENT_SECRET.exists():
        raise FileNotFoundError(
            f"Missing {CLIENT_SECRET.name}. Create an OAuth 'Desktop app' client in "
            "Google Cloud Console (YouTube Data API v3 enabled) and save the JSON "
            "here as client_secret.json. Then run: python autoclip_watch.py youtube-login"
        )
    with open(CLIENT_SECRET, "r", encoding="utf-8") as fh:
        data = json.load(fh)
    # InstalledAppFlow.from_client_config expects the FULL document, i.e.
    # {"installed": {...}} or {"web": {...}}. Return it unchanged.
    if "installed" not in data and "web" not in data:
        raise ValueError(
            "client_secret.json must be a Desktop app or Web client "
            "(top-level key 'installed' or 'web')."
        )
    return data


def get_credentials():
    """Return valid OAuth credentials, running the browser flow if needed.

    Forces a fresh consent when the stored token is missing a required scope
    (e.g. after broadening SCOPES), by checking the scopes actually recorded
    in token.json rather than the requested ones.
    """
    from google.auth.transport.requests import Request
    from google.oauth2.credentials import Credentials
    from google_auth_oauthlib.flow import InstalledAppFlow

    creds: Optional[Credentials] = None
    granted: set = set()
    if TOKEN.exists():
        creds = Credentials.from_authorized_user_file(str(TOKEN), SCOPES)
        try:
            with open(TOKEN, encoding="utf-8") as fh:
                granted = set(json.load(fh).get("scopes") or [])
        except (OSError, json.JSONDecodeError):
            granted = set(creds.scopes or [])

    need = set(SCOPES)

    if creds and creds.valid and need.issubset(granted):
        return creds

    if creds and creds.expired and creds.refresh_token and need.issubset(granted):
        try:
            creds.refresh(Request())
            _save_token(creds)
            return creds
        except Exception:
            pass  # fall through to a fresh login

    # Missing scopes (or no usable token): drop the old token and re-consent.
    try:
        if TOKEN.exists():
            TOKEN.unlink()
    except OSError:
        pass

    flow = InstalledAppFlow.from_client_config(_load_client_config(), SCOPES)
    creds = flow.run_local_server(
        port=0,
        authorization_prompt_message="Open this URL to connect your YouTube channel:",
        success_message="Connected. You can close this tab.",
        open_browser=True,
    )
    _save_token(creds)
    return creds


def _save_token(creds) -> None:
    TOKEN.write_text(creds.to_json(), encoding="utf-8")
    try:
        os.chmod(TOKEN, 0o600)
    except OSError:
        pass


def _youtube_client():
    from googleapiclient.discovery import build

    creds = get_credentials()
    return build(API_SERVICE_NAME, API_VERSION, credentials=creds)


_DEFAULT_TAGS = [
    "gta 6", "gta vi", "gta6", "gtav", "gta", "grand theft auto 6",
    "grand theft auto vi", "rockstar", "rockstar games", "vice city",
    "gaming", "gaming shorts", "shorts", "gameplay", "highlights",
    "funny moments", "gta 6 clips",
]

_GAME_HASHTAGS = "#gta6 #gtavi #gta6shorts #gaming #shorts #rockstargames #vicecity"


def _game_title(title: str, game: str) -> str:
    """Ensure the game name is in the title."""
    t = title.strip()
    if not game or game.lower() in t.lower():
        return t[:100]
    return f"{game}: {t}"[:100]


def _game_description(description: str, game: str, credit: Optional[str] = None) -> str:
    """Append game context + creator credit + hashtags to the description."""
    if not game or "#gta6" in description.lower():
        return description[:5000]
    block = f"{game} (Grand Theft Auto VI) gameplay highlights."
    if credit:
        block += f"\nCredit: {credit}"
    block += f"\n\n{_GAME_HASHTAGS}"
    return (description + "\n\n" + block)[:5000]


def _auto_tags(title: str, description: str, extra: Optional[List[str]] = None) -> List[str]:
    """Build a tag list from defaults + title/description words (<=500 chars)."""
    tags: List[str] = []
    words = [w.strip("#.,!?").lower() for w in (title + " " + description).split()]
    for t in _DEFAULT_TAGS + words + (extra or []):
        t = t.lower().strip()
        if t and len(t) > 2 and t not in tags:
            tags.append(t)
    # YouTube caps the total tag string at 500 characters.
    out: List[str] = []
    total = 0
    for t in tags:
        if total + len(t) + 1 > 500:
            break
        out.append(t)
        total += len(t) + 1
    return out


def upload_video(
    file_path: str | Path,
    *,
    title: str,
    description: str = "",
    tags: Optional[List[str]] = None,
    privacy: str = "private",
    is_short: bool = True,
    made_for_kids: bool = False,
    category_id: str = "20",   # 20 = Gaming
    language: str = "en",
    game: str = "GTA 6",
    credit: Optional[str] = None,
) -> Dict[str, Any]:
    """Upload one video to the authenticated YouTube channel.

    Fills in GTA 6-themed metadata: game-prefixed title, description with
    hashtags, auto tags, Gaming category, language, license, embeddability,
    and made-for-kids status. Returns a dict with the resulting id / url.
    """
    from googleapiclient.http import MediaFileUpload

    path = Path(file_path).expanduser().resolve()
    if not path.is_file():
        raise FileNotFoundError(f"Video not found: {path}")
    if privacy not in VALID_PRIVACY:
        raise ValueError(f"privacy must be one of {sorted(VALID_PRIVACY)}")

    if game:
        title = _game_title(title, game)
        description = _game_description(description, game, credit)

    if is_short and "#shorts" not in title.lower() and "#shorts" not in description.lower():
        description = (description + "\n\n#shorts").strip()

    if tags is None:
        tags = _auto_tags(title, description)

    body = {
        "snippet": {
            "title": title[:100],
            "description": description[:5000],
            "tags": tags,
            "categoryId": category_id,
            "defaultLanguage": language,
            "defaultAudioLanguage": language,
        },
        "status": {
            "privacyStatus": privacy,
            "selfDeclaredMadeForKids": made_for_kids,
            "license": "youtube",
            "embeddable": True,
            "publicStatsViewable": True,
        },
    }

    youtube = _youtube_client()
    media = MediaFileUpload(str(path), chunksize=-1, resumable=True, mimetype="video/*")

    request = youtube.videos().insert(
        part="snippet,status",
        body=body,
        media_body=media,
    )

    response = None
    while response is None:
        status, response = request.next_chunk()
        if status:
            pct = int(status.progress() * 100)
            print(f"  upload {path.name}: {pct}%", flush=True)

    video_id = response.get("id")
    url = f"https://www.youtube.com/watch?v={video_id}" if video_id else "(no id)"
    return {
        "id": video_id,
        "url": url,
        "title": title,
        "privacy": privacy,
        "is_short": is_short,
    }


def update_video(
    video_id: str,
    *,
    title: Optional[str] = None,
    description: Optional[str] = None,
    tags: Optional[List[str]] = None,
    category_id: Optional[str] = None,
    privacy: Optional[str] = None,
    language: Optional[str] = None,
) -> Dict[str, Any]:
    """Update metadata on an already-uploaded video (keeps unspecified fields)."""
    youtube = _youtube_client()
    existing = youtube.videos().list(part="snippet,status", id=video_id).execute()
    items = existing.get("items") or []
    if not items:
        raise ValueError(f"Video {video_id} not found")
    snippet = items[0]["snippet"]
    status = items[0]["status"]

    if title is not None:
        snippet["title"] = title[:100]
    if description is not None:
        snippet["description"] = description[:5000]
    if tags is not None:
        snippet["tags"] = tags
    if category_id is not None:
        snippet["categoryId"] = category_id
    if language is not None:
        snippet["defaultLanguage"] = language
        snippet["defaultAudioLanguage"] = language
    if privacy is not None:
        status["privacyStatus"] = privacy

    youtube.videos().update(part="snippet,status", body={"id": video_id, "snippet": snippet, "status": status}).execute()
    return {"id": video_id, "url": f"https://www.youtube.com/watch?v={video_id}", "title": snippet.get("title")}


def channel_name() -> Optional[str]:
    """Return the authenticated channel's display name (for verification)."""
    try:
        youtube = _youtube_client()
        resp = youtube.channels().list(part="snippet", mine=True).execute()
        items = resp.get("items") or []
        return items[0]["snippet"]["title"] if items else None
    except Exception:
        return None


def rendered_videos(produce_result: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Extract rendered video files from an AutoClip produce result.

    Returns a list of {path, title, description} for each completed variant.
    """
    out: List[Dict[str, Any]] = []
    variants = produce_result.get("outputs") or produce_result.get("variants") or []
    if not isinstance(variants, list):
        return out
    for v in variants:
        if not isinstance(v, dict):
            continue
        path = v.get("video_path") or v.get("path")
        if not path or not Path(path).is_file():
            continue
        post = v.get("post") if isinstance(v.get("post"), dict) else {}
        title = post.get("title") or v.get("title") or f"Short {v.get('id', '')}".strip()
        desc = post.get("description") or ""
        out.append({"path": path, "title": title, "description": desc})
    return out