"""Minimal tests covering the parts that don't need network/API keys:
filesystem sandboxing and the SQLite store. Run with: pytest tests/
"""
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.memory.store import SqliteStore
from src.tools.filesystem_tool import FilesystemTool


def test_filesystem_tool_write_and_read():
    with tempfile.TemporaryDirectory() as tmp:
        tool = FilesystemTool(workspace_root=Path(tmp))
        write_result = tool.run(action="write", path="notes/todo.txt", content="hello")
        assert write_result["status"] == "written"

        read_result = tool.run(action="read", path="notes/todo.txt")
        assert read_result["content"] == "hello"


def test_filesystem_tool_blocks_escape():
    with tempfile.TemporaryDirectory() as tmp:
        tool = FilesystemTool(workspace_root=Path(tmp))
        result = tool.run(action="read", path="../../etc/passwd")
        assert "error" in result


def test_filesystem_tool_list():
    with tempfile.TemporaryDirectory() as tmp:
        tool = FilesystemTool(workspace_root=Path(tmp))
        tool.run(action="write", path="a.txt", content="1")
        tool.run(action="write", path="b.txt", content="2")
        result = tool.run(action="list", path=".")
        assert set(result["entries"]) == {"a.txt", "b.txt"}


def test_sqlite_store_conversation_log():
    with tempfile.TemporaryDirectory() as tmp:
        with SqliteStore(Path(tmp) / "test.db") as store:
            store.log_message("session-1", "user", "hi")
            store.log_message("session-1", "assistant", "hello")
            history = store.recent_messages("session-1")
            assert [h["role"] for h in history] == ["user", "assistant"]


def test_sqlite_store_kv_state():
    with tempfile.TemporaryDirectory() as tmp:
        with SqliteStore(Path(tmp) / "test.db") as store:
            store.set_state("foo", {"bar": 1})
            assert store.get_state("foo") == {"bar": 1}
            assert store.get_state("missing", default="fallback") == "fallback"


def test_sqlite_store_tasks():
    with tempfile.TemporaryDirectory() as tmp:
        with SqliteStore(Path(tmp) / "test.db") as store:
            task_id = store.add_task("test task")
            assert len(store.open_tasks()) == 1
            store.update_task_status(task_id, "done")
            assert len(store.open_tasks()) == 0
