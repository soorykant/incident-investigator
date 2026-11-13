"""`openai_compat` backend: a local model (Ollama, LM Studio, vLLM) via `/v1/chat/completions` (Q20).

Plain httpx, not the openai SDK, to keep the dependency light. The base URL is
`$OPENAI_COMPAT_BASE_URL`, else the models config value, else Ollama's default.
`$OPENAI_COMPAT_API_KEY` is sent as a bearer token when set (vLLM `--api-key`).
"""

from __future__ import annotations

import os
import time
from typing import Any

import httpx

from incident_investigator.llm.base import (
    CALL_TIMEOUT_S,
    IN_FLIGHT,
    LLMError,
    LLMResponse,
    LLMTimeoutError,
    LLMUnavailableError,
    Usage,
)

DEFAULT_BASE_URL = "http://localhost:11434/v1"
BASE_URL_ENV = "OPENAI_COMPAT_BASE_URL"
API_KEY_ENV = "OPENAI_COMPAT_API_KEY"
_PREFLIGHT_TIMEOUT_S = 5


class OpenAICompat:
    def __init__(
        self,
        *,
        base_url: str | None = None,
        timeout_s: int = CALL_TIMEOUT_S,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        self.base_url = (os.environ.get(BASE_URL_ENV) or base_url or DEFAULT_BASE_URL).rstrip("/")
        self.timeout_s = timeout_s
        api_key = os.environ.get(API_KEY_ENV)
        headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}
        self._http = httpx.Client(headers=headers, transport=transport)

    def preflight(self) -> None:
        url = f"{self.base_url}/models"
        hint = (
            f"Start the local model server (e.g. `ollama serve`) or set {BASE_URL_ENV} "
            "or [backends.openai_compat] base_url in models.toml."
        )
        try:
            response = self._http.get(url, timeout=_PREFLIGHT_TIMEOUT_S)
        except httpx.HTTPError as exc:
            raise LLMUnavailableError(f"no OpenAI-compatible endpoint reachable at {self.base_url} ({exc}). {hint}") from exc
        if response.status_code != 200:
            raise LLMUnavailableError(
                f"{url} answered HTTP {response.status_code}; is {self.base_url} an OpenAI-compatible API? {hint}"
            )

    def complete(self, *, system: str, prompt: str, model: str) -> LLMResponse:
        body = {
            "model": model,
            "stream": False,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": prompt},
            ],
        }
        with IN_FLIGHT:
            started = time.monotonic()
            try:
                response = self._http.post(f"{self.base_url}/chat/completions", json=body, timeout=self.timeout_s)
            except httpx.TimeoutException as exc:
                raise LLMTimeoutError(f"openai_compat call timed out after {self.timeout_s} s: {exc}") from exc
            except httpx.HTTPError as exc:
                raise LLMError(f"openai_compat call to {self.base_url} failed: {exc}") from exc
            wall_s = time.monotonic() - started
        if response.status_code != 200:
            raise LLMError(f"openai_compat returned HTTP {response.status_code}: {response.text[:500]}")
        return _parse(response, wall_s)


def _parse(response: httpx.Response, wall_s: float) -> LLMResponse:
    try:
        payload: Any = response.json()
        text = payload["choices"][0]["message"]["content"]
    except (ValueError, KeyError, IndexError, TypeError) as exc:
        raise LLMError(f"openai_compat reply is not a chat completion: {response.text[:500]}") from exc
    if not isinstance(text, str):
        raise LLMError(f"openai_compat reply has no text content: {response.text[:500]}")

    usage = payload.get("usage") or {}
    prompt_tokens = int(usage.get("prompt_tokens") or 0)
    # Some servers (vLLM with prefix caching) report cached prompt tokens. Like
    # anthropic_api, cache reads go to cache_tokens and are excluded from tokens_in (D5).
    cached = int((usage.get("prompt_tokens_details") or {}).get("cached_tokens") or 0)
    return LLMResponse(
        text=text,
        usage=Usage(
            tokens_in=prompt_tokens - cached,
            tokens_out=int(usage.get("completion_tokens") or 0),
            cache_tokens=cached,
        ),
        latency_s=round(wall_s, 3),
    )
