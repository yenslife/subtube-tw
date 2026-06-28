from __future__ import annotations

import os
from dataclasses import dataclass

from openai import OpenAI

LLM_TIMEOUT_SECONDS = float(os.getenv("LLM_TIMEOUT_SECONDS", "120"))


@dataclass(frozen=True)
class LLMConfig:
    provider: str
    model: str
    summary_model: str
    base_url: str | None = None


def get_llm_config() -> LLMConfig:
    provider = os.getenv("LLM_PROVIDER", "openai").strip().lower()

    if provider == "openrouter":
        model = os.getenv("OPENROUTER_MODEL", "openai/gpt-4.1-mini")
        return LLMConfig(
            provider=provider,
            model=model,
            summary_model=os.getenv("OPENROUTER_SUMMARY_MODEL", model),
            base_url=os.getenv("OPENROUTER_BASE_URL", "https://openrouter.ai/api/v1"),
        )

    if provider != "openai":
        raise ValueError(
            f"Unsupported LLM_PROVIDER={provider!r}. Expected 'openai' or 'openrouter'."
        )

    model = os.getenv("OPENAI_MODEL", "gpt-4.1-mini")
    return LLMConfig(
        provider=provider,
        model=model,
        summary_model=os.getenv("OPENAI_SUMMARY_MODEL", model),
    )


def make_llm_client(config: LLMConfig | None = None) -> OpenAI:
    config = config or get_llm_config()

    if config.provider == "openrouter":
        api_key = os.getenv("OPENROUTER_API_KEY")
        if not api_key:
            raise RuntimeError("LLM_PROVIDER=openrouter requires OPENROUTER_API_KEY.")
        headers = {}
        if referer := os.getenv("OPENROUTER_HTTP_REFERER"):
            headers["HTTP-Referer"] = referer
        if title := os.getenv("OPENROUTER_APP_TITLE", "SubTube TW"):
            headers["X-Title"] = title
        return OpenAI(
            api_key=api_key,
            base_url=config.base_url,
            default_headers=headers or None,
            timeout=LLM_TIMEOUT_SECONDS,
        )

    api_key = os.getenv("OPENAI_API_KEY")
    if not api_key:
        raise RuntimeError("LLM_PROVIDER=openai requires OPENAI_API_KEY.")
    return OpenAI(api_key=api_key, timeout=LLM_TIMEOUT_SECONDS)
