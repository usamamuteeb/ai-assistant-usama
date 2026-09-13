"""Discovers built-in tools + plugins, and dispatches tool calls to them.

Add a new plugin by creating plugins/<name>/plugin.py with a register() function
that returns a list of Tool instances. No core code needs to change.
"""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from typing import Any

from .base import Tool
from .filesystem_tool import FilesystemTool
from .memory_tool import (
    ForgetFactTool,
    LogEpisodeTool,
    MemorySearchTool,
    RecallEpisodesTool,
    RecallFactsTool,
    RecallProcedureTool,
    RememberFactTool,
)
from ..memory.store import SqliteStore
from ..memory.vector_store import VectorMemory
from .shell_tool import ShellTool


class ToolRegistry:
    def __init__(self, tools: list[Tool]):
        self._tools: dict[str, Tool] = {t.name: t for t in tools}

    def anthropic_tools(self) -> list[dict[str, Any]]:
        return [t.to_anthropic_schema() for t in self._tools.values()]

    def call(self, name: str, **kwargs: Any) -> Any:
        tool = self._tools.get(name)
        if tool is None:
            return {"error": f"Unknown tool '{name}'."}
        try:
            return tool.run(**kwargs)
        except Exception as e:  # tool errors become model-visible feedback, not crashes
            return {"error": f"Tool '{name}' raised an exception: {e}"}

    def names(self) -> list[str]:
        return list(self._tools.keys())


def load_plugins(plugins_dir: Path, confirm_fn=None) -> list[Tool]:
    """Import every plugins/<name>/plugin.py and collect what register() returns."""
    discovered: list[Tool] = []
    if not plugins_dir.exists():
        return discovered

    for entry in sorted(plugins_dir.iterdir()):
        plugin_file = entry / "plugin.py"
        if not plugin_file.exists():
            continue
        module_name = f"plugins.{entry.name}.plugin"
        spec = importlib.util.spec_from_file_location(module_name, plugin_file)
        if spec is None or spec.loader is None:
            continue
        module = importlib.util.module_from_spec(spec)
        sys.modules[module_name] = module
        spec.loader.exec_module(module)
        if hasattr(module, "register"):
            discovered.extend(module.register(confirm_fn=confirm_fn))
    return discovered


def build_registry(
    settings,
    vector_store,
    confirm_fn=None,
    *,
    semantic_memory=None,
    episodic_memory=None,
    procedure_signatures=None,
    store=None,
) -> ToolRegistry:
    from ..config import Settings  # noqa: F401  (type hint only, avoids circular import at module load)

    retrieval_config = getattr(settings, "memory_retrieval", None)
    if not isinstance(retrieval_config, dict):
        raw_config = getattr(settings, "raw", {})
        retrieval_config = raw_config.get("memory_retrieval", {}) if isinstance(raw_config, dict) else {}
    procedure_config = retrieval_config.get("procedural_memory", {}) if isinstance(retrieval_config, dict) else {}
    semantic_memory = semantic_memory or VectorMemory(settings.chroma_path(), "semantic_memory")
    episodic_memory = episodic_memory or VectorMemory(settings.chroma_path(), "episodic_memory")
    procedure_signatures = procedure_signatures or VectorMemory(
        settings.chroma_path(), "procedure_signatures", distance_space="cosine"
    )
    store = store or SqliteStore(settings.sqlite_path())

    built_in: list[Tool] = [
        FilesystemTool(
            workspace_root=settings.workspace_root(),
            allowed_roots=settings.filesystem_tool.get("allowed_roots", []),
            confirm_fn=confirm_fn,
        ),
        ShellTool(
            timeout_seconds=settings.shell_tool.get("timeout_seconds", 30),
            require_confirmation=settings.shell_tool.get("require_confirmation", True),
            confirm_fn=confirm_fn,
        ),
        MemorySearchTool(vector_store=vector_store, retrieval_config=retrieval_config),
        RememberFactTool(semantic_memory),
        RecallFactsTool(semantic_memory, retrieval_config=retrieval_config),
        ForgetFactTool(semantic_memory, retrieval_config=retrieval_config),
        LogEpisodeTool(episodic_memory),
        RecallEpisodesTool(episodic_memory, retrieval_config=retrieval_config),
        RecallProcedureTool(
            procedure_signatures,
            store,
            retrieval_config=retrieval_config,
            similarity_threshold=procedure_config.get("similarity_threshold", 0.85),
        ),
    ]

    plugin_tools = load_plugins(settings.root / "plugins", confirm_fn=confirm_fn)
    return ToolRegistry(built_in + plugin_tools)
