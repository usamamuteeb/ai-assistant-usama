"""Run shell commands on the local machine — this is the 'laptop automation' tool.

Guarded by a confirmation callback so the CLI can ask you before anything runs.
When wired into the unattended scheduler, pass confirm=None (or a callback that
always returns True) only for commands you already trust.
"""
from __future__ import annotations

import subprocess
from typing import Any, Callable, Optional

from .base import Tool

ConfirmFn = Callable[[str], bool]


class ShellTool(Tool):
    name = "run_shell_command"
    description = (
        "Execute a shell command on the local machine and return stdout/stderr/exit code. "
        "Use this for automation tasks: running scripts, installing packages, checking system state, etc. "
        "Be specific and prefer non-destructive commands."
    )
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

        try:
            result = subprocess.run(
                command,
                shell=True,
                capture_output=True,
                text=True,
                timeout=self.timeout_seconds,
            )
            return {
                "exit_code": result.returncode,
                "stdout": result.stdout[-4000:],
                "stderr": result.stderr[-4000:],
            }
        except subprocess.TimeoutExpired:
            return {"error": f"Command timed out after {self.timeout_seconds}s."}
