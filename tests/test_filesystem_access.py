from pathlib import Path

from src.tools.filesystem_tool import FilesystemTool


def test_absolute_user_folder_access_and_only_destructive_actions_confirm(tmp_path):
    workspace = tmp_path / "workspace"
    user_folder = tmp_path / "Documents"
    user_folder.mkdir()
    decisions = []
    tool = FilesystemTool(
        workspace,
        allowed_roots=[user_folder],
        confirm_fn=lambda prompt: decisions.append(prompt) or False,
    )

    target = user_folder / "notes.txt"
    written = tool.run("write", str(target), "first")
    assert written["status"] == "written"
    assert decisions == []

    replaced = tool.run("write", str(target), "second")
    assert replaced == {"error": "Action not performed: confirmation denied or not provided."}
    assert target.read_text(encoding="utf-8") == "first"
    assert len(decisions) == 1

    read = tool.run("read", str(target))
    assert read["content"] == "first"
    listing = tool.run("list", str(user_folder))
    assert "notes.txt" in listing["entries"]

    deleted = tool.run("delete", str(target))
    assert deleted == {"error": "Action not performed: confirmation denied or not provided."}
    assert target.exists()


def test_absolute_path_outside_configured_roots_is_refused(tmp_path):
    workspace = tmp_path / "workspace"
    allowed = tmp_path / "Documents"
    outside = tmp_path / "Pictures"
    allowed.mkdir()
    outside.mkdir()
    tool = FilesystemTool(workspace, allowed_roots=[allowed])
    result = tool.run("read", str(outside / "image.png"))
    assert "outside the configured filesystem roots" in result["error"]
