"""Core Pydantic schemas (PRD data model sketch, D2).

Everything here is agent-visible. The evaluation-only key schema lives in
dataset.py, next to its loader.
"""

from __future__ import annotations

import re
from typing import Any, Literal

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, model_validator

Category = Literal[
    "bad_deploy",
    "config_change",
    "dependency_failure",
    "capacity",
    "cert_dns",
    "db_migration",
    "retry_storm",
    "feature_flag",
    "third_party_limit",
]
ToolName = Literal["logs", "metrics", "deploys", "dependencies", "past_incidents"]
Confidence = Field(ge=0.0, le=1.0)


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


# --- Scenario (agent-visible data) -------------------------------------------


class IncidentReport(_Strict):
    title: str
    symptoms: list[str]
    affected_service: str
    window_start: AwareDatetime
    window_end: AwareDatetime
    severity: Literal["SEV1", "SEV2", "SEV3", "SEV4"]


class LogRow(_Strict):
    id: str
    timestamp: AwareDatetime
    service: str
    level: Literal["DEBUG", "INFO", "WARN", "ERROR"]
    message: str


class MetricRow(_Strict):
    id: str
    timestamp: AwareDatetime
    service: str
    name: str
    value: float


class DeployRow(_Strict):
    id: str
    timestamp: AwareDatetime
    service: str
    version: str
    description: str = ""


class ConfigChangeRow(_Strict):
    id: str
    timestamp: AwareDatetime
    service: str
    key: str
    old_value: str
    new_value: str


class DependencyRow(_Strict):
    """One edge of the service graph: `service` calls `depends_on`."""

    id: str
    service: str
    depends_on: str


class PastIncidentRow(_Strict):
    id: str
    title: str
    tags: list[str]
    root_cause: str
    resolution: str


class FailureSpec(_Strict):
    tool: ToolName
    call_index: int = Field(ge=1)
    mode: Literal["timeout", "partial", "error"]
    partial_fraction: float | None = Field(default=None, gt=0.0, lt=1.0)


class Scenario(_Strict):
    id: str = Field(pattern=r"^inc\d{2}$")
    report: IncidentReport
    logs: list[LogRow] = []
    metrics: list[MetricRow] = []
    deploys: list[DeployRow] = []
    config_changes: list[ConfigChangeRow] = []
    dependencies: list[DependencyRow] = []
    past_incidents: list[PastIncidentRow] = []
    failure_schedule: list[FailureSpec] = []

    def _rows(self) -> list[tuple[str, str]]:
        groups: list[tuple[str, list[Any]]] = [
            ("log", self.logs),
            ("metric", self.metrics),
            ("deploy", self.deploys),
            ("config", self.config_changes),
            ("dependency", self.dependencies),
            ("past", self.past_incidents),
        ]
        return [(kind, row.id) for kind, rows in groups for row in rows]

    def evidence_ids(self) -> set[str]:
        return {row_id for _, row_id in self._rows()}

    def services(self) -> set[str]:
        """Every service this scenario knows about; tools reject any other name."""
        named: list[Any] = [*self.logs, *self.metrics, *self.deploys, *self.config_changes]
        edges = {svc for row in self.dependencies for svc in (row.service, row.depends_on)}
        return {self.report.affected_service, *(row.service for row in named)} | edges

    @model_validator(mode="after")
    def _evidence_ids_are_stable_and_unique(self) -> Scenario:
        seen: set[str] = set()
        for kind, row_id in self._rows():
            # <kind>:<incident>[:<series>]:NNNN, e.g. log:incNN:0003 or metric:incNN:cpu:0001
            if not re.fullmatch(rf"{kind}:{self.id}:(?:[a-z0-9_-]+:)?\d{{4}}", row_id):
                raise ValueError(
                    f"evidence id {row_id!r} must look like {kind}:{self.id}[:<series>]:NNNN"
                )
            if row_id in seen:
                raise ValueError(f"duplicate evidence id {row_id!r}")
            seen.add(row_id)
        return self


# --- Investigation ------------------------------------------------------------


class Evidence(_Strict):
    id: str
    kind: Literal["log", "metric", "deploy", "config", "past", "dependency"]
    timestamp: AwareDatetime | None = None
    payload: dict[str, Any]


class EvidenceGap(_Strict):
    id: str | None = None
    """Set by the ToolSession (`gap:<n>`), so a later successful query can close it."""
    tool: ToolName
    reason: str
    service: str | None = None
    """The service the failed query was about; None for a query over every service."""
    affects_hypotheses: list[str] = []
    task_id: str | None = None
    """The task whose tool call hit the gap, when known (set by the orchestrator)."""


class ToolResult(_Strict):
    status: Literal["ok", "partial", "timeout", "error"]
    evidence: list[Evidence] = []
    error: str | None = None
    attempts: int = Field(default=1, ge=1)
    omitted: int = Field(default=0, ge=0)
    """Rows left out of a `partial` result."""
    gap: EvidenceGap | None = None
    closes_gaps: list[str] = []
    """IDs of earlier gaps this successful call closes: same tool, same service."""
    """Set when an injected failure (timeout, partial, error) hid evidence; the caller
    records it on the blackboard. Invalid input never produces a gap."""


class Fact(_Strict):
    statement: str
    evidence_ids: list[str] = Field(min_length=1)


class Assumption(_Strict):
    statement: str
    basis: str
    how_to_verify: str


class Finding(_Strict):
    task_id: str
    agent: str
    facts: list[Fact] = []
    assumptions: list[Assumption] = []


class Hypothesis(_Strict):
    id: str
    service: str
    category: Category
    summary: str
    supporting_ids: list[str] = []
    contradicting_ids: list[str] = []
    proposed_confidence: float = Confidence
    final_confidence: float | None = Field(default=None, ge=0.0, le=1.0)


class RecommendedAction(_Strict):
    action: str
    rationale: str
    hypothesis_id: str


class ReportMetrics(_Strict):
    tool_calls: int = 0
    llm_calls: int = 0
    tokens_in: int = 0
    tokens_out: int = 0
    cache_tokens: int = 0
    latency_s: float = 0.0


class AgentFailure(_Strict):
    """An agent gave up after its failed-attempt limit (Q16). The run continues without it."""

    agent: str
    task_id: str
    reason: str
    attempts: int


class RuleAdjustment(_Strict):
    """One change the deterministic evidence rules made (ticket 05), kept for audit."""

    rule: Literal[
        "fact_demoted",
        "hypothesis_ids_dropped",
        "unsupported_cap",
        "gap_penalty",
        "gap_closed",
        "action_dropped",
    ]
    subject: str
    """What changed: `<task_id>/fact[<n>]`, a hypothesis id, or `action[<n>]`."""
    detail: str
    ids: list[str] = []
    """The evidence IDs (or, for `gap_penalty`, the gap) behind the change."""
    confidence_before: float | None = None
    confidence_after: float | None = None


class InvestigationReport(_Strict):
    incident_id: str
    outcome: Literal["ROOT_CAUSE", "INCONCLUSIVE"]
    ranked_hypotheses: list[Hypothesis]
    facts: list[Fact]
    assumptions: list[Assumption]
    recommended_actions: list[RecommendedAction] = []
    gaps: list[EvidenceGap] = []
    metrics: ReportMetrics
    terminated_by: Literal["done", "tool_budget", "token_budget", "timeout", "critic_limit"]
    rule_adjustments: list[RuleAdjustment] = []
    """Every change the evidence rules made to the agents' Facts, Hypotheses and actions."""
    failures: list[AgentFailure] = []
    """Agents that gave up after 2 failed attempts; the run continued without them (Q16)."""
