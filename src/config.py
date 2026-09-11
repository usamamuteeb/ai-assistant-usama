"""Loads config.yaml + .env into one Settings object used across the app."""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent


@dataclass
class Settings:
    raw: dict[str, Any]
    root: Path = ROOT

    # --- secrets from .env ---
    anthropic_api_key: str | None = None
    openai_api_key: str | None = None
    ollama_host_env: str | None = None
    groq_api_key: str | None = None

    def __post_init__(self) -> None:
        self.anthropic_api_key = os.getenv("ANTHROPIC_API_KEY") or None
        self.openai_api_key = os.getenv("OPENAI_API_KEY") or None
        self.ollama_host_env = os.getenv("OLLAMA_HOST") or None
        self.groq_api_key = os.getenv("GROQ_API_KEY") or None

    # --- convenience accessors over the yaml tree ---
    @property
    def premium_model(self) -> dict[str, Any]:
        return self.raw["models"]["premium"]

    @property
    def local_model(self) -> dict[str, Any]:
        cfg = dict(self.raw["models"]["local"])
        if self.ollama_host_env:
            cfg["host"] = self.ollama_host_env
        return cfg

    @property
    def free_api_model(self) -> dict[str, Any]:
        cfg = dict(self.raw["models"]["free_api"])
        chain = cfg.get("chain")
        if chain is None and cfg.get("model"):
            # Preserve compatibility with existing single-model configurations.
            chain = [cfg["model"]]
        cfg["chain"] = [str(model) for model in chain or [] if str(model).strip()]
        return cfg

    @property
    def routing(self) -> dict[str, Any]:
        return self.raw["routing"]

    @property
    def memory(self) -> dict[str, Any]:
        return self.raw["memory"]

    @property
    def filesystem_tool(self) -> dict[str, Any]:
        return self.raw["filesystem_tool"]

    @property
    def shell_tool(self) -> dict[str, Any]:
        return self.raw["shell_tool"]

    @property
    def scheduled_tasks(self) -> list[dict[str, Any]]:
        return self.raw.get("scheduled_tasks", [])

    def workspace_root(self) -> Path:
        p = self.root / self.filesystem_tool["workspace_root"]
        p.mkdir(parents=True, exist_ok=True)
        return p

    def sqlite_path(self) -> Path:
        p = self.root / self.memory["sqlite_path"]
        p.parent.mkdir(parents=True, exist_ok=True)
        return p

    def chroma_path(self) -> Path:
        p = self.root / self.memory["chroma_path"]
        p.mkdir(parents=True, exist_ok=True)
        return p


def load_settings(config_path: str | Path | None = None) -> Settings:
    load_dotenv(ROOT / ".env")
    path = Path(config_path) if config_path else ROOT / "config.yaml"
    with open(path, "r", encoding="utf-8") as f:
        raw = yaml.safe_load(f)
    return Settings(raw=raw)
