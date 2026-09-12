"""Local-only ComfyUI image generation for the personal assistant."""
from __future__ import annotations

import json
import os
import platform
import random
import re
import subprocess
import sys
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import psutil
import requests
import yaml

from src.tools.base import Tool

PROJECT_ROOT = Path(__file__).resolve().parents[2]


class GenerateImageTool(Tool):
    name = "generate_image"
    description = (
        "Generate or create a local image, picture, drawing, art, or illustration from a prompt "
        "with ComfyUI, then save it in the workspace. This uses the local CPU/GPU and no cloud "
        "API or image credits."
    )
    input_schema = {
        "type": "object",
        "properties": {"prompt": {"type": "string", "description": "Detailed image description."}},
        "required": ["prompt"],
    }

    def __init__(self, root: Path = PROJECT_ROOT):
        self.root = root
        config = self._full_config().get("image_generation", {})
        self.image_generation_config = config if isinstance(config, dict) else {}
        self.config = config.get("local_comfyui", {}) if isinstance(config, dict) else {}
        if not isinstance(self.config, dict):
            self.config = {}
        self._start_error: str | None = None

    def get_setup_status(self) -> dict[str, Any]:
        """Return a fast, display-safe snapshot for the NiceGUI status panel."""
        install_dir = self._configured_path("install_dir", "data/comfyui")
        status = _read_json(install_dir / "setup_status.json")
        state = str(status.get("state", "")).lower()
        detail = str(status.get("detail", "")).strip()
        phase = str(status.get("phase", "")).strip()
        lock_present = (install_dir / ".setup.lock").is_file()
        launcher_present = (install_dir / str(self.config.get("launch_script", "run_cpu.bat"))).is_file()

        if self._server_ready():
            status = _write_health_status(
                install_dir,
                "ready",
                "service",
                f"ComfyUI is online at {self._base_url()} and ready to generate images.",
                status,
            )
            return {
                "state": "ready",
                "phase": "service",
                "detail": f"ComfyUI is online at {self._base_url()} and ready to generate images.",
                "updated_at": status.get("updated_at", ""),
                "log_tail": _tail_lines(install_dir / "setup.log"),
                "can_retry": False,
            }
        if lock_present:
            return {
                "state": "processing",
                "phase": phase or "setup",
                "detail": detail or "Preparing local ComfyUI in the background.",
                "updated_at": status.get("updated_at", ""),
                "log_tail": _tail_lines(install_dir / "setup.log"),
                "can_retry": False,
            }
        if state == "failed":
            return {
                "state": "failed",
                "phase": phase or "setup",
                "detail": detail or "Local ComfyUI setup failed. Review the installer log and retry.",
                "updated_at": status.get("updated_at", ""),
                "log_tail": _tail_lines(install_dir / "setup.log"),
                "can_retry": True,
            }
        if launcher_present:
            return {
                "state": "starting",
                "phase": "service",
                "detail": "ComfyUI is installed, but its local service is not responding yet.",
                "updated_at": status.get("updated_at", ""),
                "log_tail": _tail_lines(install_dir / "setup.log"),
                "can_retry": True,
            }
        return {
            "state": "not_installed",
            "phase": "setup",
            "detail": "ComfyUI has not been installed yet. Start setup to download the local runtime and model.",
            "updated_at": status.get("updated_at", ""),
            "log_tail": _tail_lines(install_dir / "setup.log"),
            "can_retry": True,
        }

    def retry_setup(self) -> dict[str, Any]:
        """Request setup/server startup without waiting for it to complete."""
        self._start_error = None
        self.start_if_configured()
        return self.get_setup_status()

    def start_if_configured(self) -> None:
        """Launch the known local ComfyUI CPU server when the web app starts."""
        if not self.config.get("auto_start", True) or self._server_ready():
            return
        install_dir = self._configured_path("install_dir", "data/comfyui")
        script_name = str(self.config.get("launch_script", "run_cpu.bat"))
        script_path = install_dir / script_name
        if not script_path.is_file():
            self._start_setup_if_needed(install_dir)
            return
        try:
            creation_flags = getattr(subprocess, "CREATE_NO_WINDOW", 0) if platform.system() == "Windows" else 0
            if platform.system() == "Windows":
                command = ["cmd.exe", "/c", str(script_path)]
            else:
                command = ["/bin/sh", str(script_path)]
            subprocess.Popen(
                command,
                cwd=str(install_dir),
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                creationflags=creation_flags,
            )
        except OSError as exc:
            self._start_error = f"Could not start local ComfyUI: {exc}"

    def _start_setup_if_needed(self, install_dir: Path) -> None:
        """Start one resumable, project-local bootstrap process when configured."""
        if not self.config.get("auto_install", True):
            self._start_error = f"ComfyUI is not installed at '{install_dir}'."
            return
        install_dir.mkdir(parents=True, exist_ok=True)
        lock_path = install_dir / ".setup.lock"
        try:
            descriptor = os.open(lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            os.close(descriptor)
        except FileExistsError:
            self._start_error = "Local ComfyUI setup is already running. Check data/comfyui/setup.log for progress."
            return

        log_path = install_dir / "setup.log"
        try:
            with log_path.open("ab") as log_file:
                process = subprocess.Popen(
                    [sys.executable, "-m", "src.comfyui_setup"],
                    cwd=str(self.root),
                    stdout=log_file,
                    stderr=subprocess.STDOUT,
                    creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                )
            lock_path.write_text(json.dumps({"pid": process.pid, "started_at": time.time()}), encoding="utf-8")
            self._start_error = (
                "Initial local ComfyUI setup has started. It downloads several GB once; "
                "check data/comfyui/setup.log for progress, then retry the image request."
            )
        except OSError as exc:
            lock_path.unlink(missing_ok=True)
            self._start_error = f"Could not start local ComfyUI setup: {exc}"

    def run(self, prompt: str) -> Any:
        if not prompt.strip():
            return {"error": "An image prompt is required."}
        minimum_ram_gb = _positive_float(
            self.image_generation_config.get("generate_image_min_free_ram_gb", 1.5), 1.5
        )
        available_ram_gb = psutil.virtual_memory().available / (1024**3)
        if available_ram_gb < minimum_ram_gb:
            return {
                "error": (
                    f"Only {available_ram_gb:.1f}GB RAM free — generation needs at least "
                    f"{minimum_ram_gb:.1f}GB and is likely to fail or hang on this system right now. "
                    "Close some applications and try again."
                )
            }
        ready_error = self._ensure_server_ready()
        if ready_error:
            return {"error": ready_error}
        return self._generate(prompt)

    def _ensure_server_ready(self) -> str | None:
        if self._server_ready():
            return None
        self.start_if_configured()
        # Initial setup can take a long time and must never occupy an
        # orchestrator tool-loop slot while a multi-GB download is underway.
        # The next request will use the server once the background installer
        # reports ready.
        if self._start_error:
            return f"Local image generation is unavailable: {self._start_error}"
        timeout = _positive_int(self.config.get("start_timeout_seconds", 45), 45)
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if self._server_ready():
                return None
            time.sleep(1)
        detail = self._start_error or "ComfyUI started but did not become ready."
        return f"Local image generation is unavailable: {detail}"

    def _server_ready(self) -> bool:
        try:
            response = requests.get(f"{self._base_url()}/system_stats", timeout=3)
            return 200 <= response.status_code < 300
        except requests.RequestException:
            return False

    def _generate(self, prompt: str) -> dict[str, Any]:
        checkpoint = str(self.config.get("checkpoint", "v1-5-pruned-emaonly-fp16.safetensors"))
        timeout = _positive_int(self.config.get("timeout_seconds", 180), 180)
        workflow = _comfy_workflow(
            prompt=prompt,
            negative_prompt=str(self.config.get("negative_prompt", "")),
            checkpoint=checkpoint,
            # Conservative defaults are intentional for CPU-only hardware.
            # Increase them only after moving image generation to a capable GPU.
            width=_positive_int(self.config.get("width", 512), 512),
            height=_positive_int(self.config.get("height", 512), 512),
            steps=_positive_int(self.config.get("steps", 20), 20),
            cfg=_positive_float(self.config.get("cfg", 7.0), 7.0),
            sampler=str(self.config.get("sampler_name", "euler")),
            scheduler=str(self.config.get("scheduler", "normal")),
        )
        try:
            response = requests.post(
                f"{self._base_url()}/prompt",
                json={"prompt": workflow, "client_id": f"personal-ai-assistant-{uuid.uuid4().hex}"},
                timeout=15,
            )
            if not 200 <= response.status_code < 300:
                return {"error": f"ComfyUI rejected the workflow ({response.status_code}): {_response_preview(response)}"}
            prompt_id = str(response.json().get("prompt_id", ""))
            if not prompt_id:
                return {"error": "ComfyUI accepted the request but did not return a prompt ID."}
        except (requests.RequestException, ValueError) as exc:
            return {"error": f"Could not submit the local ComfyUI workflow: {exc}"}

        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            try:
                history = requests.get(f"{self._base_url()}/history/{prompt_id}", timeout=15)
                if 200 <= history.status_code < 300:
                    image = _output_image(history.json(), prompt_id)
                    if image:
                        return self._download_output(image, checkpoint)
            except (requests.RequestException, ValueError) as exc:
                return {"error": f"Could not read the local ComfyUI result: {exc}"}
            time.sleep(1)
        return {"error": f"Local ComfyUI did not finish within {timeout}s."}

    def _download_output(self, image: dict[str, Any], checkpoint: str) -> dict[str, Any]:
        filename = str(image.get("filename", ""))
        if not filename:
            return {"error": "ComfyUI completed but returned an image without a filename."}
        try:
            response = requests.get(
                f"{self._base_url()}/view",
                params={"filename": filename, "subfolder": str(image.get("subfolder", "")), "type": str(image.get("type", "output"))},
                timeout=30,
            )
            if not 200 <= response.status_code < 300 or not response.content:
                return {"error": f"Could not download ComfyUI output ({response.status_code}): {_response_preview(response)}"}
        except requests.RequestException as exc:
            return {"error": f"Could not download local ComfyUI output: {exc}"}
        mime_type = response.headers.get("content-type", "image/png").split(";", 1)[0]
        workspace = self._workspace_root()
        output_dir = workspace / "generated_images"
        output_dir.mkdir(parents=True, exist_ok=True)
        output_path = output_dir / f"{datetime.now(timezone.utc).strftime('%Y%m%d_%H%M%S_%f')}.{_extension(mime_type)}"
        try:
            output_path.write_bytes(response.content)
        except OSError as exc:
            return {"error": f"Could not save generated image: {exc}"}
        return {"path": output_path.relative_to(workspace).as_posix(), "mimeType": mime_type, "backend": "local_comfyui", "model": checkpoint}

    def _full_config(self) -> dict[str, Any]:
        path = self.root / "config.yaml"
        try:
            with path.open("r", encoding="utf-8") as handle:
                config = yaml.safe_load(handle) or {}
            return config if isinstance(config, dict) else {}
        except (OSError, yaml.YAMLError):
            return {}

    def _configured_path(self, key: str, default: str) -> Path:
        value = Path(str(self.config.get(key, default)))
        return value if value.is_absolute() else self.root / value

    def _workspace_root(self) -> Path:
        configured = self._full_config().get("filesystem_tool", {}).get("workspace_root", "workspace")
        path = Path(configured)
        return path if path.is_absolute() else self.root / path

    def _base_url(self) -> str:
        return str(self.config.get("url", "http://127.0.0.1:8188")).rstrip("/")


def _comfy_workflow(**values: Any) -> dict[str, Any]:
    return {
        "1": {"class_type": "CheckpointLoaderSimple", "inputs": {"ckpt_name": values["checkpoint"]}},
        "2": {"class_type": "CLIPTextEncode", "inputs": {"text": values["prompt"], "clip": ["1", 1]}},
        "3": {"class_type": "CLIPTextEncode", "inputs": {"text": values["negative_prompt"], "clip": ["1", 1]}},
        "4": {"class_type": "EmptyLatentImage", "inputs": {"width": values["width"], "height": values["height"], "batch_size": 1}},
        "5": {"class_type": "KSampler", "inputs": {"seed": random.randint(0, 2**63 - 1), "steps": values["steps"], "cfg": values["cfg"], "sampler_name": values["sampler"], "scheduler": values["scheduler"], "denoise": 1.0, "model": ["1", 0], "positive": ["2", 0], "negative": ["3", 0], "latent_image": ["4", 0]}},
        "6": {"class_type": "VAEDecode", "inputs": {"samples": ["5", 0], "vae": ["1", 2]}},
        "7": {"class_type": "SaveImage", "inputs": {"filename_prefix": "PersonalAIAssistant", "images": ["6", 0]}},
    }


def _output_image(data: dict[str, Any], prompt_id: str) -> dict[str, Any] | None:
    history = data.get(prompt_id, data)
    outputs = history.get("outputs", {}) if isinstance(history, dict) else {}
    for output in outputs.values() if isinstance(outputs, dict) else []:
        images = output.get("images", []) if isinstance(output, dict) else []
        if images and isinstance(images[0], dict):
            return images[0]
    return None


def _positive_int(value: Any, default: int) -> int:
    try:
        return max(1, int(value))
    except (TypeError, ValueError):
        return default


def _positive_float(value: Any, default: float) -> float:
    try:
        return max(0.1, float(value))
    except (TypeError, ValueError):
        return default


def _extension(mime_type: str) -> str:
    subtype = mime_type.partition("/")[2].split(";", 1)[0].lower()
    return {"jpeg": "jpg"}.get(subtype, re.sub(r"[^a-z0-9]+", "", subtype) or "png")


def _response_preview(response: requests.Response) -> str:
    try:
        raw = str(response.json())
    except ValueError:
        raw = response.text
    return " ".join(raw.split())[:200]


def _read_json(path: Path) -> dict[str, Any]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _tail_lines(path: Path, limit: int = 8) -> list[str]:
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
        return [line[-240:] for line in lines[-limit:] if line.strip()]
    except OSError:
        return []


def _write_health_status(
    install_dir: Path,
    state: str,
    phase: str,
    detail: str,
    current: dict[str, Any],
) -> dict[str, Any]:
    """Self-heal pre-panel setup files without rewriting them on every poll."""
    if (
        current.get("state") == state
        and current.get("phase") == phase
        and current.get("detail") == detail
        and current.get("updated_at")
    ):
        return current
    status = {
        "state": state,
        "phase": phase,
        "detail": detail,
        "updated_at": datetime.now(timezone.utc).isoformat(),
    }
    try:
        install_dir.mkdir(parents=True, exist_ok=True)
        (install_dir / "setup_status.json").write_text(json.dumps(status, indent=2), encoding="utf-8")
    except OSError:
        pass
    return status


def register(confirm_fn=None) -> list[Tool]:
    _ = confirm_fn
    # Starting a local server is a web-UI concern.  Keeping plugin discovery
    # side-effect free means CLI and scheduler processes can still load tools
    # without launching a second ComfyUI instance.
    return [GenerateImageTool()]
