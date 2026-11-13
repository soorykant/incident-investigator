"""Tool registry and dispatcher, with deterministic failure injection (Q5, Q16).

Agents call tools by name with a raw args dict. `ToolSession.call` validates the
args into the tool's typed input, runs the tool and applies the scenario's
`failure_schedule`. It never raises: bad input comes back as `status=error`.

Create one `ToolSession` per investigation run. It owns the per-tool call
counters, so the Nth call to a tool fails the same way on every run.

Failure semantics:
- The schedule counts accepted calls per tool, starting at 1. A call rejected as
  invalid input never reached the "backend", so it does not count.
- `timeout`: the scheduled call keeps timing out, so it is retried `retries` times
  with exponential backoff (backoff_s, 2*backoff_s, ...), then returns
  `status=timeout`, `attempts=retries+1` and an EvidenceGap.
- `partial`: the first floor(n * partial_fraction) rows (default fraction 0.5),
  `status=partial`, `omitted` set, and an EvidenceGap.
- `error`: `status=error` and an EvidenceGap. Not retried.

Each gap gets an ID (`gap:1`, `gap:2`, ... in call order) and the service its query
was about. A later call to the same tool for the same service (or, for `deploys` and
`past_incidents`, again over every service) that returns `status=ok` closes every open
gap with that key: the result lists them in `closes_gaps`. A partial result closes
nothing. The query may differ (another window or metric name): the gap was about
missing data for that service, and a successful query shows the backend answers again.
"""

from __future__ import annotations

import math
import threading
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any, cast, get_args

from pydantic import BaseModel, ValidationError

from incident_investigator.schemas import (
    EvidenceGap,
    FailureSpec,
    Scenario,
    ToolName,
    ToolResult,
)
from incident_investigator.tools._common import invalid
from incident_investigator.tools.dependencies import DependenciesQuery, query_dependencies
from incident_investigator.tools.deploys import DeploysQuery, query_deploys
from incident_investigator.tools.logs import LogsQuery, query_logs
from incident_investigator.tools.metrics import MetricsQuery, query_metrics
from incident_investigator.tools.past_incidents import PastIncidentsQuery, query_past_incidents

DEFAULT_PARTIAL_FRACTION = 0.5
DEFAULT_BACKOFF_S = 0.5
"""Base backoff before the first retry of a timed-out call; it doubles per retry."""


@dataclass(frozen=True)
class ToolSpec:
    name: ToolName
    description: str
    input_model: type[BaseModel]
    run: Callable[[Scenario, Any], ToolResult]


TOOL_SPECS: dict[ToolName, ToolSpec] = {
    spec.name: spec
    for spec in [
        ToolSpec(
            "logs",
            "Log lines for one service in a time window, optionally filtered by level.",
            LogsQuery,
            query_logs,
        ),
        ToolSpec(
            "metrics",
            "Metric samples for one service in a time window, optionally one metric name.",
            MetricsQuery,
            query_metrics,
        ),
        ToolSpec(
            "deploys",
            "Deployments and config changes in a time window, for one service or all.",
            DeploysQuery,
            query_deploys,
        ),
        ToolSpec(
            "dependencies",
            "Service-graph edges around a service: what it calls (downstream), "
            "what calls it (upstream), or both.",
            DependenciesQuery,
            query_dependencies,
        ),
        ToolSpec(
            "past_incidents",
            "Earlier incidents matching keywords (title, root cause, resolution) or tags.",
            PastIncidentsQuery,
            query_past_incidents,
        ),
    ]
}
assert set(TOOL_SPECS) == set(get_args(ToolName))


class ToolSession:
    """Tool access for one investigation run. Thread-safe."""

    def __init__(
        self,
        scenario: Scenario,
        *,
        retries: int = 2,
        backoff_s: float = DEFAULT_BACKOFF_S,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self._scenario = scenario
        self._retries = retries
        self._backoff_s = backoff_s
        self._sleep = sleep
        self._calls: dict[str, int] = {}
        self._gaps = 0
        self._open_gaps: dict[tuple[str, str | None], list[str]] = {}
        self._lock = threading.Lock()

    def call(self, tool: str, args: Mapping[str, Any]) -> ToolResult:
        """Dispatch by name with raw args, e.g. from an agent's `call_tool` action."""
        spec = TOOL_SPECS.get(cast(ToolName, tool))
        if spec is None:
            return invalid(f"unknown tool {tool!r} (known: {', '.join(TOOL_SPECS)})")
        try:
            query = spec.input_model.model_validate(dict(args))
        except ValidationError as exc:
            problems = "; ".join(
                f"{'.'.join(map(str, e['loc'])) or 'args'}: {e['msg']}" for e in exc.errors()
            )
            return invalid(f"{tool}: {problems}")
        return self.run(spec.name, query)

    def run(self, tool: ToolName, query: BaseModel) -> ToolResult:
        """Run a tool with an already-typed query, applying the failure schedule."""
        spec = TOOL_SPECS[tool]
        if not isinstance(query, spec.input_model):
            return invalid(f"{tool} expects {spec.input_model.__name__}")
        result = spec.run(self._scenario, query)
        if result.status == "error":
            return result  # invalid input: not a backend call, so not counted
        failure = self._next_failure(tool)
        key = (tool, cast(str | None, getattr(query, "service", None)))
        if failure is None:
            with self._lock:
                closed = self._open_gaps.pop(key, []) if result.status == "ok" else []
            return result.model_copy(update={"closes_gaps": closed}) if closed else result
        failed = self._inject(tool, failure, result)
        assert failed.gap is not None
        with self._lock:
            self._gaps += 1
            gap_id = f"gap:{self._gaps}"
            self._open_gaps.setdefault(key, []).append(gap_id)
        return failed.model_copy(update={"gap": failed.gap.model_copy(update={"id": gap_id, "service": key[1]})})

    def _next_failure(self, tool: ToolName) -> FailureSpec | None:
        with self._lock:
            index = self._calls.get(tool, 0) + 1
            self._calls[tool] = index
        return next(
            (f for f in self._scenario.failure_schedule if f.tool == tool and f.call_index == index),
            None,
        )

    def _inject(self, tool: ToolName, failure: FailureSpec, result: ToolResult) -> ToolResult:
        if failure.mode == "timeout":
            for retry in range(self._retries):
                self._sleep(self._backoff_s * 2**retry)
            attempts = self._retries + 1
            reason = f"timeout after {attempts} attempts"
            return ToolResult(
                status="timeout",
                error=reason,
                attempts=attempts,
                gap=EvidenceGap(tool=tool, reason=reason),
            )
        if failure.mode == "partial":
            fraction = failure.partial_fraction or DEFAULT_PARTIAL_FRACTION
            kept = math.floor(len(result.evidence) * fraction)
            omitted = len(result.evidence) - kept
            return ToolResult(
                status="partial",
                evidence=result.evidence[:kept],
                omitted=omitted,
                gap=EvidenceGap(
                    tool=tool,
                    reason=f"partial result: {omitted} of {len(result.evidence)} rows missing",
                ),
            )
        reason = "tool backend error"
        return ToolResult(status="error", error=reason, gap=EvidenceGap(tool=tool, reason=reason))
