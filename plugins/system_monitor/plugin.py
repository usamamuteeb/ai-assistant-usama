"""Optional system monitoring plugin for local process and resource insight."""
from __future__ import annotations

import os
from typing import Any, Callable, Optional

import psutil

from src.tools.base import Tool

ConfirmFn = Callable[[str], bool]


def _main_volume_path() -> str:
    root = os.path.abspath(os.sep)
    drive = os.path.splitdrive(root)[0]
    return drive + "\\" if drive else root


class SystemStatsTool(Tool):
    name = "system_stats"
    description = "Return the current CPU, memory, and disk usage for the local system."
    input_schema = {"type": "object", "properties": {}, "required": []}

    def run(self) -> Any:
        cpu_percent = psutil.cpu_percent(interval=None)
        memory = psutil.virtual_memory()
        disk = psutil.disk_usage(_main_volume_path())

        return {
            "cpu_percent": round(cpu_percent, 2),
            "memory": {
                "total_gb": round(memory.total / (1024 ** 3), 2),
                "used_gb": round(memory.used / (1024 ** 3), 2),
                "available_gb": round(memory.available / (1024 ** 3), 2),
                "percent": round(memory.percent, 2),
            },
            "disk": {
                "path": _main_volume_path(),
                "total_gb": round(disk.total / (1024 ** 3), 2),
                "used_gb": round(disk.used / (1024 ** 3), 2),
                "free_gb": round(disk.free / (1024 ** 3), 2),
                "percent": round((disk.used / disk.total) * 100, 2),
            },
        }


class ListProcessesTool(Tool):
    name = "list_processes"
    description = "List running processes sorted by memory usage, returning pid, name, cpu%, and memory usage."
    input_schema = {
        "type": "object",
        "properties": {
            "limit": {
                "type": "integer",
                "description": "Maximum number of processes to return, sorted by memory usage descending.",
                "default": 15,
            }
        },
        "required": [],
    }

    def run(self, limit: int = 15) -> Any:
        rows: list[dict[str, Any]] = []
        limit = max(1, int(limit))

        for proc in psutil.process_iter(["pid", "name", "cpu_percent", "memory_info"]):
            try:
                info = proc.info
                memory_info = info.get("memory_info")
                rss_bytes = memory_info.rss if memory_info is not None else 0
                rows.append(
                    {
                        "pid": int(info.get("pid", 0)),
                        "name": info.get("name", "unknown"),
                        "cpu_percent": round(float(info.get("cpu_percent", 0.0) or 0.0), 2),
                        "memory_mb": round(rss_bytes / (1024 ** 2), 2),
                    }
                )
            except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess):
                continue

        rows.sort(key=lambda item: item["memory_mb"], reverse=True)
        return {"count": len(rows[:limit]), "processes": rows[:limit]}


class KillProcessTool(Tool):
    name = "kill_process"
    description = "Terminate a running process by PID after explicit confirmation. This is destructive and protected against PID 0/1 and the current process."
    input_schema = {
        "type": "object",
        "properties": {"pid": {"type": "integer", "description": "The PID to terminate."}},
        "required": ["pid"],
    }

    def __init__(self, confirm_fn: Optional[ConfirmFn] = None):
        self.confirm_fn = confirm_fn or (lambda command: False)
        self.current_pid = os.getpid()

    def run(self, pid: int) -> Any:
        if pid in (0, 1, self.current_pid):
            return {"error": "Refusing to terminate protected process IDs (0, 1, or the current process)."}

        command = f"Terminate process {pid}? This is destructive."
        if not self.confirm_fn(command):
            return {"error": "Process not terminated: confirmation denied or not provided."}

        try:
            proc = psutil.Process(pid)
            proc.terminate()
            return {"status": "terminated", "pid": pid, "name": proc.name()}
        except psutil.NoSuchProcess:
            return {"error": f"Process {pid} does not exist."}
        except psutil.AccessDenied:
            return {"error": f"Access denied: process {pid} cannot be terminated."}


def register(confirm_fn: Optional[ConfirmFn] = None) -> list[Tool]:
    return [
        SystemStatsTool(),
        ListProcessesTool(),
        KillProcessTool(confirm_fn=confirm_fn),
    ]
