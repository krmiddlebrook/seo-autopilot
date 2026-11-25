"""LLM providers behind one interface: Ollama (free, local), Anthropic, any OpenAI-compatible API, signed-in agent
CLIs (Claude Code, Codex, Gemini), and a fake for tests."""

from __future__ import annotations

from typing import Any, Callable, Protocol

import httpx

from .config import LLMConfig, secret
from .textutil import clean_model_text, extract_json


class LLM(Protocol):
    def complete(self, system: str, prompt: str, json_mode: bool = False, max_tokens: int | None = None) -> str: ...


def complete_json(llm: LLM, system: str, prompt: str, retries: int = 1) -> Any:
    """Ask for JSON and parse it, re-asking once with the parse error if the model fumbles the format."""
    last_err: Exception | None = None
    for _ in range(retries + 1):
        raw = llm.complete(system, prompt, json_mode=True)
        try:
            return extract_json(raw)
        except (ValueError, TypeError) as err:
            last_err = err
            prompt = f"{prompt}\n\nYour previous reply was not valid JSON ({err}). Reply with JSON only."
    raise RuntimeError(f"Model did not return valid JSON: {last_err}")


def complete_text(llm: LLM, system: str, prompt: str, max_tokens: int | None = None) -> str:
    return clean_model_text(llm.complete(system, prompt, max_tokens=max_tokens))


class OllamaLLM:
    def __init__(self, cfg: LLMConfig, client: httpx.Client):
        self.cfg, self.client = cfg, client
        self.base = (cfg.base_url or "http://localhost:11434").rstrip("/")

    def complete(self, system: str, prompt: str, json_mode: bool = False, max_tokens: int | None = None) -> str:
        body: dict[str, Any] = {
            "model": self.cfg.model,
            "messages": [{"role": "system", "content": system}, {"role": "user", "content": prompt}],
            "stream": False,
            "options": {"temperature": self.cfg.temperature, "num_predict": max_tokens or self.cfg.max_tokens},
        }
        if json_mode:
            body["format"] = "json"
        r = self.client.post(f"{self.base}/api/chat", json=body, timeout=self.cfg.timeout)
        r.raise_for_status()
        return r.json()["message"]["content"]


class AnthropicLLM:
    def __init__(self, cfg: LLMConfig, client: httpx.Client):
        self.cfg, self.client = cfg, client
        self.base = (cfg.base_url or "https://api.anthropic.com").rstrip("/")
        self.key = secret(cfg.api_key_env or "ANTHROPIC_API_KEY")

    def complete(self, system: str, prompt: str, json_mode: bool = False, max_tokens: int | None = None) -> str:
        if json_mode:
            system += "\nRespond with a single valid JSON value and nothing else."
        r = self.client.post(
            f"{self.base}/v1/messages",
            headers={"x-api-key": self.key, "anthropic-version": "2023-06-01"},
            json={
                "model": self.cfg.model,
                "system": system,
                "max_tokens": max_tokens or self.cfg.max_tokens,
                "temperature": self.cfg.temperature,
                "messages": [{"role": "user", "content": prompt}],
            },
            timeout=self.cfg.timeout,
        )
        r.raise_for_status()
        return "".join(b.get("text", "") for b in r.json()["content"] if b.get("type") == "text")


class OpenAICompatLLM:
    """OpenAI, Gemini (OpenAI-compatible endpoint), OpenRouter, LM Studio, vLLM, llama.cpp server..."""

    def __init__(self, cfg: LLMConfig, client: httpx.Client):
        self.cfg, self.client = cfg, client
        self.base = (cfg.base_url or "https://api.openai.com/v1").rstrip("/")
        self.key = secret(cfg.api_key_env, required=False) if cfg.api_key_env else ""

    def complete(self, system: str, prompt: str, json_mode: bool = False, max_tokens: int | None = None) -> str:
        body: dict[str, Any] = {
            "model": self.cfg.model,
            "messages": [{"role": "system", "content": system}, {"role": "user", "content": prompt}],
            "temperature": self.cfg.temperature,
            "max_tokens": max_tokens or self.cfg.max_tokens,
        }
        if json_mode:
            body["response_format"] = {"type": "json_object"}
        headers = {"Authorization": f"Bearer {self.key}"} if self.key else {}
        r = self.client.post(f"{self.base}/chat/completions", headers=headers, json=body, timeout=self.cfg.timeout)
        r.raise_for_status()
        return r.json()["choices"][0]["message"]["content"] or ""


class FakeLLM:
    """Deterministic LLM for tests and `--dry-run` smoke checks. `responder(system, prompt, json_mode)` -> str."""

    def __init__(self, responder: Callable[[str, str, bool], str]):
        self.responder = responder
        self.calls: list[tuple[str, str, bool]] = []

    def complete(self, system: str, prompt: str, json_mode: bool = False, max_tokens: int | None = None) -> str:
        self.calls.append((system, prompt, json_mode))
        return self.responder(system, prompt, json_mode)


def make_llm(cfg: LLMConfig, client: httpx.Client) -> LLM:
    provider = cfg.provider.lower()
    if provider == "ollama":
        return OllamaLLM(cfg, client)
    if provider == "anthropic":
        return AnthropicLLM(cfg, client)
    if provider in ("openai", "openai-compatible", "gemini", "openrouter"):
        return OpenAICompatLLM(cfg, client)
    if provider in ("agent", "agents"):
        from .agents import AgentCLILLM

        return AgentCLILLM(cfg)
    if provider == "fake":
        from .fake import fake_responder

        return FakeLLM(fake_responder)
    raise ValueError(f"Unknown LLM provider '{cfg.provider}'")
