"""Gemini image generation plugin."""
from __future__ import annotations

import base64
import os
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import requests
from dotenv import load_dotenv
import yaml

from src.tools.base import Tool

PROJECT_ROOT = Path(__file__).resolve().parents[2]
MODEL = "gemini-3.1-flash-image"
_GEMINI_API_URL = "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"


class GenerateImageTool(Tool):
    name = "generate_image"
    description = "Generate an image from a text prompt with Gemini and save it in the workspace."
    input_schema = {
        "type": "object",
        "properties": {
            "prompt": {
                "type": "string",
                "description": "A detailed text description of the image to generate.",
            }
        },
        "required": ["prompt"],
    }

    def __init__(self, root: Path = PROJECT_ROOT):
        self.root = root
        load_dotenv(root / ".env")
        self.api_key = os.getenv("GEMINI_API_KEY")

    def run(self, prompt: str) -> Any:
        if not self.api_key:
            return {"error": "GEMINI_API_KEY is not set. Add it to .env to generate images."}
        if not prompt.strip():
            return {"error": "An image prompt is required."}

        payload = {
            "contents": [{"parts": [{"text": prompt}]}],
            "generationConfig": {"responseModalities": ["IMAGE"]},
        }
        try:
            response = requests.post(
                _GEMINI_API_URL.format(model=MODEL),
                params={"key": self.api_key},
                json=payload,
                timeout=120,
            )
        except requests.RequestException as exc:
            return {"error": f"Image generation request failed: {exc}"}

        if response.status_code in {403, 429}:
            raw_error = _response_preview(response)
            return {
                "error": (
                    f"Image generation isn't available on this API key/tier (got a {response.status_code}). "
                    "This model may require billing enabled on the Google Cloud project, or may not be "
                    "enabled for this account yet - check aistudio.google.com or Cloud Console. "
                    f"Raw error: {raw_error}"
                )
            }
        if response.status_code == 404:
            return {
                "error": (
                    f"Image generation model '{MODEL}' not found - the model ID may have changed. "
                    "Check current model names at Google's Gemini API docs."
                )
            }

        try:
            data = response.json()
        except ValueError:
            data = {}
        if not 200 <= response.status_code < 300:
            return {"error": f"Image generation API error ({response.status_code}): {_response_preview(response)}"}

        image_part = _find_image_part(data)
        if image_part is None:
            return {"error": "Image generation returned no image data."}

        mime_type = image_part.get("mimeType", "")
        encoded_data = image_part.get("data")
        if not mime_type or not encoded_data:
            return {"error": "Image generation returned incomplete image data."}

        try:
            image_bytes = base64.b64decode(encoded_data, validate=True)
        except (ValueError, TypeError):
            return {"error": "Image generation returned invalid base64 image data."}

        output_dir = self._workspace_root() / "generated_images"
        output_dir.mkdir(parents=True, exist_ok=True)
        output_path = output_dir / f"{datetime.now(timezone.utc).strftime('%Y%m%d_%H%M%S_%f')}.{_extension(mime_type)}"
        output_path.write_bytes(image_bytes)
        return {"path": output_path.relative_to(self._workspace_root()).as_posix(), "mimeType": mime_type}

    def _workspace_root(self) -> Path:
        config_path = self.root / "config.yaml"
        if config_path.exists():
            with config_path.open("r", encoding="utf-8") as handle:
                config = yaml.safe_load(handle) or {}
            configured = config.get("filesystem_tool", {}).get("workspace_root", "workspace")
        else:
            configured = "workspace"
        workspace_root = Path(configured)
        return workspace_root if workspace_root.is_absolute() else self.root / workspace_root


def _find_image_part(data: dict[str, Any]) -> dict[str, Any] | None:
    for candidate in data.get("candidates", []):
        for part in candidate.get("content", {}).get("parts", []):
            inline_data = part.get("inlineData")
            if isinstance(inline_data, dict):
                return inline_data
    return None


def _extension(mime_type: str) -> str:
    subtype = mime_type.partition("/")[2].split(";", 1)[0].lower()
    subtype = subtype.removesuffix("+xml")
    return {"jpeg": "jpg", "svg": "svg"}.get(subtype, re.sub(r"[^a-z0-9]+", "", subtype) or "bin")


def _response_preview(response: requests.Response) -> str:
    try:
        body = response.json()
        raw = str(body)
    except ValueError:
        raw = response.text
    return " ".join(raw.split())[:200]


def register(confirm_fn=None) -> list[Tool]:
    _ = confirm_fn
    return [GenerateImageTool()]
