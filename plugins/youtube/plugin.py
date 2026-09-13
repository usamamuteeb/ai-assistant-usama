"""Personal YouTube tools using OAuth and the existing local browser session.

Read operations use the official YouTube Data API, playback reuses the
persistent BrowserSession, and account-changing actions are confirmation-gated.
Local notes are additive and can be picked up by the knowledge-base watcher.
"""
from __future__ import annotations

import re
import os
import shutil
import threading
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
from googleapiclient.http import MediaFileUpload

from plugins.google_workspace.plugin import GoogleWorkspaceService
from src.config import Settings
from src.tools.base import Tool

PROJECT_ROOT = Path(__file__).resolve().parents[2]
YOUTUBE_SCOPE = "https://www.googleapis.com/auth/youtube.readonly"
YOUTUBE_MANAGE_SCOPE = "https://www.googleapis.com/auth/youtube"
YOUTUBE_FORCE_SSL_SCOPE = "https://www.googleapis.com/auth/youtube.force-ssl"
YOUTUBE_UPLOAD_SCOPE = "https://www.googleapis.com/auth/youtube.upload"
_VIDEO_ID_RE = re.compile(r"^[A-Za-z0-9_-]{6,}$")
_QUALITY_RE = re.compile(r"^(\d{3,4})p$", re.IGNORECASE)
_DOWNLOAD_EXECUTOR = ThreadPoolExecutor(max_workers=2, thread_name_prefix="youtube-download")
_DOWNLOAD_LOCK = threading.RLock()
_DOWNLOAD_JOBS: dict[str, dict[str, Any]] = {}


class _DownloadCancelled(Exception):
    """Internal signal used by the yt-dlp progress hook for cancellation."""


def _error(message: str) -> dict[str, str]:
    return {"error": message}


def _safe_name(value: str, fallback: str = "youtube") -> str:
    name = re.sub(r"[^A-Za-z0-9._ -]+", "", str(value or "")).strip(" .")
    return name[:100] or fallback


def _path_within(candidate: Path, root: Path) -> bool:
    try:
        return os.path.commonpath([
            os.path.normcase(os.path.abspath(str(candidate))),
            os.path.normcase(os.path.abspath(str(root))),
        ]) == os.path.normcase(os.path.abspath(str(root)))
    except ValueError:
        return False


def _allowed_local_file(root: Path, value: str) -> tuple[Path | None, str | None]:
    """Resolve an upload path under the configured workspace/user roots."""
    settings = _settings(root)
    raw = Path(str(value or "")).expanduser()
    candidate = (raw if raw.is_absolute() else settings.workspace_root() / raw).resolve(strict=False)
    filesystem = settings.filesystem_tool
    roots = [settings.workspace_root()]
    roots.extend(Path(item).expanduser().resolve() for item in filesystem.get("allowed_roots", []))
    if not any(_path_within(candidate, allowed) for allowed in roots):
        return None, f"Refused: upload path '{candidate}' is outside configured filesystem roots."
    if not candidate.is_file():
        return None, f"File '{candidate}' does not exist."
    return candidate, None


def _download_directory(root: Path, override: str = "") -> tuple[Path | None, str | None]:
    settings = _settings(root)
    configured = (settings.raw.get("youtube", {}) or {}).get("download_folder", "")
    configured_path = Path(str(override or configured)).expanduser() if (override or configured) else Path.home() / "Downloads" / "Neural YouTube"
    directory = (configured_path if configured_path.is_absolute() else settings.workspace_root() / configured_path).resolve(strict=False)
    filesystem = settings.filesystem_tool
    allowed = [settings.workspace_root()]
    allowed.extend(Path(item).expanduser().resolve() for item in filesystem.get("allowed_roots", []))
    if not any(_path_within(directory, root_path) for root_path in allowed):
        return None, f"Refused: YouTube download folder '{directory}' is outside configured filesystem roots."
    try:
        directory.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        return None, f"Could not create YouTube download folder '{directory}': {exc}"
    return directory, None


def _quality_cap(quality: str | None) -> tuple[int | None, str | None]:
    selected = str(quality or "720p").strip().lower()
    if selected == "low":
        return 360, "360p"
    if selected == "high":
        return 1080, "1080p"
    match = _QUALITY_RE.fullmatch(selected)
    if match:
        cap = int(match.group(1))
        if 144 <= cap <= 2160:
            return cap, f"{cap}p"
    return None, None


def _ffmpeg_bin_dir() -> Path | None:
    """Find FFmpeg even when Neural was started before the user PATH changed."""
    ffmpeg = shutil.which("ffmpeg")
    ffprobe = shutil.which("ffprobe")
    if ffmpeg and ffprobe:
        ffmpeg_path = Path(ffmpeg).resolve()
        ffprobe_path = Path(ffprobe).resolve()
        if ffmpeg_path.parent == ffprobe_path.parent:
            return ffmpeg_path.parent

    # Windows GUI apps and already-running terminals keep the environment they
    # inherited at launch.  The installer/setup flow may therefore place the
    # binaries on the user's PATH while this process still cannot see them.
    # Check the standard local setup location directly as a reliable fallback.
    candidates = (
        Path.home() / "Downloads" / "ffmpeg-essentials",
        Path.home() / "Downloads" / "ffmpeg",
        Path("C:/ffmpeg"),
    )
    for root in candidates:
        if not root.is_dir():
            continue
        for ffmpeg_path in root.rglob("ffmpeg.exe"):
            ffprobe_path = ffmpeg_path.with_name("ffprobe.exe")
            if ffprobe_path.is_file():
                return ffmpeg_path.parent
    return None


def _job_snapshot(job: dict[str, Any]) -> dict[str, Any]:
    with _DOWNLOAD_LOCK:
        result = {key: value for key, value in job.items() if key != "cancel_requested"}
    return result


def _settings(root: Path) -> Settings:
    import yaml

    try:
        with (root / "config.yaml").open("r", encoding="utf-8") as handle:
            raw = yaml.safe_load(handle) or {}
        return Settings(raw=raw if isinstance(raw, dict) else {}, root=root)
    except Exception as exc:
        raise RuntimeError(f"Could not load YouTube configuration: {exc}") from exc


def _video_id(value: str) -> str:
    candidate = str(value or "").strip()
    if _VIDEO_ID_RE.fullmatch(candidate):
        return candidate
    parsed = urlparse(candidate)
    if parsed.hostname in {"youtu.be", "www.youtu.be"}:
        return parsed.path.strip("/").split("/")[0]
    if parsed.hostname and parsed.hostname.endswith("youtube.com"):
        query_id = parse_qs(parsed.query).get("v", [""])[0]
        if query_id:
            return query_id
        parts = [part for part in parsed.path.split("/") if part]
        if len(parts) >= 2 and parts[0] in {"shorts", "embed", "live"}:
            return parts[1]
    return candidate


def _duration_seconds(value: str) -> int | None:
    match = re.fullmatch(r"PT(?:(\d+)H)?(?:(\d+)M)?(?:(\d+)S)?", value or "")
    if not match:
        return None
    hours, minutes, seconds = (int(part or 0) for part in match.groups())
    return hours * 3600 + minutes * 60 + seconds


def _snippet(item: dict[str, Any]) -> dict[str, Any]:
    snippet = item.get("snippet") or {}
    identifier = item.get("id")
    if isinstance(identifier, dict):
        identifier = identifier.get("videoId") or identifier.get("channelId") or identifier.get("playlistId")
    return {
        "id": identifier,
        "title": snippet.get("title", ""),
        "description": snippet.get("description", ""),
        "channel_id": snippet.get("channelId"),
        "channel_title": snippet.get("channelTitle"),
        "published_at": snippet.get("publishedAt"),
        "thumbnail": (snippet.get("thumbnails") or {}).get("high", {}).get("url")
        or (snippet.get("thumbnails") or {}).get("default", {}).get("url"),
    }


class YouTubeService:
    def __init__(self, root: Path = PROJECT_ROOT):
        self.root = root
        self.google = GoogleWorkspaceService(root)

    def credentials(self, required_scopes: list[str] | None = None):
        credentials = self.google.get_credentials()
        granted = set(credentials.scopes or [])
        required = [YOUTUBE_SCOPE, *(required_scopes or [])]
        if all(scope in granted for scope in required):
            return credentials

        # Existing Gmail/Calendar or read-only YouTube tokens may predate a
        # write feature. Re-consent once with the combined configured scopes;
        # the encrypted shared token cache keeps later calls login-free.
        scopes = list(self.google.config.scopes)
        for scope in required:
            if scope not in scopes:
                scopes.append(scope)
        if not self.google.config.credentials_path.exists():
            raise FileNotFoundError(
                f"Google OAuth credentials not found at {self.google.config.credentials_path}."
            )
        flow = InstalledAppFlow.from_client_secrets_file(
            str(self.google.config.credentials_path), scopes
        )
        credentials = flow.run_local_server(port=0)
        self.google._save_cached_token(credentials)
        return credentials

    def api(self, required_scopes: list[str] | None = None):
        return build("youtube", "v3", credentials=self.credentials(required_scopes))


class _YouTubeTool(Tool):
    def __init__(self, root: Path = PROJECT_ROOT):
        self.root = root
        self.service = YouTubeService(root)

    def _call(self, callback, required_scopes: list[str] | None = None) -> Any:
        try:
            api = self.service.api() if required_scopes is None else self.service.api(required_scopes)
            return callback(api)
        except Exception as exc:
            return _error(f"YouTube action failed: {exc}")


class YouTubeStatusTool(_YouTubeTool):
    name = "youtube_status"
    description = "Check the connected YouTube account and whether OAuth is ready. Read-only."
    input_schema = {"type": "object", "properties": {}, "required": []}

    def run(self) -> Any:
        def action(api):
            response = api.channels().list(part="snippet,statistics,contentDetails", mine=True).execute()
            items = response.get("items", [])
            if not items:
                return {"status": "connected", "channel": None, "message": "OAuth is valid, but no YouTube channel was returned."}
            channel = items[0]
            return {
                "status": "connected",
                "channel": {
                    "id": channel.get("id"),
                    "title": (channel.get("snippet") or {}).get("title"),
                    "description": (channel.get("snippet") or {}).get("description"),
                    "statistics": channel.get("statistics", {}),
                    "uploads_playlist_id": (channel.get("contentDetails") or {}).get("relatedPlaylists", {}).get("uploads"),
                },
            }

        return self._call(action)


class YouTubeSearchTool(_YouTubeTool):
    name = "youtube_search"
    description = "Search public YouTube videos, channels, or playlists with optional date and duration filters. Read-only."
    input_schema = {
        "type": "object",
        "properties": {
            "query": {"type": "string"},
            "result_type": {"type": "string", "enum": ["video", "channel", "playlist"], "default": "video"},
            "max_results": {"type": "integer", "minimum": 1, "maximum": 50, "default": 10},
            "order": {"type": "string", "enum": ["relevance", "date", "viewCount", "rating"], "default": "relevance"},
            "published_after": {"type": "string", "description": "Optional RFC3339 timestamp."},
            "published_before": {"type": "string", "description": "Optional RFC3339 timestamp."},
        },
        "required": ["query"],
    }

    def run(
        self,
        query: str,
        result_type: str = "video",
        max_results: int = 10,
        order: str = "relevance",
        published_after: str = "",
        published_before: str = "",
    ) -> Any:
        def action(api):
            params: dict[str, Any] = {
                "part": "snippet",
                "q": query,
                "type": result_type,
                "maxResults": max(1, min(int(max_results), 50)),
                "order": order,
            }
            if published_after:
                params["publishedAfter"] = published_after
            if published_before:
                params["publishedBefore"] = published_before
            response = api.search().list(**params).execute()
            return {
                "query": query,
                "result_type": result_type,
                "results": [_snippet(item) for item in response.get("items", [])],
                "next_page_token": response.get("nextPageToken"),
            }

        return self._call(action)


class YouTubeGetVideoTool(_YouTubeTool):
    name = "youtube_get_video"
    description = "Return detailed metadata and statistics for a YouTube video URL or ID. Read-only."
    input_schema = {
        "type": "object",
        "properties": {"video": {"type": "string"}},
        "required": ["video"],
    }

    def run(self, video: str) -> Any:
        identifier = _video_id(video)
        if not _VIDEO_ID_RE.fullmatch(identifier):
            return _error("A valid YouTube video URL or video ID is required.")

        def action(api):
            response = api.videos().list(part="snippet,contentDetails,statistics,status", id=identifier).execute()
            items = response.get("items", [])
            if not items:
                return _error(f"YouTube video '{identifier}' was not found or is not accessible.")
            item = items[0]
            result = _snippet(item)
            details = item.get("contentDetails") or {}
            result.update({
                "url": f"https://www.youtube.com/watch?v={identifier}",
                "duration": details.get("duration"),
                "duration_seconds": _duration_seconds(details.get("duration", "")),
                "definition": details.get("definition"),
                "caption_available": details.get("caption") == "true",
                "statistics": item.get("statistics", {}),
                "status": item.get("status", {}),
                "tags": (item.get("snippet") or {}).get("tags", []),
            })
            return result

        return self._call(action)


class YouTubeGetChannelTool(_YouTubeTool):
    name = "youtube_get_channel"
    description = "Return channel metadata, statistics, and its uploads playlist. Read-only."
    input_schema = {
        "type": "object",
        "properties": {"channel_id": {"type": "string", "description": "Channel ID, @handle, or 'mine'."}},
        "required": ["channel_id"],
    }

    def run(self, channel_id: str) -> Any:
        def action(api):
            params: dict[str, Any] = {"part": "snippet,statistics,contentDetails"}
            if channel_id.casefold() == "mine":
                params["mine"] = True
            elif channel_id.startswith("@"):
                params["forHandle"] = channel_id
            else:
                params["id"] = channel_id
            response = api.channels().list(**params).execute()
            items = response.get("items", [])
            if not items:
                return _error(f"YouTube channel '{channel_id}' was not found or is not accessible.")
            item = items[0]
            result = _snippet(item)
            result["id"] = item.get("id")
            result["statistics"] = item.get("statistics", {})
            result["uploads_playlist_id"] = (item.get("contentDetails") or {}).get("relatedPlaylists", {}).get("uploads")
            return result

        return self._call(action)


class YouTubeChannelOverviewTool(_YouTubeTool):
    name = "youtube_channel_overview"
    description = "Return a YouTube channel's details and its latest uploaded videos in one lookup. Use this for questions asking about a named channel and recent/latest videos. Read-only."
    input_schema = {
        "type": "object",
        "properties": {"channel": {"type": "string", "description": "Channel ID, @handle, or channel name."}, "recent_count": {"type": "integer", "minimum": 1, "maximum": 10, "default": 5}},
        "required": ["channel"],
    }

    def run(self, channel: str, recent_count: int = 5) -> Any:
        channel = str(channel or "").strip()
        if not channel:
            return _error("Provide a YouTube channel ID, handle, or name.")

        def action(api):
            channel_id = channel
            if channel.casefold() == "mine":
                response = api.channels().list(part="snippet,statistics,contentDetails", mine=True).execute()
            elif channel.startswith("@"):
                response = api.channels().list(part="snippet,statistics,contentDetails", forHandle=channel).execute()
            elif _VIDEO_ID_RE.fullmatch(channel):
                response = api.channels().list(part="snippet,statistics,contentDetails", id=channel).execute()
            else:
                search = api.search().list(part="snippet", q=channel, type="channel", maxResults=5).execute()
                search_items = search.get("items", [])
                if not search_items:
                    return _error(f"No YouTube channel was found for '{channel}'.")
                channel_id = (search_items[0].get("id") or {}).get("channelId")
                response = api.channels().list(part="snippet,statistics,contentDetails", id=channel_id).execute()
            items = response.get("items", [])
            if not items:
                return _error(f"YouTube channel '{channel}' was not found or is not accessible.")
            item = items[0]
            snippet = item.get("snippet") or {}
            details = item.get("contentDetails") or {}
            channel_info = {
                "id": item.get("id"),
                "title": snippet.get("title", ""),
                "description": snippet.get("description", ""),
                "published_at": snippet.get("publishedAt"),
                "country": snippet.get("country"),
                "statistics": item.get("statistics", {}),
                "uploads_playlist_id": (details.get("relatedPlaylists") or {}).get("uploads"),
            }
            uploads_id = channel_info["uploads_playlist_id"]
            if not uploads_id:
                return {"channel": channel_info, "recent_videos": []}
            playlist = api.playlistItems().list(part="snippet,contentDetails", playlistId=uploads_id, maxResults=max(1, min(int(recent_count), 10))).execute()
            recent = []
            video_ids = []
            for playlist_item in playlist.get("items", []):
                video_id = (playlist_item.get("contentDetails") or {}).get("videoId")
                if video_id:
                    video_ids.append(video_id)
            if video_ids:
                videos = api.videos().list(part="snippet,contentDetails,statistics", id=",".join(video_ids)).execute()
                by_id = {video.get("id"): video for video in videos.get("items", [])}
                for video_id in video_ids:
                    video = by_id.get(video_id)
                    if not video:
                        continue
                    normalized = _snippet(video)
                    normalized.update({
                        "url": f"https://www.youtube.com/watch?v={video_id}",
                        "duration": (video.get("contentDetails") or {}).get("duration"),
                        "statistics": video.get("statistics", {}),
                    })
                    recent.append(normalized)
            return {"channel": channel_info, "recent_videos": recent}

        return self._call(action)


class YouTubeListPlaylistVideosTool(_YouTubeTool):
    name = "youtube_list_playlist_videos"
    description = "List videos in a public or authorized YouTube playlist. Read-only."
    input_schema = {
        "type": "object",
        "properties": {
            "playlist_id": {"type": "string"},
            "max_results": {"type": "integer", "minimum": 1, "maximum": 50, "default": 25},
        },
        "required": ["playlist_id"],
    }

    def run(self, playlist_id: str, max_results: int = 25) -> Any:
        def action(api):
            response = api.playlistItems().list(
                part="snippet,contentDetails",
                playlistId=playlist_id,
                maxResults=max(1, min(int(max_results), 50)),
            ).execute()
            videos = []
            for item in response.get("items", []):
                row = _snippet(item)
                row["id"] = (item.get("contentDetails") or {}).get("videoId") or row.get("id")
                videos.append(row)
            return {"playlist_id": playlist_id, "videos": videos, "next_page_token": response.get("nextPageToken")}

        return self._call(action)


class YouTubeListSubscriptionsTool(_YouTubeTool):
    name = "youtube_list_subscriptions"
    description = "List channels followed by the authenticated YouTube account. Read-only."
    input_schema = {
        "type": "object",
        "properties": {"max_results": {"type": "integer", "minimum": 1, "maximum": 50, "default": 25}},
        "required": [],
    }

    def run(self, max_results: int = 25) -> Any:
        def action(api):
            response = api.subscriptions().list(
                part="snippet,contentDetails",
                mine=True,
                maxResults=max(1, min(int(max_results), 50)),
            ).execute()
            return {
                "subscriptions": [_snippet(item) for item in response.get("items", [])],
                "next_page_token": response.get("nextPageToken"),
            }

        return self._call(action)


class YouTubeListMyPlaylistsTool(_YouTubeTool):
    name = "youtube_list_my_playlists"
    description = "List playlists owned by the connected YouTube account, including private playlists. Read-only."
    input_schema = {
        "type": "object",
        "properties": {"max_results": {"type": "integer", "minimum": 1, "maximum": 50, "default": 25}},
        "required": [],
    }

    def run(self, max_results: int = 25) -> Any:
        def action(api):
            response = api.playlists().list(
                part="snippet,status,contentDetails",
                mine=True,
                maxResults=max(1, min(int(max_results), 50)),
            ).execute()
            playlists = []
            for item in response.get("items", []):
                snippet = item.get("snippet") or {}
                playlists.append({
                    "id": item.get("id"),
                    "title": snippet.get("title", ""),
                    "description": snippet.get("description", ""),
                    "privacy_status": (item.get("status") or {}).get("privacyStatus"),
                    "item_count": (item.get("contentDetails") or {}).get("itemCount"),
                    "published_at": snippet.get("publishedAt"),
                })
            return {"playlists": playlists, "next_page_token": response.get("nextPageToken")}

        return self._call(action)


class YouTubeCreatePlaylistTool(_YouTubeTool):
    name = "youtube_create_playlist"
    description = "Create a personal YouTube playlist. Requires confirmation and reports its new ID and URL."
    input_schema = {
        "type": "object",
        "properties": {
            "title": {"type": "string"},
            "description": {"type": "string", "default": ""},
            "privacy_status": {"type": "string", "enum": ["private", "unlisted", "public"], "default": "private"},
        },
        "required": ["title"],
    }

    def __init__(self, confirm_fn=None, root: Path = PROJECT_ROOT):
        super().__init__(root)
        self.confirm_fn = confirm_fn or (lambda _: False)

    def run(self, title: str, description: str = "", privacy_status: str = "private") -> Any:
        title = str(title or "").strip()
        if not title:
            return _error("Playlist title cannot be empty.")
        if privacy_status not in {"private", "unlisted", "public"}:
            return _error("privacy_status must be private, unlisted, or public.")
        if not self.confirm_fn(f"Create YouTube playlist '{title}' with {privacy_status} visibility?"):
            return _error("Playlist not created: confirmation denied or not provided.")

        def action(api):
            item = api.playlists().insert(
                part="snippet,status",
                body={
                    "snippet": {"title": title, "description": description},
                    "status": {"privacyStatus": privacy_status},
                },
            ).execute()
            return {
                "status": "created",
                "playlist_id": item.get("id"),
                "title": (item.get("snippet") or {}).get("title", title),
                "privacy_status": (item.get("status") or {}).get("privacyStatus", privacy_status),
                "url": f"https://www.youtube.com/playlist?list={item.get('id')}",
            }

        return self._call(action, [YOUTUBE_MANAGE_SCOPE])


class YouTubeAddToPlaylistTool(_YouTubeTool):
    name = "youtube_add_to_playlist"
    description = "Add a video to a YouTube playlist. Requires confirmation."
    input_schema = {
        "type": "object",
        "properties": {"playlist_id": {"type": "string"}, "video": {"type": "string", "description": "Video URL or ID."}},
        "required": ["playlist_id", "video"],
    }

    def __init__(self, confirm_fn=None, root: Path = PROJECT_ROOT):
        super().__init__(root)
        self.confirm_fn = confirm_fn or (lambda _: False)

    def run(self, playlist_id: str, video: str) -> Any:
        video_id = _video_id(video)
        if not _VIDEO_ID_RE.fullmatch(video_id):
            return _error("A valid YouTube video URL or video ID is required.")
        if not self.confirm_fn(f"Add YouTube video '{video_id}' to playlist '{playlist_id}'?"):
            return _error("Video not added to playlist: confirmation denied or not provided.")

        def action(api):
            item = api.playlistItems().insert(
                part="snippet",
                body={"snippet": {"playlistId": playlist_id, "resourceId": {"kind": "youtube#video", "videoId": video_id}}},
            ).execute()
            return {"status": "added", "playlist_id": playlist_id, "video_id": video_id, "playlist_item_id": item.get("id")}

        return self._call(action, [YOUTUBE_MANAGE_SCOPE])


class YouTubeRemoveFromPlaylistTool(_YouTubeTool):
    name = "youtube_remove_from_playlist"
    description = "Remove a playlist item from YouTube. Supply the playlist item ID returned by list_playlist_videos. Requires confirmation."
    input_schema = {"type": "object", "properties": {"playlist_item_id": {"type": "string"}}, "required": ["playlist_item_id"]}

    def __init__(self, confirm_fn=None, root: Path = PROJECT_ROOT):
        super().__init__(root)
        self.confirm_fn = confirm_fn or (lambda _: False)

    def run(self, playlist_item_id: str) -> Any:
        if not self.confirm_fn(f"Remove playlist item '{playlist_item_id}' from YouTube?"):
            return _error("Video not removed from playlist: confirmation denied or not provided.")

        def action(api):
            api.playlistItems().delete(id=playlist_item_id).execute()
            return {"status": "removed", "playlist_item_id": playlist_item_id}

        return self._call(action, [YOUTUBE_MANAGE_SCOPE])


class YouTubeUpdatePlaylistTool(_YouTubeTool):
    name = "youtube_update_playlist"
    description = "Update provided fields on a personal YouTube playlist. Requires confirmation."
    input_schema = {
        "type": "object",
        "properties": {
            "playlist_id": {"type": "string"},
            "title": {"type": "string"},
            "description": {"type": "string"},
            "privacy_status": {"type": "string", "enum": ["private", "unlisted", "public"]},
        },
        "required": ["playlist_id"],
    }

    def __init__(self, confirm_fn=None, root: Path = PROJECT_ROOT):
        super().__init__(root)
        self.confirm_fn = confirm_fn or (lambda _: False)

    def run(self, playlist_id: str, title: str | None = None, description: str | None = None, privacy_status: str | None = None) -> Any:
        if title is None and description is None and privacy_status is None:
            return _error("Provide at least one playlist field to update.")
        if privacy_status is not None and privacy_status not in {"private", "unlisted", "public"}:
            return _error("privacy_status must be private, unlisted, or public.")

        def action(api):
            current_response = api.playlists().list(part="snippet,status", id=playlist_id).execute()
            items = current_response.get("items", [])
            if not items:
                return _error(f"YouTube playlist '{playlist_id}' was not found.")
            current = items[0]
            current_snippet = current.get("snippet") or {}
            current_status = current.get("status") or {}
            display_title = str(title if title is not None else current_snippet.get("title", playlist_id))
            if not self.confirm_fn(f"Update YouTube playlist '{display_title}' ({playlist_id})?"):
                return _error("Playlist not updated: confirmation denied or not provided.")
            item = api.playlists().update(
                part="snippet,status",
                body={
                    "id": playlist_id,
                    "snippet": {
                        "title": title if title is not None else current_snippet.get("title", ""),
                        "description": description if description is not None else current_snippet.get("description", ""),
                    },
                    "status": {"privacyStatus": privacy_status if privacy_status is not None else current_status.get("privacyStatus", "private")},
                },
            ).execute()
            return {"status": "updated", "playlist_id": playlist_id, "title": (item.get("snippet") or {}).get("title")}

        return self._call(action, [YOUTUBE_MANAGE_SCOPE])


class YouTubeDeletePlaylistTool(_YouTubeTool):
    name = "youtube_delete_playlist"
    description = "Permanently delete a YouTube playlist after confirmation."
    input_schema = {"type": "object", "properties": {"playlist_id": {"type": "string"}}, "required": ["playlist_id"]}

    def __init__(self, confirm_fn=None, root: Path = PROJECT_ROOT):
        super().__init__(root)
        self.confirm_fn = confirm_fn or (lambda _: False)

    def run(self, playlist_id: str) -> Any:
        def action(api):
            response = api.playlists().list(part="snippet", id=playlist_id).execute()
            items = response.get("items", [])
            if not items:
                return _error(f"YouTube playlist '{playlist_id}' was not found.")
            title = (items[0].get("snippet") or {}).get("title", playlist_id)
            if not self.confirm_fn(f"Permanently delete YouTube playlist '{title}' ({playlist_id})?"):
                return _error("Playlist not deleted: confirmation denied or not provided.")
            api.playlists().delete(id=playlist_id).execute()
            return {"status": "deleted", "playlist_id": playlist_id, "title": title}

        return self._call(action, [YOUTUBE_MANAGE_SCOPE])


class YouTubeSubscribeChannelTool(_YouTubeTool):
    name = "youtube_subscribe_channel"
    description = "Subscribe the connected YouTube account to a channel. Requires confirmation."
    input_schema = {"type": "object", "properties": {"channel_id": {"type": "string"}}, "required": ["channel_id"]}

    def __init__(self, confirm_fn=None, root: Path = PROJECT_ROOT):
        super().__init__(root)
        self.confirm_fn = confirm_fn or (lambda _: False)

    def run(self, channel_id: str) -> Any:
        if not self.confirm_fn(f"Subscribe this YouTube account to channel '{channel_id}'?"):
            return _error("Subscription not created: confirmation denied or not provided.")

        def action(api):
            item = api.subscriptions().insert(
                part="snippet",
                body={"snippet": {"resourceId": {"kind": "youtube#channel", "channelId": channel_id}}},
            ).execute()
            return {"status": "subscribed", "channel_id": channel_id, "subscription_id": item.get("id")}

        return self._call(action, [YOUTUBE_MANAGE_SCOPE])


class YouTubeUnsubscribeChannelTool(_YouTubeTool):
    name = "youtube_unsubscribe_channel"
    description = "Unsubscribe using the subscription ID returned by youtube_list_subscriptions. Requires confirmation."
    input_schema = {"type": "object", "properties": {"subscription_id": {"type": "string"}}, "required": ["subscription_id"]}

    def __init__(self, confirm_fn=None, root: Path = PROJECT_ROOT):
        super().__init__(root)
        self.confirm_fn = confirm_fn or (lambda _: False)

    def run(self, subscription_id: str) -> Any:
        if not self.confirm_fn(f"Unsubscribe this YouTube account using subscription '{subscription_id}'?"):
            return _error("Subscription not removed: confirmation denied or not provided.")

        def action(api):
            api.subscriptions().delete(id=subscription_id).execute()
            return {"status": "unsubscribed", "subscription_id": subscription_id}

        return self._call(action, [YOUTUBE_MANAGE_SCOPE])


class YouTubeRateVideoTool(_YouTubeTool):
    name = "youtube_rate_video"
    description = "Like, dislike, or clear the connected account's rating for a YouTube video. Requires confirmation."
    input_schema = {
        "type": "object",
        "properties": {"video": {"type": "string"}, "rating": {"type": "string", "enum": ["like", "dislike", "none"]}},
        "required": ["video", "rating"],
    }

    def __init__(self, confirm_fn=None, root: Path = PROJECT_ROOT):
        super().__init__(root)
        self.confirm_fn = confirm_fn or (lambda _: False)

    def run(self, video: str, rating: str) -> Any:
        video_id = _video_id(video)
        if not _VIDEO_ID_RE.fullmatch(video_id):
            return _error("A valid YouTube video URL or video ID is required.")
        if rating not in {"like", "dislike", "none"}:
            return _error("rating must be like, dislike, or none.")
        if not self.confirm_fn(f"Set this account's YouTube rating for '{video_id}' to '{rating}'?"):
            return _error("Video rating not changed: confirmation denied or not provided.")

        def action(api):
            api.videos().rate(id=video_id, rating=rating).execute()
            return {"status": "rated", "video_id": video_id, "rating": rating}

        return self._call(action, [YOUTUBE_FORCE_SSL_SCOPE])


class YouTubeGetVideoRatingTool(_YouTubeTool):
    name = "youtube_get_video_rating"
    description = "Return the connected account's current rating for a YouTube video. Read-only."
    input_schema = {"type": "object", "properties": {"video": {"type": "string"}}, "required": ["video"]}

    def run(self, video: str) -> Any:
        video_id = _video_id(video)
        if not _VIDEO_ID_RE.fullmatch(video_id):
            return _error("A valid YouTube video URL or video ID is required.")

        def action(api):
            response = api.videos().getRating(id=video_id).execute()
            ratings = response.get("items", [])
            return {"video_id": video_id, "rating": (ratings[0].get("rating") if ratings else "none")}

        return self._call(action)


class YouTubeOpenInBrowserTool(Tool):
    name = "youtube_open_in_browser"
    description = "Open a YouTube video or page in the existing persistent BrowserSession. Read-only."
    input_schema = {"type": "object", "properties": {"url": {"type": "string"}}, "required": ["url"]}

    def __init__(self, root: Path = PROJECT_ROOT):
        self.root = root

    def run(self, url: str) -> Any:
        try:
            from plugins.browser.plugin import BrowserRecoveryError, BrowserSession

            session = BrowserSession.get_instance(self.root)

            def action(_page):
                tab_id, page = session.page_for_site("youtube.com")
                page.goto(url, wait_until="domcontentloaded", timeout=30000)
                return {"status": "opened", "tab_id": tab_id, "url": page.url, "title": page.title()}

            return session.run_with_recovery(action)
        except BrowserRecoveryError as exc:
            return _error(str(exc))
        except Exception as exc:
            return _error(f"Could not open YouTube in the browser: {exc}")


class YouTubePlayTool(Tool):
    name = "youtube_play"
    description = (
        "Find a YouTube video for a natural-language request, open it in the "
        "persistent browser session, and attempt to start playback. Requires "
        "confirmation showing the selected video and destination."
    )
    input_schema = {
        "type": "object",
        "properties": {
            "query": {"type": "string", "description": "Song, artist, video title, or YouTube URL/ID."},
        },
        "required": ["query"],
    }

    def __init__(self, confirm_fn=None, root: Path = PROJECT_ROOT):
        self.root = root
        self.confirm_fn = confirm_fn or (lambda _: False)

    def run(self, query: str) -> Any:
        query = str(query or "").strip()
        if not query:
            return _error("Provide a song, artist, video title, or YouTube URL.")

        # A direct URL/ID avoids an unnecessary search request. Natural-language
        # requests use the Data API and are confirmed only after a result is chosen.
        identifier = _video_id(query)
        is_direct_reference = bool(_VIDEO_ID_RE.fullmatch(query)) or bool(urlparse(query).scheme)
        if not is_direct_reference or not _VIDEO_ID_RE.fullmatch(identifier):
            search_result = YouTubeSearchTool(self.root).run(query, result_type="video", max_results=5)
            if not isinstance(search_result, dict) or search_result.get("error"):
                return search_result
            results = search_result.get("results") or []
            if not results or not results[0].get("id"):
                return _error(f"No YouTube video was found for '{query}'.")
            selected = results[0]
            identifier = str(selected.get("id"))
            title = str(selected.get("title") or query)
            channel = str(selected.get("channel_title") or "Unknown channel")
        else:
            title = query
            channel = "Unknown channel"

        url = f"https://www.youtube.com/watch?v={identifier}&autoplay=1"
        prompt = f"Play YouTube video '{title}' by {channel} at {url}?"
        if not self.confirm_fn(prompt):
            return _error("Playback not started: confirmation denied or not provided.")

        try:
            from plugins.browser.plugin import BrowserRecoveryError, BrowserSession

            session = BrowserSession.get_instance(self.root)

            def action(_active_page: Any) -> Any:
                tab_id, page = session.page_for_site("youtube.com")
                page.goto(url, wait_until="domcontentloaded", timeout=30000)
                playback = "opened"
                # YouTube may honor autoplay, but a visible player can still
                # require a click because of the browser's media autoplay policy.
                for selector in ("button.ytp-large-play-button", "button.ytp-play-button"):
                    button = page.locator(selector)
                    if button.count() and button.first.is_visible():
                        button.first.click(timeout=10000)
                        playback = "playing"
                        break
                return {
                    "status": playback,
                    "title": title,
                    "channel": channel,
                    "url": f"https://www.youtube.com/watch?v={identifier}",
                    "tab_id": tab_id,
                    "message": "YouTube opened in the persistent browser session; playback was started when the player allowed it.",
                }

            return session.run_with_recovery(action)
        except BrowserRecoveryError as exc:
            return _error(str(exc))
        except Exception as exc:
            return _error(f"Could not play YouTube video '{title}': {exc}")


class _YouTubePlaybackControlTool(Tool):
    def __init__(self, root: Path = PROJECT_ROOT):
        self.root = root

    def _control(self, command: str, value: float | None = None) -> Any:
        try:
            from plugins.browser.plugin import BrowserRecoveryError, BrowserSession

            session = BrowserSession.get_instance(self.root)

            def action(_active_page: Any) -> Any:
                page = None
                tab_id = None
                for candidate_id, candidate_page in session._tabs.items():
                    try:
                        if "youtube.com" in str(candidate_page.url).casefold() and not candidate_page.is_closed():
                            tab_id, page = candidate_id, candidate_page
                            break
                    except Exception:
                        continue
                if page is None:
                    return _error("No open YouTube player was found. Play a YouTube video first.")
                media = page.locator("video")
                if media.count() == 0:
                    return _error("The active YouTube page has no video player yet.")
                if command == "pause":
                    state = media.first.evaluate("video => { video.pause(); return {paused: video.paused, current_time: video.currentTime}; }")
                elif command == "resume":
                    state = media.first.evaluate("video => video.play().then(() => ({paused: video.paused, current_time: video.currentTime}))")
                elif command == "stop":
                    state = media.first.evaluate("video => { video.pause(); video.currentTime = 0; return {paused: video.paused, current_time: video.currentTime}; }")
                elif command == "volume":
                    state = media.first.evaluate("(video, amount) => { video.volume = amount; video.muted = amount === 0; return {volume: video.volume, muted: video.muted}; }", value)
                else:
                    state = media.first.evaluate("(video, seconds) => { video.currentTime = Math.max(0, seconds); return {current_time: video.currentTime, duration: video.duration}; }", value)
                return {"status": command, "tab_id": tab_id, **(state or {})}

            return session.run_with_recovery(action)
        except BrowserRecoveryError as exc:
            return _error(str(exc))
        except Exception as exc:
            return _error(f"YouTube playback control failed: {exc}")


class YouTubePauseTool(_YouTubePlaybackControlTool):
    name = "youtube_pause"
    description = "Pause the video in the open YouTube player. Local browser playback control; no account change."
    input_schema = {"type": "object", "properties": {}, "required": []}

    def run(self) -> Any:
        return self._control("pause")


class YouTubeResumeTool(_YouTubePlaybackControlTool):
    name = "youtube_resume"
    description = "Resume the video in the open YouTube player. Local browser playback control; no account change."
    input_schema = {"type": "object", "properties": {}, "required": []}

    def run(self) -> Any:
        return self._control("resume")


class YouTubeStopTool(_YouTubePlaybackControlTool):
    name = "youtube_stop"
    description = "Stop and rewind the open YouTube player to the beginning. Local browser playback control; no account change."
    input_schema = {"type": "object", "properties": {}, "required": []}

    def run(self) -> Any:
        return self._control("stop")


class YouTubeSetVolumeTool(_YouTubePlaybackControlTool):
    name = "youtube_set_volume"
    description = "Set the open YouTube player's volume from 0.0 to 1.0. Local browser playback control; no account change."
    input_schema = {"type": "object", "properties": {"volume": {"type": "number", "minimum": 0, "maximum": 1}}, "required": ["volume"]}

    def run(self, volume: float) -> Any:
        try:
            volume = float(volume)
        except (TypeError, ValueError):
            return _error("volume must be a number between 0 and 1.")
        if not 0 <= volume <= 1:
            return _error("volume must be between 0 and 1.")
        return self._control("volume", volume)


class YouTubeSeekTool(_YouTubePlaybackControlTool):
    name = "youtube_seek"
    description = "Seek the open YouTube player to a position in seconds. Local browser playback control; no account change."
    input_schema = {"type": "object", "properties": {"seconds": {"type": "number", "minimum": 0}}, "required": ["seconds"]}

    def run(self, seconds: float) -> Any:
        try:
            seconds = float(seconds)
        except (TypeError, ValueError):
            return _error("seconds must be a non-negative number.")
        if seconds < 0:
            return _error("seconds must be non-negative.")
        return self._control("seek", seconds)


def _comment_summary(item: dict[str, Any]) -> dict[str, Any]:
    snippet = item.get("snippet") or {}
    top = snippet.get("topLevelComment") or {}
    top_snippet = top.get("snippet") or {}
    return {
        "comment_id": top.get("id") or item.get("id"),
        "author": top_snippet.get("authorDisplayName"),
        "text": top_snippet.get("textDisplay") or top_snippet.get("textOriginal", ""),
        "like_count": top_snippet.get("likeCount", 0),
        "published_at": top_snippet.get("publishedAt"),
        "updated_at": top_snippet.get("updatedAt"),
        "reply_count": snippet.get("totalReplyCount", 0),
    }


class YouTubeListCommentsTool(_YouTubeTool):
    name = "youtube_list_comments"
    description = "List top-level comments on a YouTube video, with author, text, likes, and reply counts. Read-only."
    input_schema = {
        "type": "object",
        "properties": {
            "video": {"type": "string"},
            "max_results": {"type": "integer", "minimum": 1, "maximum": 100, "default": 20},
            "order": {"type": "string", "enum": ["time", "relevance"], "default": "relevance"},
        },
        "required": ["video"],
    }

    def run(self, video: str, max_results: int = 20, order: str = "relevance") -> Any:
        video_id = _video_id(video)
        if not _VIDEO_ID_RE.fullmatch(video_id):
            return _error("A valid YouTube video URL or video ID is required.")

        def action(api):
            response = api.commentThreads().list(
                part="snippet",
                videoId=video_id,
                maxResults=max(1, min(int(max_results), 100)),
                order=order,
                textFormat="plainText",
            ).execute()
            return {"video_id": video_id, "comments": [_comment_summary(item) for item in response.get("items", [])], "next_page_token": response.get("nextPageToken")}

        return self._call(action)


class YouTubePostCommentTool(_YouTubeTool):
    name = "youtube_post_comment"
    description = "Post a top-level comment on a YouTube video. Requires confirmation."
    input_schema = {"type": "object", "properties": {"video": {"type": "string"}, "text": {"type": "string"}}, "required": ["video", "text"]}

    def __init__(self, confirm_fn=None, root: Path = PROJECT_ROOT):
        super().__init__(root)
        self.confirm_fn = confirm_fn or (lambda _: False)

    def run(self, video: str, text: str) -> Any:
        video_id = _video_id(video)
        text = str(text or "").strip()
        if not _VIDEO_ID_RE.fullmatch(video_id) or not text:
            return _error("Provide a valid video URL/ID and non-empty comment text.")
        if not self.confirm_fn(f"Post this comment on YouTube video '{video_id}': {text!r}?"):
            return _error("Comment not posted: confirmation denied or not provided.")

        def action(api):
            item = api.commentThreads().insert(
                part="snippet",
                body={"snippet": {"videoId": video_id, "topLevelComment": {"snippet": {"textOriginal": text}}}},
            ).execute()
            return {"status": "posted", "video_id": video_id, "comment_id": item.get("id"), "text": text}

        return self._call(action, [YOUTUBE_FORCE_SSL_SCOPE])


class YouTubeReplyToCommentTool(_YouTubeTool):
    name = "youtube_reply_to_comment"
    description = "Reply to an existing YouTube comment by comment ID. Requires confirmation."
    input_schema = {"type": "object", "properties": {"comment_id": {"type": "string"}, "text": {"type": "string"}}, "required": ["comment_id", "text"]}

    def __init__(self, confirm_fn=None, root: Path = PROJECT_ROOT):
        super().__init__(root)
        self.confirm_fn = confirm_fn or (lambda _: False)

    def run(self, comment_id: str, text: str) -> Any:
        text = str(text or "").strip()
        if not comment_id or not text:
            return _error("Provide a comment ID and non-empty reply text.")
        if not self.confirm_fn(f"Reply to YouTube comment '{comment_id}' with {text!r}?"):
            return _error("Comment reply not posted: confirmation denied or not provided.")

        def action(api):
            item = api.comments().insert(
                part="snippet",
                body={"snippet": {"parentId": comment_id, "textOriginal": text}},
            ).execute()
            return {"status": "posted", "parent_comment_id": comment_id, "comment_id": item.get("id"), "text": text}

        return self._call(action, [YOUTUBE_FORCE_SSL_SCOPE])


class YouTubeListCaptionTracksTool(_YouTubeTool):
    name = "youtube_list_caption_tracks"
    description = "List caption tracks available for a YouTube video. Read-only; track contents require youtube_get_transcript."
    input_schema = {"type": "object", "properties": {"video": {"type": "string"}}, "required": ["video"]}

    def run(self, video: str) -> Any:
        video_id = _video_id(video)
        if not _VIDEO_ID_RE.fullmatch(video_id):
            return _error("A valid YouTube video URL or video ID is required.")

        def action(api):
            response = api.captions().list(part="snippet", videoId=video_id).execute()
            tracks = []
            for item in response.get("items", []):
                snippet = item.get("snippet") or {}
                tracks.append({
                    "caption_id": item.get("id"),
                    "language": snippet.get("language"),
                    "name": snippet.get("name"),
                    "track_kind": snippet.get("trackKind"),
                    "status": snippet.get("status"),
                    "is_draft": snippet.get("isDraft"),
                })
            return {"video_id": video_id, "tracks": tracks}

        return self._call(action)


class YouTubeGetTranscriptTool(_YouTubeTool):
    name = "youtube_get_transcript"
    description = "Download an available YouTube caption track and return its text, optionally choosing a language. Read-only."
    input_schema = {
        "type": "object",
        "properties": {"video": {"type": "string"}, "language": {"type": "string", "description": "Optional ISO language code such as en."}},
        "required": ["video"],
    }

    def run(self, video: str, language: str = "") -> Any:
        video_id = _video_id(video)
        if not _VIDEO_ID_RE.fullmatch(video_id):
            return _error("A valid YouTube video URL or video ID is required.")

        def action(api):
            listing = api.captions().list(part="snippet", videoId=video_id).execute()
            tracks = listing.get("items", [])
            if language:
                tracks = [item for item in tracks if (item.get("snippet") or {}).get("language") == language] or tracks
            if not tracks:
                return _error(f"No caption track is available for YouTube video '{video_id}'.")
            track = tracks[0]
            snippet = track.get("snippet") or {}
            raw = api.captions().download(id=track.get("id"), tfmt="vtt").execute()
            if isinstance(raw, bytes):
                raw = raw.decode("utf-8", errors="replace")
            text_lines = []
            for line in str(raw).splitlines():
                line = line.strip()
                if not line or "-->" in line or line.upper() == "WEBVTT" or line.isdigit():
                    continue
                text_lines.append(re.sub(r"<[^>]+>", "", line))
            text = "\n".join(text_lines)
            return {"video_id": video_id, "caption_id": track.get("id"), "language": snippet.get("language"), "text": text[:20000] + ("... [truncated]" if len(text) > 20000 else "")}

        return self._call(action, [YOUTUBE_FORCE_SSL_SCOPE])


class YouTubeListMyActivitiesTool(_YouTubeTool):
    name = "youtube_list_my_activities"
    description = "List recent activity for the connected YouTube account, such as uploads, likes, comments, and subscriptions. Read-only."
    input_schema = {"type": "object", "properties": {"max_results": {"type": "integer", "minimum": 1, "maximum": 50, "default": 25}}, "required": []}

    def run(self, max_results: int = 25) -> Any:
        def action(api):
            response = api.activities().list(part="snippet,contentDetails", mine=True, maxResults=max(1, min(int(max_results), 50))).execute()
            rows = []
            for item in response.get("items", []):
                snippet = item.get("snippet") or {}
                rows.append({"id": item.get("id"), "type": snippet.get("type"), "published_at": snippet.get("publishedAt"), "title": snippet.get("title"), "description": snippet.get("description")})
            return {"activities": rows, "next_page_token": response.get("nextPageToken")}

        return self._call(action)


class YouTubeUploadVideoTool(_YouTubeTool):
    name = "youtube_upload_video"
    description = "Upload a local video to YouTube. The path must be inside a configured filesystem root, privacy defaults to private, and confirmation is required."
    input_schema = {
        "type": "object",
        "properties": {
            "file_path": {"type": "string"},
            "title": {"type": "string"},
            "description": {"type": "string", "default": ""},
            "privacy_status": {"type": "string", "enum": ["private", "unlisted", "public"], "default": "private"},
            "tags": {"type": "array", "items": {"type": "string"}, "default": []},
            "category_id": {"type": "string", "default": "22"},
        },
        "required": ["file_path", "title"],
    }

    def __init__(self, confirm_fn=None, root: Path = PROJECT_ROOT):
        super().__init__(root)
        self.confirm_fn = confirm_fn or (lambda _: False)

    def run(self, file_path: str, title: str, description: str = "", privacy_status: str = "private", tags: list[str] | None = None, category_id: str = "22") -> Any:
        candidate, error = _allowed_local_file(self.root, file_path)
        if error:
            return _error(error)
        if privacy_status not in {"private", "unlisted", "public"}:
            return _error("privacy_status must be private, unlisted, or public.")
        if not self.confirm_fn(f"Upload '{candidate.name}' to YouTube as '{title}' with {privacy_status} visibility?"):
            return _error("Video not uploaded: confirmation denied or not provided.")

        def action(api):
            media = MediaFileUpload(str(candidate), resumable=True)
            request = api.videos().insert(
                part="snippet,status",
                body={
                    "snippet": {"title": title, "description": description, "tags": tags or [], "categoryId": category_id},
                    "status": {"privacyStatus": privacy_status},
                },
                media_body=media,
            )
            response = None
            while response is None:
                _, response = request.next_chunk()
            return {"status": "uploaded", "video_id": response.get("id"), "title": title, "privacy_status": privacy_status, "url": f"https://www.youtube.com/watch?v={response.get('id')}"}

        return self._call(action, [YOUTUBE_UPLOAD_SCOPE])


class YouTubeUpdateVideoTool(_YouTubeTool):
    name = "youtube_update_video"
    description = "Update selected metadata or privacy on a YouTube video. Requires confirmation."
    input_schema = {
        "type": "object",
        "properties": {
            "video": {"type": "string"},
            "title": {"type": "string"},
            "description": {"type": "string"},
            "privacy_status": {"type": "string", "enum": ["private", "unlisted", "public"]},
            "tags": {"type": "array", "items": {"type": "string"}},
        },
        "required": ["video"],
    }

    def __init__(self, confirm_fn=None, root: Path = PROJECT_ROOT):
        super().__init__(root)
        self.confirm_fn = confirm_fn or (lambda _: False)

    def run(self, video: str, title: str | None = None, description: str | None = None, privacy_status: str | None = None, tags: list[str] | None = None) -> Any:
        video_id = _video_id(video)
        if not _VIDEO_ID_RE.fullmatch(video_id):
            return _error("A valid YouTube video URL or video ID is required.")
        if all(value is None for value in (title, description, privacy_status, tags)):
            return _error("Provide at least one video field to update.")

        def action(api):
            response = api.videos().list(part="snippet,status", id=video_id).execute()
            items = response.get("items", [])
            if not items:
                return _error(f"YouTube video '{video_id}' was not found.")
            current = items[0]
            current_snippet = current.get("snippet") or {}
            current_status = current.get("status") or {}
            display_title = title if title is not None else current_snippet.get("title", video_id)
            if not self.confirm_fn(f"Update YouTube video '{display_title}' ({video_id})?"):
                return _error("Video not updated: confirmation denied or not provided.")
            updated = api.videos().update(
                part="snippet,status",
                body={
                    "id": video_id,
                    "snippet": {
                        "title": title if title is not None else current_snippet.get("title", ""),
                        "description": description if description is not None else current_snippet.get("description", ""),
                        "tags": tags if tags is not None else current_snippet.get("tags", []),
                        "categoryId": current_snippet.get("categoryId"),
                    },
                    "status": {"privacyStatus": privacy_status if privacy_status is not None else current_status.get("privacyStatus", "private")},
                },
            ).execute()
            return {"status": "updated", "video_id": video_id, "title": (updated.get("snippet") or {}).get("title")}

        return self._call(action, [YOUTUBE_FORCE_SSL_SCOPE])


class YouTubeDeleteVideoTool(_YouTubeTool):
    name = "youtube_delete_video"
    description = "Permanently delete a YouTube video after informed confirmation."
    input_schema = {"type": "object", "properties": {"video": {"type": "string"}}, "required": ["video"]}

    def __init__(self, confirm_fn=None, root: Path = PROJECT_ROOT):
        super().__init__(root)
        self.confirm_fn = confirm_fn or (lambda _: False)

    def run(self, video: str) -> Any:
        video_id = _video_id(video)
        if not _VIDEO_ID_RE.fullmatch(video_id):
            return _error("A valid YouTube video URL or video ID is required.")

        def action(api):
            response = api.videos().list(part="snippet", id=video_id).execute()
            items = response.get("items", [])
            if not items:
                return _error(f"YouTube video '{video_id}' was not found.")
            title = (items[0].get("snippet") or {}).get("title", video_id)
            if not self.confirm_fn(f"Permanently delete YouTube video '{title}' ({video_id})?"):
                return _error("Video not deleted: confirmation denied or not provided.")
            api.videos().delete(id=video_id).execute()
            return {"status": "deleted", "video_id": video_id, "title": title}

        return self._call(action, [YOUTUBE_FORCE_SSL_SCOPE])


def _download_worker(job_id: str, url: str, mode: str, quality_label: str, quality_cap: int | None, directory: Path, title: str, video_id: str) -> None:
    with _DOWNLOAD_LOCK:
        job = _DOWNLOAD_JOBS[job_id]
        job.update({"status": "downloading", "started_at": datetime.now(timezone.utc).isoformat()})
    print(f"YouTube download started: {title} ({mode}, {quality_label})")

    def hook(progress: dict[str, Any]) -> None:
        with _DOWNLOAD_LOCK:
            current = _DOWNLOAD_JOBS.get(job_id)
            if current is None:
                return
            if current.get("cancel_requested"):
                raise _DownloadCancelled()
            status = progress.get("status")
            current["status"] = "processing" if status == "finished" else "downloading"
            current["downloaded_bytes"] = int(progress.get("downloaded_bytes") or 0)
            total = progress.get("total_bytes") or progress.get("total_bytes_estimate")
            current["total_bytes"] = int(total) if total else None
            current["speed"] = progress.get("speed")
            current["eta_seconds"] = progress.get("eta")
            current["percent"] = round((current["downloaded_bytes"] / total) * 100, 1) if total else None

    try:
        import yt_dlp

        output_prefix = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
        output_template = str(directory / f"{output_prefix}_%(title).180B [%(id)s].%(ext)s")
        options: dict[str, Any] = {
            "noplaylist": True,
            "quiet": True,
            "no_warnings": True,
            "noprogress": True,
            "windowsfilenames": True,
            "overwrites": False,
            "continuedl": True,
            "retries": 3,
            "fragment_retries": 3,
            "outtmpl": output_template,
            "progress_hooks": [hook],
        }
        ffmpeg_dir = _ffmpeg_bin_dir()
        if ffmpeg_dir is not None:
            options["ffmpeg_location"] = str(ffmpeg_dir)
        if mode == "audio":
            options.update({
                "format": "bestaudio/best",
                "postprocessors": [{"key": "FFmpegExtractAudio", "preferredcodec": "mp3", "preferredquality": "192"}],
            })
        else:
            cap = quality_cap or 720
            options.update({
                "format": f"bestvideo[height<={cap}][ext=mp4]+bestaudio[ext=m4a]/best[height<={cap}][ext=mp4]/best[height<={cap}]",
                "merge_output_format": "mp4",
            })
        with yt_dlp.YoutubeDL(options) as downloader:
            downloader.download([url])
        expected_suffix = ".mp3" if mode == "audio" else ".mp4"
        candidates = [
            item for item in directory.iterdir()
            if item.is_file() and item.suffix.casefold() == expected_suffix and video_id in item.stem
        ]
        output = max(candidates, key=lambda item: item.stat().st_mtime, default=None)
        if output is None:
            raise RuntimeError(
                f"yt-dlp finished, but no final {expected_suffix[1:].upper()} file was found in '{directory}'."
            )
        with _DOWNLOAD_LOCK:
            job = _DOWNLOAD_JOBS[job_id]
            job.update({
                "status": "completed",
                "percent": 100.0,
                "output_path": str(output) if output else None,
                "filename": output.name if output else None,
                "completed_at": datetime.now(timezone.utc).isoformat(),
            })
        print(f"YouTube download completed: {output.name if output else title}")
    except _DownloadCancelled:
        with _DOWNLOAD_LOCK:
            _DOWNLOAD_JOBS[job_id].update({"status": "cancelled", "completed_at": datetime.now(timezone.utc).isoformat()})
        print(f"YouTube download cancelled: {title}")
    except Exception as exc:
        with _DOWNLOAD_LOCK:
            _DOWNLOAD_JOBS[job_id].update({"status": "failed", "error": str(exc), "completed_at": datetime.now(timezone.utc).isoformat()})
        print(f"YouTube download failed: {title}: {exc}")


class _YouTubeDownloadTool(Tool):
    mode: str

    def __init__(self, confirm_fn=None, root: Path = PROJECT_ROOT):
        self.root = root
        self.confirm_fn = confirm_fn or (lambda _: False)

    @staticmethod
    def _dependency_error() -> str | None:
        try:
            import yt_dlp  # noqa: F401
        except ImportError:
            return "yt-dlp is not installed. Run '.venv\\Scripts\\python.exe -m pip install yt-dlp'."
        if _ffmpeg_bin_dir() is None:
            return "FFmpeg and FFprobe are required for MP3 conversion and reliable MP4 merging. Install them from https://ffmpeg.org/download.html, then restart Neural."
        return None

    def _start(self, video_url: str, quality: str = "720p", destination: str = "") -> Any:
        dependency_error = self._dependency_error()
        if dependency_error:
            return _error(dependency_error)
        video_url = str(video_url or "").strip()
        # Accept the stable 11-character video ID as a convenience, while
        # keeping yt-dlp scoped to a single canonical YouTube video URL.
        if _VIDEO_ID_RE.fullmatch(video_url):
            video_url = f"https://www.youtube.com/watch?v={video_url}"
        parsed = urlparse(video_url)
        if parsed.hostname not in {"youtube.com", "www.youtube.com", "m.youtube.com", "youtu.be", "www.youtu.be"}:
            return _error("Only YouTube video URLs are supported by the downloader.")
        directory, error = _download_directory(self.root, destination)
        if error:
            return _error(error)
        try:
            import yt_dlp
            with yt_dlp.YoutubeDL({"quiet": True, "no_warnings": True, "noplaylist": True}) as downloader:
                info = downloader.extract_info(video_url, download=False)
        except Exception as exc:
            return _error(f"Could not inspect the YouTube video before download: {exc}")
        if not isinstance(info, dict) or info.get("_type") == "playlist":
            return _error("A single YouTube video URL is required, not a playlist URL.")
        title = str(info.get("title") or "YouTube video")
        video_id = str(info.get("id") or "")
        if self.mode == "video":
            quality_cap, quality_label = _quality_cap(quality)
            if quality_cap is None:
                return _error("quality must be low, high, 144p-2160p, or omitted for the default 720p.")
        else:
            quality_cap, quality_label = None, "audio"
        extension = "mp3" if self.mode == "audio" else "mp4"
        prompt = (
            f"Download YouTube video '{title}' as {extension.upper()}"
            + (f" at up to {quality_label}" if self.mode == "video" else "")
            + f" to '{directory}'?"
        )
        if not self.confirm_fn(prompt):
            return _error("YouTube download not started: confirmation denied or not provided.")
        job_id = uuid.uuid4().hex[:10]
        job = {
            "job_id": job_id,
            "status": "queued",
            "title": title,
            "video_id": video_id,
            "url": video_url,
            "type": self.mode,
            "format": extension,
            "quality": quality_label,
            "destination": str(directory),
            "percent": 0.0,
            "downloaded_bytes": 0,
            "total_bytes": None,
            "speed": None,
            "eta_seconds": None,
            "error": None,
            "output_path": None,
            "filename": None,
            "created_at": datetime.now(timezone.utc).isoformat(),
            "cancel_requested": False,
        }
        with _DOWNLOAD_LOCK:
            _DOWNLOAD_JOBS[job_id] = job
        _DOWNLOAD_EXECUTOR.submit(_download_worker, job_id, video_url, self.mode, quality_label, quality_cap, directory, title, video_id)
        return {
            "status": "started",
            "job_id": job_id,
            "title": title,
            "format": extension,
            "quality": quality_label,
            "destination": str(directory),
            "message": "Download started. Use youtube_download_status with this job_id for progress, or youtube_cancel_download to stop it.",
        }


class YouTubeDownloadVideoTool(_YouTubeDownloadTool):
    name = "youtube_download_video"
    description = "Download one authorized YouTube video as MP4. Quality defaults to 720p; use low for up to 360p, high for up to 1080p, or a resolution such as 480p. Requires confirmation."
    input_schema = {"type": "object", "properties": {"video_url": {"type": "string"}, "quality": {"type": "string", "default": "720p"}, "destination": {"type": "string", "description": "Optional configured filesystem folder; defaults to the YouTube download folder."}}, "required": ["video_url"]}
    mode = "video"

    def run(self, video_url: str, quality: str = "720p", destination: str = "") -> Any:
        return self._start(video_url, quality, destination)


class YouTubeDownloadAudioTool(_YouTubeDownloadTool):
    name = "youtube_download_audio"
    description = "Extract one authorized YouTube video as MP3 at 192 kbps. The output format is fixed to MP3 and requires confirmation; no format choice is needed."
    input_schema = {"type": "object", "properties": {"video_url": {"type": "string"}, "destination": {"type": "string", "description": "Optional configured filesystem folder; defaults to the YouTube download folder."}}, "required": ["video_url"]}
    mode = "audio"

    def run(self, video_url: str, destination: str = "") -> Any:
        return self._start(video_url, destination=destination)


class YouTubeDownloadStatusTool(Tool):
    name = "youtube_download_status"
    description = "Show progress and final path for one YouTube download job, or recent jobs when job_id is omitted. Read-only."
    input_schema = {"type": "object", "properties": {"job_id": {"type": "string"}}, "required": []}

    def run(self, job_id: str = "") -> Any:
        with _DOWNLOAD_LOCK:
            if job_id:
                job = _DOWNLOAD_JOBS.get(str(job_id))
                return _job_snapshot(job) if job else _error(f"YouTube download job '{job_id}' was not found.")
            jobs = list(_DOWNLOAD_JOBS.values())[-20:]
        return {"jobs": [_job_snapshot(job) for job in reversed(jobs)]}


class YouTubeCancelDownloadTool(Tool):
    name = "youtube_cancel_download"
    description = "Cancel a queued or active local YouTube download. It does not change the YouTube account."
    input_schema = {"type": "object", "properties": {"job_id": {"type": "string"}}, "required": ["job_id"]}

    def run(self, job_id: str) -> Any:
        with _DOWNLOAD_LOCK:
            job = _DOWNLOAD_JOBS.get(str(job_id))
            if job is None:
                return _error(f"YouTube download job '{job_id}' was not found.")
            if job.get("status") in {"completed", "failed", "cancelled"}:
                return _error(f"YouTube download job '{job_id}' is already {job.get('status')}.")
            job["cancel_requested"] = True
            job["status"] = "cancelling"
        return {"status": "cancelling", "job_id": str(job_id)}


class YouTubeSaveNotesTool(_YouTubeTool):
    name = "youtube_save_video_notes"
    description = "Save a video's metadata and description as a local Markdown note for the knowledge base. Existing notes are never overwritten; a numbered filename is used instead."
    input_schema = {
        "type": "object",
        "properties": {
            "video": {"type": "string"},
            "filename": {"type": "string", "description": "Optional Markdown filename without path traversal."},
        },
        "required": ["video"],
    }

    def run(self, video: str, filename: str = "") -> Any:
        result = YouTubeGetVideoTool(self.root).run(video)
        if not isinstance(result, dict) or result.get("error"):
            return result
        title = str(result.get("title") or "YouTube video")
        safe_name = re.sub(r"[^A-Za-z0-9._ -]+", "", filename or title).strip(" .")[:100]
        if not safe_name:
            safe_name = "youtube_video"
        if not safe_name.lower().endswith(".md"):
            safe_name += ".md"
        if Path(safe_name).name != safe_name or ".." in safe_name:
            return _error("filename must be a simple Markdown filename without path traversal.")

        settings = _settings(self.root)
        notes_dir = settings.workspace_root() / "knowledge_base"
        notes_dir.mkdir(parents=True, exist_ok=True)
        output = notes_dir / safe_name
        if output.exists():
            stem = output.stem
            suffix = output.suffix or ".md"
            counter = 1
            while output.exists():
                output = notes_dir / f"{stem} ({counter}){suffix}"
                counter += 1
        body = (
            f"# {title}\n\n"
            f"- URL: {result.get('url')}\n"
            f"- Channel: {result.get('channel_title') or 'Unknown'}\n"
            f"- Published: {result.get('published_at') or 'Unknown'}\n"
            f"- Saved at: {datetime.now(timezone.utc).isoformat()}\n\n"
            f"## Description\n\n{result.get('description') or '(No description provided.)'}\n"
        )
        try:
            output.write_text(body, encoding="utf-8")
        except OSError as exc:
            return _error(f"Could not save YouTube notes: {exc}")
        return {
            "status": "saved",
            "path": str(output),
            "message": "The web UI knowledge-base watcher will index this note automatically; CLI users can re-index manually.",
            "video_id": result.get("id"),
        }


def register(confirm_fn=None) -> list[Tool]:
    _ = confirm_fn
    return [
        YouTubeStatusTool(),
        YouTubeSearchTool(),
        YouTubeGetVideoTool(),
        YouTubeGetChannelTool(),
        YouTubeChannelOverviewTool(),
        YouTubeListPlaylistVideosTool(),
        YouTubeListSubscriptionsTool(),
        YouTubeListMyPlaylistsTool(),
        YouTubeCreatePlaylistTool(confirm_fn=confirm_fn),
        YouTubeAddToPlaylistTool(confirm_fn=confirm_fn),
        YouTubeRemoveFromPlaylistTool(confirm_fn=confirm_fn),
        YouTubeUpdatePlaylistTool(confirm_fn=confirm_fn),
        YouTubeDeletePlaylistTool(confirm_fn=confirm_fn),
        YouTubeSubscribeChannelTool(confirm_fn=confirm_fn),
        YouTubeUnsubscribeChannelTool(confirm_fn=confirm_fn),
        YouTubeRateVideoTool(confirm_fn=confirm_fn),
        YouTubeGetVideoRatingTool(),
        YouTubeOpenInBrowserTool(),
        YouTubePlayTool(confirm_fn=confirm_fn),
        YouTubePauseTool(),
        YouTubeResumeTool(),
        YouTubeStopTool(),
        YouTubeSetVolumeTool(),
        YouTubeSeekTool(),
        YouTubeListCommentsTool(),
        YouTubePostCommentTool(confirm_fn=confirm_fn),
        YouTubeReplyToCommentTool(confirm_fn=confirm_fn),
        YouTubeListCaptionTracksTool(),
        YouTubeGetTranscriptTool(),
        YouTubeListMyActivitiesTool(),
        YouTubeUploadVideoTool(confirm_fn=confirm_fn),
        YouTubeUpdateVideoTool(confirm_fn=confirm_fn),
        YouTubeDeleteVideoTool(confirm_fn=confirm_fn),
        YouTubeDownloadVideoTool(confirm_fn=confirm_fn),
        YouTubeDownloadAudioTool(confirm_fn=confirm_fn),
        YouTubeDownloadStatusTool(),
        YouTubeCancelDownloadTool(),
        YouTubeSaveNotesTool(),
    ]
