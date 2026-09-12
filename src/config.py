"""Loads config.yaml + .env into one Settings object used across the app."""
from __future__ import annotations

import os
from dataclasses import dataclass
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
    gemini_api_key: str | None = None

    def __post_init__(self) -> None:
        self.anthropic_api_key = os.getenv("ANTHROPIC_API_KEY") or None
        self.openai_api_key = os.getenv("OPENAI_API_KEY") or None
        self.ollama_host_env = os.getenv("OLLAMA_HOST") or None
        self.groq_api_key = os.getenv("GROQ_API_KEY") or None
        self.gemini_api_key = os.getenv("GEMINI_API_KEY") or None

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
        cfg = dict(self.raw["models"].get("free_api", {}))
        chain = cfg.get("chain") or []
        if not isinstance(chain, list):
            chain = [chain]

        default_provider = self.raw["models"].get("groq", {}).get("provider") or cfg.get("provider") or "groq"
        normalized: list[dict[str, str]] = []
        for entry in chain:
            if isinstance(entry, str):
                model = entry.strip()
                if model:
                    normalized.append({"provider": default_provider, "model": model})
                continue
            if isinstance(entry, dict):
                provider = str(entry.get("provider") or default_provider).strip() or default_provider
                model = str(entry.get("model") or "").strip()
                if provider and model:
                    normalized.append({"provider": provider, "model": model})

        cfg["chain"] = normalized
        if not normalized and cfg.get("model"):
            cfg["chain"] = [{"provider": default_provider, "model": str(cfg["model"]).strip()}]
        return cfg

    @property
    def routing(self) -> dict[str, Any]:
        routing = dict(self.raw["routing"])
        routing["allow_premium"] = self.allow_premium
        return routing

    @property
    def allow_premium(self) -> bool:
        value = self.raw.get("routing", {}).get("allow_premium", True)
        if isinstance(value, str):
            return value.strip().lower() not in {"false", "0", "no"}
        return bool(value)

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
    def tool_relevance_filter(self) -> dict[str, Any]:
        cfg = dict(self.raw.get("tool_relevance_filter", {}))
        cfg["enabled"] = bool(cfg.get("enabled", False))
        cfg["max_tools"] = int(cfg.get("max_tools", 10))
        cfg["min_tools"] = int(cfg.get("min_tools", 6))
        cfg["core_tools"] = {str(name) for name in cfg.get("core_tools", [])}
        return cfg

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
