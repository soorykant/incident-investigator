"""The single-agent baseline (ticket 07, Q6): one agent in a loop, for a fair comparison.

It shares everything with the multi-agent graph except the architecture:

- **Tools:** the run's `ToolSession` (same tools, same failure schedule), every call
  through the run's `RunGuard`.
- **Limits:** the same `RunLimits` object. The baseline is one task, so the per-task
  cap (6) does not apply; the run's tool budget (30) is the binding limit, as it is for
  the multi-agent run as a whole. The 31st tool call is refused, trips
  `tool_budget` and gives the agent one finish-only turn, exactly as in multi mode. The token budget, wall clock and failed
  attempt limit (2, then an AgentFailure) are the same. There is no Critic, so no
  `critic_limit`.
- **Protocol:** the same JSON action protocol (`run_agent`), and the same finish check
  as the Reasoning agent: at least 2 competing hypotheses that cite evidence.
- **Report:** the same evidence-rule step (`apply_evidence_rules`), the same
  recommended-actions call (`recommend_actions`, made with the baseline's own client
  and model, outside the guard and skipped after `token_budget` or `timeout`, as in
  multi mode) and the same `build_report`.

The agent writes one Finding (task id `baseline`) and its hypotheses to a Blackboard,
which keeps the same counters and a one-entry trace for inspection.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from incident_investigator.agents.loop import run_agent
from incident_investigator.agents.prompts import baseline_system
from incident_investigator.agents.specialists import check_hypotheses, recommend_actions
from incident_investigator.blackboard import Blackboard, Counters, TaskTrace
from incident_investigator.evidence_rules import apply_evidence_rules, build_report
from incident_investigator.limits import GuardedLLM, RunGuard, TerminatedBy
from incident_investigator.llm.base import LLMClient
from incident_investigator.llm.router import RoleRouter
from incident_investigator.models_config import ModelsConfig, load_models
from incident_investigator.schemas import EvidenceGap, Finding, ReportMetrics, ToolResult
from incident_investigator.tools import TOOL_SPECS, ToolSession

ROLE = "baseline"
TASK_ID = "baseline"


def run_baseline(
    llm: LLMClient,
    tools: ToolSession,
    start: Blackboard,
    *,
    guard: RunGuard,
    models: ModelsConfig | None = None,
) -> Blackboard:
    """Run the baseline on `start` (a blackboard with just the incident) and return the
    final blackboard, with `report` set."""
    raw = llm.client_for(ROLE) if isinstance(llm, RoleRouter) else llm
    config = models or (llm.models if isinstance(llm, RoleRouter) else load_models())
    model = config.model_for(ROLE)
    limits = guard.limits
    gaps: list[EvidenceGap] = []
    closed_gaps: list[str] = []

    def tool(name: str):  # noqa: ANN202
        def call(args: dict[str, Any]) -> ToolResult:
            result = guard.tool_call(lambda: tools.call(name, args))
            if result.gap is not None:
                gaps.append(result.gap.model_copy(update={"task_id": TASK_ID}))
            closed_gaps.extend(result.closes_gaps)
            return result

        return call

    started = datetime.now(UTC)
    run = run_agent(
        llm=GuardedLLM(raw, guard),
        agent=ROLE,
        task_id=TASK_ID,
        model=model,
        system=baseline_system(limits.max_tool_calls_per_run),
        prompt=f"Incident report:\n{start.incident.model_dump_json(indent=1)}\n\n"
        "Investigate this incident and find its root cause.",
        tools={name: tool(name) for name in TOOL_SPECS},
        max_tool_calls=None,  # the run budget binds (see the module docstring)
        max_failed_attempts=limits.max_failed_attempts,
        check_finish=check_hypotheses,
    )
    finish = run.finish
    counters = Counters().add(
        usage=run.usage, tool_calls=run.tool_calls, llm_calls=run.llm_calls, llm_latency_s=run.llm_latency_s
    )
    board = start.model_copy(
        update={
            "findings": [Finding(task_id=TASK_ID, agent=ROLE, facts=finish.facts, assumptions=finish.assumptions)]
            if finish is not None
            else [],
            "hypotheses": finish.hypotheses if finish is not None else [],
            "evidence": run.evidence,
            "gaps": gaps,
            "closed_gaps": closed_gaps,
            "failures": [run.failure] if run.failure is not None else [],
            "trace": [
                TaskTrace(
                    task_id=TASK_ID,
                    agent=ROLE,
                    wave=0,
                    started_at=started,
                    ended_at=datetime.now(UTC),
                    status="done" if finish is not None else "failed",
                    tool_calls=run.tool_calls,
                    llm_calls=run.llm_calls,
                )
            ],
        }
    )

    stop = guard.stop
    terminated_by: TerminatedBy = stop or "done"
    review = apply_evidence_rules(board)
    actions = (
        recommend_actions(raw, board=board, ranked=review.hypotheses, model=model)
        if review.hypotheses and stop not in ("token_budget", "timeout")
        else None
    )
    if actions is not None:
        counters = counters + actions.counters
    failures = board.failures + ([actions.failure] if actions is not None and actions.failure is not None else [])
    board = board.model_copy(update={"counters": counters, "failures": failures})
    report = build_report(
        board,
        review=review,
        actions=(actions.value or []) if actions is not None else [],
        metrics=ReportMetrics(
            tool_calls=counters.tool_calls,
            llm_calls=counters.llm_calls,
            tokens_in=counters.tokens_in,
            tokens_out=counters.tokens_out,
            cache_tokens=counters.cache_tokens,
            latency_s=guard.elapsed_s(),
        ),
        terminated_by=terminated_by,
    ).model_copy(update={"failures": failures})
    return board.model_copy(update={"report": report, "terminated_by": terminated_by})
