"""The LLMClient interface every backend implements, and the errors backends raise."""

from __future__ import annotations

import threading
from typing import Protocol

from pydantic import BaseModel


class Usage(BaseModel):
    """Tokens for one or more calls.

    `tokens_in` = uncached input + cache writes. `cache_tokens` = cache reads only,
    which the token budget excludes (D5)."""

    tokens_in: int = 0
    tokens_out: int = 0
    cache_tokens: int = 0

    def __add__(self, other: Usage) -> Usage:
        return Usage(
            tokens_in=self.tokens_in + other.tokens_in,
            tokens_out=self.tokens_out + other.tokens_out,
            cache_tokens=self.cache_tokens + other.cache_tokens,
        )


class LLMResponse(BaseModel):
    text: str
    usage: Usage = Usage()
    latency_s: float = 0.0


class LLMError(RuntimeError):
    """One LLM call failed. The agent loop counts it as a failed attempt."""


class LLMTimeoutError(LLMError):
    """One LLM call ran past its timeout."""


class LLMFatalError(LLMError):
    """An LLM error that must stop the whole run, not count as one failed attempt.

    Retrying soon would fail the same way, and finishing the run would only produce a
    report built from failed calls. The agent loop, `ask`, the graph and the judge let
    it propagate out of `investigate()`, so the CLI, the API and the eval runner stop
    cleanly (the eval runner prints the resume command)."""


class LLMRateLimitError(LLMFatalError):
    """The backend refused the call because of a rate limit (HTTP 429). Resume later."""


class LLMAuthError(LLMFatalError):
    """The backend rejected the credentials (HTTP 401 or 403). Fix the key, then resume."""


class LLMUnavailableError(RuntimeError):
    """The backend cannot run at all (no API key, no server). Raised before a run starts."""


class LLMClient(Protocol):
    def preflight(self) -> None:
        """Raise LLMUnavailableError if this backend cannot serve calls."""
        ...

    def complete(self, *, system: str, prompt: str, model: str) -> LLMResponse: ...


# Shared by every backend in the process (Q20): each call times out after 120 s,
# and at most 3 LLM calls are in flight at once, whichever backends serve them.
CALL_TIMEOUT_S = 120
MAX_IN_FLIGHT = 3
IN_FLIGHT = threading.BoundedSemaphore(MAX_IN_FLIGHT)
