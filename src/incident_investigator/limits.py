"""Run limits (L26, Q16, D5): one config object and the guard that enforces it.

Both systems (the multi-agent graph and the single-agent baseline) read the same
`RunLimits`, so they are compared under identical budgets:

    from incident_investigator.limits import DEFAULT_LIMITS, RunGuard

`RunGuard` enforces the per-run limits for one investigation. Every LLM call goes
through `GuardedLLM` (or calls `before_llm`/`after_llm` itself) and every tool call
through `RunGuard.tool_call`. The first limit hit sets `terminated_by`.

- Tool budget: the call that would exceed `max_tool_calls_per_run` is refused (not
  made, not counted) and trips `tool_budget`. It stops tool calls only: agents still
  get to finish from the evidence they have, pending Reasoning tasks still run, and
  Reporting writes the report. No new investigator task or Critic loop starts.
- Token budget and wall clock are hard stops: after one trips, any further LLM or tool
  call raises `RunStopped`, the agent loop stops cleanly, and the run goes straight to
  Reporting.
- Token budget (D5): input + output tokens, cache reads excluded. The call that
  reaches the budget completes (it is already paid for) and trips `token_budget`;
  no new LLM call starts.
- Wall clock: checked before every LLM call, tool call and scheduling step against
  the injectable `clock`. A call already in flight is bounded by the per-call LLM
  timeout (120 s), not interrupted.
"""

from __future__ import annotations

import threading
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict

from incident_investigator.llm.base import LLMClient, LLMResponse, Usage

TerminatedBy = Literal["done", "tool_budget", "token_budget", "timeout", "critic_limit"]
Clock = Callable[[], datetime]


def utc_now() -> datetime:
    return datetime.now(UTC)


class RunLimits(BaseModel):
    """Every limit of one investigation. The defaults are the locked values (L26, Q16)."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    max_tool_calls_per_task: int = 6
    max_tool_calls_per_run: int = 30
    max_tokens_per_run: int = 300_000
    """Input plus output tokens, excluding cache reads (D5)."""
    wall_clock_s: float = 600.0
    max_critic_loops: int = 2
    max_failed_attempts: int = 2
    """Failed attempts (invalid reply, LLM error or timeout) before an AgentFailure."""


DEFAULT_LIMITS = RunLimits()


class RunStopped(Exception):  # noqa: N818 - a stop signal, not an error
    """A run limit was hit; the caller stops its work and keeps what it has."""

    def __init__(self, reason: TerminatedBy) -> None:
        super().__init__(f"run stopped: {reason}")
        self.reason = reason


class RunGuard:
    """Tracks one run's tool calls, tokens and elapsed time. Thread-safe."""

    def __init__(self, limits: RunLimits = DEFAULT_LIMITS, *, clock: Clock = utc_now) -> None:
        self.limits = limits
        self.clock = clock
        self.started_at = clock()
        self.tool_calls = 0
        self.tokens = 0
        self._stop: TerminatedBy | None = None
        """A hard stop (token budget or wall clock): no more LLM or tool calls."""
        self._tools_spent = False
        self._lock = threading.Lock()

    @property
    def stop(self) -> TerminatedBy | None:
        """The limit that ended the run (`terminated_by`): a hard stop, else `tool_budget`
        once the tool budget refused a call, else None."""
        return self._stop or ("tool_budget" if self._tools_spent else None)

    def elapsed_s(self) -> float:
        return round((self.clock() - self.started_at).total_seconds(), 3)

    def _trip(self, reason: TerminatedBy) -> None:
        if self._stop is None:
            self._stop = reason

    def _poll_locked(self) -> TerminatedBy | None:
        if self._stop is None and self.elapsed_s() >= self.limits.wall_clock_s:
            self._trip("timeout")
        return self._stop

    def poll(self) -> TerminatedBy | None:
        """Check the wall clock; return the hard stop reason, if any. Never raises."""
        with self._lock:
            return self._poll_locked()

    def check(self) -> None:
        """Raise RunStopped on a hard stop (token budget or wall clock)."""
        with self._lock:
            if (reason := self._poll_locked()) is not None:
                raise RunStopped(reason)

    def before_llm(self) -> None:
        self.check()

    def after_llm(self, usage: Usage) -> None:
        with self._lock:
            self.tokens += usage.tokens_in + usage.tokens_out
            if self.tokens >= self.limits.max_tokens_per_run:
                self._trip("token_budget")

    def tool_calls_left(self) -> int:
        with self._lock:
            return max(0, self.limits.max_tool_calls_per_run - self.tool_calls)

    def tools_spent(self) -> bool:
        """True, and trips `tool_budget`, when no tool call is left in the run. Only the
        Critic routing asks: a follow-up loop needs tools, a pending Reasoning task does not."""
        with self._lock:
            if self.tool_calls < self.limits.max_tool_calls_per_run:
                return False
            self._tools_spent = True
            return True

    def tool_call[R](self, dispatch: Callable[[], R]) -> R:
        """Make one tool call if the run budget allows it; otherwise trip and raise."""
        with self._lock:
            if (reason := self._poll_locked()) is not None:
                raise RunStopped(reason)
            if self.tool_calls >= self.limits.max_tool_calls_per_run:
                self._tools_spent = True
                raise RunStopped("tool_budget")
            self.tool_calls += 1
        return dispatch()


class GuardedLLM:
    """An LLMClient that checks the guard before each call and counts its tokens after."""

    def __init__(self, inner: LLMClient, guard: RunGuard) -> None:
        self.inner = inner
        self.guard = guard

    def preflight(self) -> None:
        self.inner.preflight()

    def complete(self, *, system: str, prompt: str, model: str) -> LLMResponse:
        self.guard.before_llm()
        response = self.inner.complete(system=system, prompt=prompt, model=model)
        self.guard.after_llm(response.usage)
        return response
