"""The specialist agents. Each reads typed blackboard entries and returns typed entries.

- Observability and Dependency (investigators): call tools through the run's
  ToolSession and write a Finding of Facts and Assumptions.
- Reasoning: no tools; reads Findings and writes at least 2 competing Hypotheses.
- Reporting: reads ranked Hypotheses and gaps and writes recommended actions.

No agent sees another agent's prompt or raw transcript, only Findings and Hypotheses.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from pydantic import BaseModel, ConfigDict, ValidationError

from incident_investigator.agents.actions import FinishAction
from incident_investigator.agents.ask import Answer, AnswerError, ask
from incident_investigator.agents.loop import AgentRun, run_agent
from incident_investigator.agents.prompts import (
    REASONING_SYSTEM,
    REPORTING_SYSTEM,
    investigator_system,
)
from incident_investigator.blackboard import AgentFailure, Blackboard, Counters, Task
from incident_investigator.limits import DEFAULT_LIMITS, RunLimits
from incident_investigator.llm.base import LLMClient
from incident_investigator.schemas import (
    Evidence,
    EvidenceGap,
    Finding,
    Hypothesis,
    RecommendedAction,
    ToolResult,
)
from incident_investigator.tools import TOOL_SPECS

MIN_HYPOTHESES = 2
CallTool = Callable[[str, dict[str, Any]], ToolResult]


@dataclass
class TaskOutcome:
    """Everything one task writes to the blackboard."""

    findings: list[Finding] = field(default_factory=list[Finding])
    hypotheses: list[Hypothesis] = field(default_factory=list[Hypothesis])
    evidence: list[Evidence] = field(default_factory=list[Evidence])
    gaps: list[EvidenceGap] = field(default_factory=list[EvidenceGap])
    closed_gaps: list[str] = field(default_factory=list[str])
    failure: AgentFailure | None = None
    stopped: bool = False
    """A run limit stopped the task before it finished (not the agent's fault)."""
    counters: Counters = field(default_factory=Counters)


def _counters(run: AgentRun) -> Counters:
    return Counters().add(
        usage=run.usage,
        tool_calls=run.tool_calls,
        llm_calls=run.llm_calls,
        llm_latency_s=run.llm_latency_s,
    )


def _json(items: list[Any]) -> str:
    return json.dumps([i.model_dump(mode="json") for i in items], indent=1)


def _task_block(task: Task) -> str:
    params = f"\nParameters: {json.dumps(task.params)}" if task.params else ""
    return f"Your task ({task.id}): {task.goal}{params}"


def run_investigator(
    llm: LLMClient,
    *,
    task: Task,
    board: Blackboard,
    model: str,
    call_tool: CallTool,
    limits: RunLimits = DEFAULT_LIMITS,
) -> TaskOutcome:
    gaps: list[EvidenceGap] = []
    closed_gaps: list[str] = []

    def tool(name: str) -> Callable[[dict[str, Any]], ToolResult]:
        def call(args: dict[str, Any]) -> ToolResult:
            result = call_tool(name, args)
            if result.gap is not None:
                gaps.append(result.gap.model_copy(update={"task_id": task.id}))
            closed_gaps.extend(result.closes_gaps)
            return result

        return call

    earlier = board.findings_for(task.depends_on)
    context = f"\n\nFindings from earlier tasks:\n{_json(earlier)}" if earlier else ""
    run = run_agent(
        llm=llm,
        agent=task.agent,
        task_id=task.id,
        model=model,
        system=investigator_system(task.agent),
        prompt=f"Incident report:\n{board.incident.model_dump_json(indent=1)}\n\n{_task_block(task)}{context}",
        tools={name: tool(name) for name in TOOL_SPECS},
        max_tool_calls=limits.max_tool_calls_per_task,
        max_failed_attempts=limits.max_failed_attempts,
    )
    outcome = TaskOutcome(
        evidence=run.evidence, gaps=gaps, closed_gaps=closed_gaps, failure=run.failure, counters=_counters(run), stopped=run.stopped
    )
    if run.finish is not None:
        outcome.findings = [
            Finding(task_id=task.id, agent=task.agent, facts=run.finish.facts, assumptions=run.finish.assumptions)
        ]
    return outcome


def check_hypotheses(finish: FinishAction) -> str | None:
    """Reject a finish without at least 2 competing, supported hypotheses (Reasoning and baseline)."""
    hyps = finish.hypotheses
    if len(hyps) < MIN_HYPOTHESES:
        return f"give at least {MIN_HYPOTHESES} competing hypotheses, got {len(hyps)}"
    if len({(h.service, h.category) for h in hyps}) < len(hyps):
        return "hypotheses must compete: each needs a different service or category"
    if len({h.id for h in hyps}) < len(hyps):
        return "hypothesis ids must be unique"
    unsupported = [h.id for h in hyps if not h.supporting_ids]
    if unsupported:
        return f"hypotheses {unsupported} cite no supporting evidence IDs"
    return None


def run_reasoning(
    llm: LLMClient, *, task: Task, board: Blackboard, model: str, limits: RunLimits = DEFAULT_LIMITS
) -> TaskOutcome:
    prompt = (
        f"Incident report:\n{board.incident.model_dump_json(indent=1)}\n\n{_task_block(task)}\n\n"
        f"Findings on the blackboard:\n{_json(board.findings)}\n\n"
        f"Open evidence gaps (data that could not be read):\n{_json(board.open_gaps())}"
    )
    if board.hypotheses:  # a Critic loop: revise the earlier pass
        prompt += (
            f"\n\nCurrent hypotheses from an earlier pass:\n{_json(board.hypotheses)}\n"
            "Return the full revised set. Reuse an id to replace that hypothesis."
        )
    run = run_agent(
        llm=llm,
        agent=task.agent,
        task_id=task.id,
        model=model,
        system=REASONING_SYSTEM,
        prompt=prompt,
        tools={},
        check_finish=check_hypotheses,
        max_failed_attempts=limits.max_failed_attempts,
    )
    outcome = TaskOutcome(failure=run.failure, counters=_counters(run), stopped=run.stopped)
    if run.finish is not None:
        outcome.hypotheses = run.finish.hypotheses
        if run.finish.assumptions or run.finish.facts:
            outcome.findings = [
                Finding(task_id=task.id, agent=task.agent, facts=run.finish.facts, assumptions=run.finish.assumptions)
            ]
    return outcome


class _Actions(BaseModel):
    model_config = ConfigDict(extra="forbid")

    recommended_actions: list[RecommendedAction] = []


REPORTING_TASK_ID = "report"


def recommend_actions(
    llm: LLMClient, *, board: Blackboard, ranked: list[Hypothesis], model: str
) -> Answer[list[RecommendedAction]]:
    known = {h.id for h in ranked}

    def parse(text: str) -> list[RecommendedAction]:
        try:
            actions = _Actions.model_validate_json(text).recommended_actions
        except ValidationError as exc:
            raise AnswerError(str(exc.errors(include_url=False)[:3])) from exc
        unknown = sorted({a.hypothesis_id for a in actions} - known)
        if unknown:
            raise AnswerError(f"actions name unknown hypothesis ids {unknown}; known: {sorted(known)}")
        return actions

    prompt = (
        f"Incident report:\n{board.incident.model_dump_json(indent=1)}\n\n"
        f"Ranked hypotheses (highest confidence first):\n{_json(ranked)}\n\n"
        f"Open evidence gaps:\n{_json(board.open_gaps())}"
    )
    return ask(
        llm,
        agent="reporting",
        task_id=REPORTING_TASK_ID,
        model=model,
        system=REPORTING_SYSTEM,
        prompt=prompt,
        parse=parse,
    )
