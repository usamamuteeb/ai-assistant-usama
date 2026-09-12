"""Project-local, resumable installer for CPU-mode ComfyUI on Windows.

Run indirectly by the NiceGUI image plugin. It intentionally installs only
inside ``data/comfyui`` and never changes system Python or Windows settings.
"""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import sys
import time
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_INSTALL_DIR = ROOT / "data" / "comfyui"
COMFY_REPOSITORY = "https://github.com/Comfy-Org/ComfyUI.git"
CHECKPOINT_URL = (
    "https://huggingface.co/Comfy-Org/stable-diffusion-v1-5-archive/resolve/main/"
    "v1-5-pruned-emaonly-fp16.safetensors?download=true"
)
CHECKPOINT_SHA256 = "e9476a13728cd75d8279f6ec8bad753a66a1957ca375a1464dc63b37db6e3916"


def _load_config() -> dict:
    try:
        with (ROOT / "config.yaml").open("r", encoding="utf-8") as config_file:
            config = yaml.safe_load(config_file) or {}
        return config if isinstance(config, dict) else {}
    except (OSError, yaml.YAMLError):
        return {}


def _install_dir(config: dict) -> Path:
    local = config.get("image_generation", {}).get("local_comfyui", {})
    value = Path(str(local.get("install_dir", "data/comfyui")))
    return value if value.is_absolute() else ROOT / value


def _write_status(directory: Path, state: str, detail: str, phase: str = "") -> None:
    (directory / "setup_status.json").write_text(
        json.dumps(
            {
                "state": state,
                "phase": phase,
                "detail": detail,
                "updated_at": datetime.now(timezone.utc).isoformat(),
            },
            indent=2,
        ),
        encoding="utf-8",
    )


def _run(command: list[str], cwd: Path | None = None, *, timeout: int = 1800) -> None:
    print("+", " ".join(command), flush=True)
    subprocess.run(command, cwd=cwd, check=True, timeout=timeout)


def _download_checkpoint(destination: Path) -> None:
    if destination.is_file() and _sha256(destination) == CHECKPOINT_SHA256:
        return
    temporary = destination.with_suffix(destination.suffix + ".part")
    temporary.unlink(missing_ok=True)
    print(f"Downloading default checkpoint to {destination}", flush=True)
    request = urllib.request.Request(CHECKPOINT_URL, headers={"User-Agent": "personal-ai-assistant/1.0"})
    with urllib.request.urlopen(request, timeout=60) as response, temporary.open("wb") as output:
        shutil.copyfileobj(response, output, length=1024 * 1024)
    if _sha256(temporary) != CHECKPOINT_SHA256:
        temporary.unlink(missing_ok=True)
        raise RuntimeError("Downloaded checkpoint failed its SHA-256 verification.")
    temporary.replace(destination)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as file:
        for block in iter(lambda: file.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _write_launcher(directory: Path) -> None:
    launcher = directory / "run_cpu.bat"
    launcher.write_text(
        "@echo off\r\n"
        "\"%~dp0.venv\\Scripts\\python.exe\" -s \"%~dp0ComfyUI\\main.py\" "
        "--cpu --listen 127.0.0.1 --port 8188\r\n",
        encoding="utf-8",
    )


def _clone_comfyui(source_dir: Path) -> None:
    """Download ComfyUI with conservative Git transport and bounded retries."""
    last_error: Exception | None = None
    for attempt in range(1, 4):
        if source_dir.exists():
            shutil.rmtree(source_dir)
        try:
            # HTTP/1.1 avoids a common Windows Git/libcurl reset seen with
            # HTTP/2 on slower or filtered connections.
            _run(
                [
                    "git",
                    "-c",
                    "http.version=HTTP/1.1",
                    "clone",
                    "--depth",
                    "1",
                    COMFY_REPOSITORY,
                    str(source_dir),
                ]
            )
            return
        except (subprocess.CalledProcessError, subprocess.TimeoutExpired) as exc:
            last_error = exc
            print(f"ComfyUI source download attempt {attempt}/3 failed: {exc}", flush=True)
            if attempt < 3:
                time.sleep(5 * attempt)
    raise RuntimeError(
        "Could not download ComfyUI after 3 attempts. Check the internet connection or "
        "a firewall/proxy blocking GitHub, then restart the web UI."
    ) from last_error


def install() -> None:
    directory = _install_dir(_load_config())
    directory.mkdir(parents=True, exist_ok=True)
    _write_status(directory, "processing", "Preparing local CPU-mode ComfyUI.", "prepare")
    source_dir = directory / "ComfyUI"
    environment_dir = directory / ".venv"
    try:
        if not (source_dir / "requirements.txt").is_file():
            _write_status(directory, "processing", "Downloading the official ComfyUI source.", "source")
            _clone_comfyui(source_dir)
        python = environment_dir / "Scripts" / "python.exe"
        if not python.is_file():
            _write_status(directory, "processing", "Creating an isolated Python environment.", "environment")
            _run([sys.executable, "-m", "venv", str(environment_dir)])
        _write_status(directory, "processing", "Installing the CPU PyTorch runtime.", "pytorch")
        _run([str(python), "-m", "pip", "install", "--upgrade", "pip"])
        _run([str(python), "-m", "pip", "install", "torch", "torchvision", "--index-url", "https://download.pytorch.org/whl/cpu"])
        _write_status(directory, "processing", "Installing ComfyUI dependencies.", "dependencies")
        _run([str(python), "-m", "pip", "install", "-r", str(source_dir / "requirements.txt")])
        checkpoint_dir = source_dir / "models" / "checkpoints"
        checkpoint_dir.mkdir(parents=True, exist_ok=True)
        _write_status(directory, "processing", "Downloading the default Stable Diffusion checkpoint.", "checkpoint")
        _download_checkpoint(checkpoint_dir / "v1-5-pruned-emaonly-fp16.safetensors")
        _write_launcher(directory)
        _write_status(directory, "ready", "ComfyUI is installed. Starting its local service now.", "service")
        print("Local ComfyUI setup completed successfully.", flush=True)
    except Exception as exc:
        _write_status(directory, "failed", str(exc), "failed")
        print(f"Local ComfyUI setup failed: {exc}", flush=True)
        raise
    finally:
        (directory / ".setup.lock").unlink(missing_ok=True)


if __name__ == "__main__":
    install()
