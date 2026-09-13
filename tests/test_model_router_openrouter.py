from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import yaml

from src.model_router import ChatResult, ModelRouter, OpenAICompatibleBackend


def _settings(chain):
    return SimpleNamespace(
        groq_api_key="groq-key",
        gemini_api_key="gemini-key",
        openrouter_api_key="openrouter-key",
        free_api_model={"max_tokens": 128, "chain": chain},
        raw={"models": {"groq": {"base_url": "https://groq.test/v1"}}},
        routing={"premium_keywords": [], "long_message_char_threshold": 400, "default_tier": "free_api"},
        allow_premium=True,
    )


def _response(payload):
    response = MagicMock(status_code=200, headers={})
    response.json.return_value = payload
    return response


def test_config_removes_decommissioned_models_and_keeps_qwen_then_openrouter_last():
    config = yaml.safe_load((Path(__file__).parents[1] / "config.yaml").read_text(encoding="utf-8"))
    chain = config["models"]["free_api"]["chain"]
    models = [entry["model"] for entry in chain]
    assert "llama-3.3-70b-versatile" not in models
    assert "llama-3.1-8b-instant" not in models
    assert "qwen/qwen3.6-27b" in models
    assert chain[-1] == {"provider": "openrouter", "model": "google/gemma-4-26b-a4b-it:free"}
    assert all(entry["provider"] == "openrouter" for entry in chain[7:])


def test_forced_groq_qwen_entry_uses_generic_openai_compatible_call():
    router = ModelRouter(_settings([{"provider": "groq", "model": "qwen/qwen3.6-27b"}]))
    payload = {"choices": [{"finish_reason": "stop", "message": {"content": "qwen response"}}]}
    with patch("src.model_router.requests.post", return_value=_response(payload)) as post:
        result = router.chat("free_api", [{"role": "user", "content": "hello"}], force_chain_start_index=0)
    assert result.text == "qwen response"
    assert result.model_used == "groq:qwen/qwen3.6-27b"
    assert post.call_args.args[0] == "https://groq.test/v1/chat/completions"
    assert post.call_args.kwargs["json"]["model"] == "qwen/qwen3.6-27b"


def test_forced_openrouter_entry_uses_openrouter_endpoint():
    chain = [
        {"provider": "gemini", "model": "first"},
        {"provider": "groq", "model": "second"},
        {"provider": "openrouter", "model": "nex-agi/nex-n2.5-pro:free"},
    ]
    router = ModelRouter(_settings(chain))
    payload = {"choices": [{"finish_reason": "stop", "message": {"content": "openrouter response"}}]}
    with patch("src.model_router.requests.post", return_value=_response(payload)) as post:
        result = router.chat("free_api", [{"role": "user", "content": "hello"}], force_chain_start_index=2)
    assert result.text == "openrouter response"
    assert result.model_used == "openrouter:nex-agi/nex-n2.5-pro:free"
    assert post.call_args.args[0] == "https://openrouter.ai/api/v1/chat/completions"
    assert post.call_args.kwargs["headers"]["Authorization"] == "Bearer openrouter-key"
    assert post.call_args.kwargs["json"]["model"] == "nex-agi/nex-n2.5-pro:free"


def test_normal_auto_routing_starts_at_first_chain_entry():
    chain = [
        {"provider": "gemini", "model": "first"},
        {"provider": "groq", "model": "second"},
        {"provider": "openrouter", "model": "last"},
    ]
    router = ModelRouter(_settings(chain))
    backend = MagicMock()
    backend.chat.return_value = ChatResult(text="first response")
    with patch.object(router, "_get_free_api", return_value=backend) as get_backend:
        result = router.chat("free_api", [{"role": "user", "content": "hello"}])
    get_backend.assert_called_once_with("gemini", "first")
    assert result.model_used == "gemini:first"


def test_generic_backend_constructor_is_provider_agnostic():
    backend = OpenAICompatibleBackend("https://provider.test/v1", "key", "model-x")
    assert backend.base_url == "https://provider.test/v1"
    assert backend.model == "model-x"
