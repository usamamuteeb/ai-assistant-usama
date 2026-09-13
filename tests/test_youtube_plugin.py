"""Offline coverage for the read-oriented YouTube plugin."""
from __future__ import annotations

from plugins.youtube import plugin


def test_video_urls_are_normalized_to_ids():
    assert plugin._video_id("dQw4w9WgXcQ") == "dQw4w9WgXcQ"
    assert plugin._video_id("https://youtu.be/dQw4w9WgXcQ?t=12") == "dQw4w9WgXcQ"
    assert plugin._video_id("https://www.youtube.com/watch?v=dQw4w9WgXcQ") == "dQw4w9WgXcQ"
    assert plugin._video_id("https://www.youtube.com/shorts/dQw4w9WgXcQ") == "dQw4w9WgXcQ"


def test_search_normalizes_api_items_without_network_access(tmp_path):
    tool = plugin.YouTubeSearchTool(tmp_path)

    class Request:
        def execute(self):
            return {"items": [{
                "id": {"videoId": "dQw4w9WgXcQ"},
                "snippet": {
                    "title": "Example",
                    "description": "Description",
                    "channelId": "channel-1",
                    "channelTitle": "Channel",
                    "publishedAt": "2026-09-14T00:00:00Z",
                },
            }]}

    class Search:
        def list(self, **kwargs):
            assert kwargs["q"] == "example"
            return Request()

    class Api:
        def search(self):
            return Search()

    tool.service.api = lambda: Api()
    result = tool.run("example")
    assert result["results"][0]["id"] == "dQw4w9WgXcQ"
    assert result["results"][0]["title"] == "Example"


def test_register_exposes_the_initial_youtube_slice():
    names = {tool.name for tool in plugin.register()}
    assert names == {
        "youtube_status",
        "youtube_search",
        "youtube_get_video",
        "youtube_get_channel",
        "youtube_channel_overview",
        "youtube_list_playlist_videos",
        "youtube_list_subscriptions",
        "youtube_list_my_playlists",
        "youtube_create_playlist",
        "youtube_add_to_playlist",
        "youtube_remove_from_playlist",
        "youtube_update_playlist",
        "youtube_delete_playlist",
        "youtube_subscribe_channel",
        "youtube_unsubscribe_channel",
        "youtube_rate_video",
        "youtube_get_video_rating",
        "youtube_open_in_browser",
        "youtube_play",
        "youtube_pause",
        "youtube_resume",
        "youtube_stop",
        "youtube_set_volume",
        "youtube_seek",
        "youtube_list_comments",
        "youtube_post_comment",
        "youtube_reply_to_comment",
        "youtube_list_caption_tracks",
        "youtube_get_transcript",
        "youtube_list_my_activities",
        "youtube_upload_video",
        "youtube_update_video",
        "youtube_delete_video",
        "youtube_download_video",
        "youtube_download_audio",
        "youtube_download_status",
        "youtube_cancel_download",
        "youtube_save_video_notes",
    }


def test_play_requires_confirmation_after_selecting_a_video(tmp_path, monkeypatch):
    monkeypatch.setattr(
        plugin.YouTubeSearchTool,
        "run",
        lambda self, query, result_type="video", max_results=5: {
            "results": [{"id": "dQw4w9WgXcQ", "title": "Hindi song", "channel_title": "Artist"}]
        },
    )
    prompts = []
    tool = plugin.YouTubePlayTool(
        confirm_fn=lambda prompt: prompts.append(prompt) or False,
        root=tmp_path,
    )
    result = tool.run("play a Hindi song")
    assert result == {"error": "Playback not started: confirmation denied or not provided."}
    assert "Hindi song" in prompts[0]
    assert "youtube.com/watch?v=dQw4w9WgXcQ" in prompts[0]


def test_download_quality_defaults_and_keywords():
    assert plugin._quality_cap(None) == (720, "720p")
    assert plugin._quality_cap("low") == (360, "360p")
    assert plugin._quality_cap("high") == (1080, "1080p")
    assert plugin._quality_cap("480p") == (480, "480p")
    assert plugin._quality_cap("9999p") == (None, None)


def test_download_status_reports_unknown_job_without_network_access():
    result = plugin.YouTubeDownloadStatusTool().run("missing-job")
    assert result == {"error": "YouTube download job 'missing-job' was not found."}


def test_notes_choose_a_numbered_name_instead_of_overwriting(tmp_path, monkeypatch):
    (tmp_path / "config.yaml").write_text(
        "filesystem_tool:\n  workspace_root: workspace\n",
        encoding="utf-8",
    )
    tool = plugin.YouTubeSaveNotesTool(tmp_path)
    tool_result = {
        "id": "dQw4w9WgXcQ",
        "title": "A Video",
        "description": "Content",
        "url": "https://www.youtube.com/watch?v=dQw4w9WgXcQ",
        "channel_title": "Channel",
        "published_at": "2026-09-14T00:00:00Z",
    }
    monkeypatch.setattr(plugin.YouTubeGetVideoTool, "run", lambda self, video: tool_result)
    notes = tmp_path / "workspace" / "knowledge_base"
    notes.mkdir(parents=True)
    (notes / "A Video.md").write_text("original", encoding="utf-8")

    result = tool.run("dQw4w9WgXcQ")
    assert result["status"] == "saved"
    assert result["path"].endswith("A Video (1).md")
    assert (notes / "A Video.md").read_text(encoding="utf-8") == "original"
