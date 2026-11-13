"""The blackboard: the shared, typed investigation state (Q4, PRD data model).

It is the LangGraph state. Agents read it and write typed entries to it (Tasks,
Findings, Hypotheses, EvidenceGaps, AgentFailures, Evidence rows); they never pass
free text to each other. It never holds the Scenario: agents learn scenario data
only through tool results, and the Scenario stays inside the run's ToolSession.

Fields that parallel tasks write in the same graph step carry a reducer, so
concurrent updates merge instead of overwriting each other.
"""

from __future__ import annotations

import operator
from datetime import datetime
from typing import Annotated, Any, Literal, Protocol

from pydantic import AwareDatetime, BaseModel, ConfigDict

from incident_investigator.llm.base import Usage
from incident_investigator.limits import TerminatedBy
from incident_investigator.schemas import (
    AgentFailure,  # defined in schemas (the report lists failures); re-exported here
    Evidence,
    EvidenceGap,
    Finding,
    Hypothesis,
    IncidentReport,
    InvestigationReport,
)

AgentName = Literal["observability", "dependency", "reasoning"]
"""Agents the Orchestrator can give a task to."""
INVESTIGATORS: tuple[AgentName, ...] = ("observability", "dependency")
"""Agents that call tools and write Findings."""


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Task(_Strict):
    id: str
    agent: AgentName
    goal: str
    params: dict[str, Any] = {}
    depends_on: list[str] = []
    status: Literal["pending", "running", "done", "failed"] = "pending"
    origin: Literal["plan", "critic"] = "plan"
    loop: int = 0
    """0 for the plan; the Critic loop number (1, 2) for follow-up tasks (ticket 06)."""


class FollowUp(_Strict):
    """One extra investigation the Critic asks for."""

    agent: Literal["observability", "dependency"]
    goal: str
    params: dict[str, Any] = {}


class CriticReview(_Strict):
    """One Critic review. No follow-ups means the Critic is satisfied."""

    loop: int
    """The loop this review would start (1 for the first review)."""
    critique: str
    follow_ups: list[FollowUp] = []


class TaskTrace(_Strict):
    """When one task ran. Overlapping intervals show parallel work."""

    task_id: str
    agent: str
    wave: int
    """Index of the scheduling step that started the task; tasks in one wave ran together."""
    started_at: AwareDatetime
    ended_at: AwareDatetime
    status: Literal["done", "failed"]
    tool_calls: int = 0
    llm_calls: int = 0


class Counters(_Strict):
    tool_calls: int = 0
    llm_calls: int = 0
    tokens_in: int = 0
    tokens_out: int = 0
    cache_tokens: int = 0
    llm_latency_s: float = 0.0
    """Sum of per-call LLM latency. Calls overlap, so this exceeds the wall clock."""

    def add(
        self,
        *,
        usage: Usage | None = None,
        tool_calls: int = 0,
        llm_calls: int = 0,
        llm_latency_s: float = 0.0,
    ) -> Counters:
        usage = usage or Usage()
        return Counters(
            tool_calls=self.tool_calls + tool_calls,
            llm_calls=self.llm_calls + llm_calls,
            tokens_in=self.tokens_in + usage.tokens_in,
            tokens_out=self.tokens_out + usage.tokens_out,
            cache_tokens=self.cache_tokens + usage.cache_tokens,
            llm_latency_s=round(self.llm_latency_s + llm_latency_s, 3),
        )

    def __add__(self, other: Counters) -> Counters:
        return self.add(
            usage=Usage(
                tokens_in=other.tokens_in,
                tokens_out=other.tokens_out,
                cache_tokens=other.cache_tokens,
            ),
            tool_calls=other.tool_calls,
            llm_calls=other.llm_calls,
            llm_latency_s=other.llm_latency_s,
        )


class _HasId(Protocol):
    @property
    def id(self) -> str: ...


def merge_by_id[T: _HasId](old: list[T], new: list[T]) -> list[T]:
    """Reducer: an entry with a known id replaces the old one in place; new ids append."""
    merged = {item.id: item for item in old}
    for item in new:
        merged[item.id] = item
    return list(merged.values())


class Blackboard(_Strict):
    incident_id: str
    incident: IncidentReport
    started_at: AwareDatetime
    tasks: Annotated[list[Task], merge_by_id] = []
    findings: Annotated[list[Finding], operator.add] = []
    evidence: Annotated[list[Evidence], merge_by_id] = []
    """Every row a tool returned in this run (the IDs that really exist, for ticket 05)."""
    hypotheses: Annotated[list[Hypothesis], merge_by_id] = []
    gaps: Annotated[list[EvidenceGap], operator.add] = []
    closed_gaps: Annotated[list[str], operator.add] = []
    """IDs of gaps a later successful query for the same tool and service closed."""
    failures: Annotated[list[AgentFailure], operator.add] = []
    counters: Annotated[Counters, operator.add] = Counters()
    trace: Annotated[list[TaskTrace], operator.add] = []
    critic_loops: int = 0
    """How many times the Critic sent the run back for more evidence."""
    critic_reviews: Annotated[list[CriticReview], operator.add] = []
    terminated_by: TerminatedBy | None = None
    """Set when the run ends early: by the Critic loop limit, or by a run limit (limits.py)."""
    report: InvestigationReport | None = None

    def task(self, task_id: str) -> Task:
        return next(t for t in self.tasks if t.id == task_id)

    def open_gaps(self) -> list[EvidenceGap]:
        closed = set(self.closed_gaps)
        return [g for g in self.gaps if g.id is None or g.id not in closed]

    def findings_for(self, task_ids: list[str]) -> list[Finding]:
        return [f for f in self.findings if f.task_id in task_ids]

    def elapsed_s(self, now: datetime) -> float:
        return round((now - self.started_at).total_seconds(), 3)
