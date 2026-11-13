"""The Orchestrator's single planning call (Q9): incident report -> validated task DAG.

The plan is checked in code: unique task IDs, known agents, dependencies that exist,
no cycles, at least one investigator task and exactly one reasoning task that depends
(directly or through other tasks) on every investigator task. An invalid plan is sent
back once with the problem; a second failure records an AgentFailure for the planner
and the run uses DEFAULT_PLAN, so an investigation always has a plan.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from incident_investigator.agents.ask import AnswerError, ask
from incident_investigator.agents.prompts import PLANNER_SYSTEM
from incident_investigator.blackboard import (
    INVESTIGATORS,
    AgentFailure,
    AgentName,
    Counters,
    Task,
)
from incident_investigator.llm.base import LLMClient
from incident_investigator.schemas import IncidentReport

MAX_PLAN_TASKS = 8
PLAN_TASK_ID = "plan"
"""The task_id an AgentFailure of the planner carries."""


class _PlanTask(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str = Field(pattern=r"^[A-Za-z0-9_-]{1,32}$")
    agent: AgentName
    goal: str = Field(min_length=1)
    params: dict[str, Any] = {}
    depends_on: list[str] = []


class _Plan(BaseModel):
    model_config = ConfigDict(extra="forbid")

    tasks: list[_PlanTask] = Field(min_length=1, max_length=MAX_PLAN_TASKS)

    @model_validator(mode="after")
    def _is_a_runnable_dag(self) -> _Plan:
        ids = [t.id for t in self.tasks]
        duplicates = sorted({i for i in ids if ids.count(i) > 1})
        if duplicates:
            raise ValueError(f"duplicate task ids {duplicates}")
        for task in self.tasks:
            unknown = [d for d in task.depends_on if d not in ids or d == task.id]
            if unknown:
                raise ValueError(f"task {task.id} depends on unknown task ids {unknown}")
        deps = {t.id: set(t.depends_on) for t in self.tasks}
        if _has_cycle(deps):
            raise ValueError("the depends_on edges form a cycle")
        reasoning = [t for t in self.tasks if t.agent == "reasoning"]
        investigators = [t.id for t in self.tasks if t.agent in INVESTIGATORS]
        if len(reasoning) != 1:
            raise ValueError(f"the plan needs exactly one reasoning task, found {len(reasoning)}")
        if not investigators:
            raise ValueError("the plan needs at least one observability or dependency task")
        missing = sorted(set(investigators) - _ancestors(reasoning[0].id, deps))
        if missing:
            raise ValueError(f"the reasoning task must depend on investigator tasks {missing}")
        return self


def _ancestors(task_id: str, deps: dict[str, set[str]]) -> set[str]:
    seen: set[str] = set()
    stack = list(deps[task_id])
    while stack:
        current = stack.pop()
        if current not in seen:
            seen.add(current)
            stack.extend(deps[current])
    return seen


def _has_cycle(deps: dict[str, set[str]]) -> bool:
    return any(task_id in _ancestors(task_id, deps) for task_id in deps)


DEFAULT_PLAN: list[Task] = [
    Task(id="t1", agent="observability", goal="Find what changed and what broke: logs, metrics, deploys and config changes for the affected service in the incident window."),
    Task(id="t2", agent="dependency", goal="Check the services the affected service depends on and similar past incidents."),
    Task(id="t3", agent="reasoning", goal="Build and compare competing root-cause hypotheses from the findings.", depends_on=["t1", "t2"]),
]  # fmt: skip


@dataclass
class PlanResult:
    tasks: list[Task]
    failure: AgentFailure | None = None
    counters: Counters = field(default_factory=Counters)


def _parse(text: str) -> list[Task]:
    try:
        plan = _Plan.model_validate_json(text)
    except ValidationError as exc:
        problems = "; ".join(
            f"{'.'.join(map(str, e['loc'])) or 'plan'}: {e['msg']}"
            for e in exc.errors(include_url=False)[:3]
        )
        raise AnswerError(problems) from exc
    return [Task(**t.model_dump()) for t in plan.tasks]


def make_plan(llm: LLMClient, *, incident: IncidentReport, model: str) -> PlanResult:
    answer = ask(
        llm,
        agent="planner",
        task_id=PLAN_TASK_ID,
        model=model,
        system=PLANNER_SYSTEM,
        prompt=f"Incident report:\n{incident.model_dump_json(indent=2)}",
        parse=_parse,
        retry_hint="Your last reply was not a valid plan",
    )
    if answer.value is None:
        return PlanResult(tasks=list(DEFAULT_PLAN), failure=answer.failure, counters=answer.counters)
    return PlanResult(tasks=answer.value, counters=answer.counters)
