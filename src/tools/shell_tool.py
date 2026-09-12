"""Run shell commands on the local machine — this is the 'laptop automation' tool.

Guarded by a confirmation callback so the CLI can ask you before anything runs.
When wired into the unattended scheduler, pass confirm=None (or a callback that
always returns True) only for commands you already trust.
"""
from __future__ import annotations

import os
import platform
import signal
import subprocess
from typing import Any, Callable, Optional

from .base import Tool

ConfirmFn = Callable[[str], bool]


class ShellTool(Tool):
    name = "run_shell_command"
    description = "Execute a specific shell command and return its output and exit code, with confirmation when required."
    input_schema = {
        "type": "object",
        "properties": {
            "command": {"type": "string", "description": "The shell command to run."},
        },
        "required": ["command"],
    }

    def __init__(
        self,
        timeout_seconds: int = 30,
        require_confirmation: bool = True,
        confirm_fn: Optional[ConfirmFn] = None,
    ):
        self.timeout_seconds = timeout_seconds
        self.require_confirmation = require_confirmation
        # Default confirm_fn: reject everything, so unattended runs fail closed
        # unless the caller explicitly wires up a real confirmation path.
        self.confirm_fn = confirm_fn or (lambda cmd: False)

    def run(self, command: str) -> Any:
        if self.require_confirmation and not self.confirm_fn(command):
            return {"error": "Command not executed: confirmation denied or not provided."}

        is_windows = platform.system() == "Windows"
        creation_flags = subprocess.CREATE_NEW_PROCESS_GROUP if is_windows else 0
        process = subprocess.Popen(
            command,
            shell=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            creationflags=creation_flags,
            start_new_session=not is_windows,
        )
        try:
            stdout, stderr = process.communicate(timeout=self.timeout_seconds)
        except subprocess.TimeoutExpired:
            if is_windows:
                subprocess.run(
                    ["taskkill", "/PID", str(process.pid), "/T", "/F"],
                    capture_output=True,
                    text=True,
                    check=False,
                )
            else:
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                process.kill()
            process.communicate()
            return {
                "error": (
                    f"Command timed out after {self.timeout_seconds}s and was forcibly "
                    "terminated (including any child processes)."
                )
            }
        return {
            "exit_code": process.returncode,
            "stdout": stdout[-4000:],
            "stderr": stderr[-4000:],
        }
