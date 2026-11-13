"""`anthropic_api` backend: the Messages API through the official SDK, paid by `ANTHROPIC_API_KEY`.

The default backend for every role, and the one the published evaluation uses
(ADR 0002). Cache writes count as input tokens; cache reads are reported apart (D5).
A 429 rate limit raises LLMRateLimitError and a rejected key (401, 403) raises
LLMAuthError; both stop the run, so the eval runner stops cleanly and can resume. Any
other API error (including a 529 overload) is an ordinary failed attempt.
"""

from __future__ import annotations

import os
import time
from typing import Any

import anthropic

from incident_investigator.llm.base import (
    CALL_TIMEOUT_S,
    IN_FLIGHT,
    LLMAuthError,
    LLMError,
    LLMRateLimitError,
    LLMResponse,
    LLMTimeoutError,
    LLMUnavailableError,
    Usage,
)

API_KEY_ENV = "ANTHROPIC_API_KEY"
DEFAULT_MAX_TOKENS = 4096


class AnthropicAPI:
    def __init__(
        self,
        *,
        client: Any = None,
        timeout_s: int = CALL_TIMEOUT_S,
        max_tokens: int = DEFAULT_MAX_TOKENS,
    ) -> None:
        """`client` is anything with `.messages.create(...)`; tests inject a fake."""
        self._client = client
        self.timeout_s = timeout_s
        self.max_tokens = max_tokens

    def preflight(self) -> None:
        if self._client is None and not os.environ.get(API_KEY_ENV):
            raise LLMUnavailableError(
                f"{API_KEY_ENV} is not set; the anthropic_api backend needs an API key. "
                "Export it, or use --llm mock to run offline."
            )

    def _sdk(self) -> Any:
        if self._client is None:
            self.preflight()
            # No SDK retries: a failed call is one failed attempt for the agent loop.
            self._client = anthropic.Anthropic(
                api_key=os.environ[API_KEY_ENV], max_retries=0, timeout=self.timeout_s
            )
        return self._client

    def complete(self, *, system: str, prompt: str, model: str) -> LLMResponse:
        try:
            client = self._sdk()
        except LLMUnavailableError as exc:
            raise LLMError(str(exc)) from exc
        with IN_FLIGHT:
            started = time.monotonic()
            try:
                message = client.messages.create(
                    model=model,
                    max_tokens=self.max_tokens,
                    system=system,
                    messages=[{"role": "user", "content": prompt}],
                    timeout=self.timeout_s,
                )
            except anthropic.APITimeoutError as exc:
                raise LLMTimeoutError(f"anthropic_api call timed out after {self.timeout_s} s") from exc
            except (anthropic.AuthenticationError, anthropic.PermissionDeniedError) as exc:
                raise LLMAuthError(
                    f"the Anthropic API rejected {API_KEY_ENV} (HTTP {exc.status_code}); "
                    f"check the key and its permissions: {exc}"
                ) from exc
            except anthropic.RateLimitError as exc:
                raise LLMRateLimitError(f"anthropic_api rate limit reached: {exc}") from exc
            except anthropic.APIError as exc:
                raise LLMError(f"anthropic_api call failed: {exc}") from exc
            wall_s = time.monotonic() - started
        return _parse(message, wall_s)


def _count(usage: Any, name: str) -> int:
    return int(getattr(usage, name, 0) or 0)


def _parse(message: Any, wall_s: float) -> LLMResponse:
    text = "".join(block.text for block in message.content if getattr(block, "type", None) == "text")
    if not text:
        raise LLMError(f"anthropic_api reply has no text (stop_reason={getattr(message, 'stop_reason', None)})")
    usage = message.usage
    return LLMResponse(
        text=text,
        usage=Usage(
            # Cache writes are new input; cache reads are reported apart and
            # excluded from the budget (D5).
            tokens_in=_count(usage, "input_tokens") + _count(usage, "cache_creation_input_tokens"),
            tokens_out=_count(usage, "output_tokens"),
            cache_tokens=_count(usage, "cache_read_input_tokens"),
        ),
        latency_s=round(wall_s, 3),
    )
