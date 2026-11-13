"""The investigation graph: plan -> run the task DAG -> evidence rules -> Critic -> report.

The LangGraph state is the Blackboard. The Orchestrator is a hybrid (Q9): one
planning LLM call, then plain Python runs the DAG:

    START -> plan -> schedule --(Send per ready task)--> run_task -> schedule ...
                         \\--(nothing ready, or a run limit hit)--> evidence_rules
    evidence_rules --(hypotheses, no limit hit)--> critic --(follow-ups)--> schedule ...
                   \\--(otherwise)--> reporting        \\--(otherwise)--> reporting -> END

`schedule` marks every ready task (all dependencies finished) as running and fans
them out with `Send`; LangGraph runs one graph step's sends in parallel threads.
When a wave ends, `schedule` runs again, so a task never starts before its
dependencies are done. Each `run_task` returns only typed blackboard entries;
reducers on the Blackboard merge parallel writes.

Critic loop (Q16): after the evidence rules, the Critic reviews the hypotheses. If it
asks for follow-ups, they become `Task(origin="critic", loop=n)` entries plus a
Reasoning task that revises the hypotheses, and the run goes back to `schedule`. At
most `max_critic_loops` loops run; if the Critic still wants more after the last one,
Reporting runs with `terminated_by=critic_limit`.

Run limits (limits.py): one `RunGuard` per run. Every agent's LLM client is wrapped
in `GuardedLLM` and every tool call goes through `guard.tool_call`. Once a limit
trips, in-flight agents stop at their next LLM or tool call, `schedule` dispatches
nothing more, the Critic is skipped, and Reporting writes the report from the
current blackboard. Reporting's one LLM call (recommended actions) bypasses the
guard, but is skipped after `token_budget` or `timeout`.

`terminated_by` = the guard's stop reason if a run limit tripped, else `critic_limit`
if the Critic still wanted more at the loop limit, else `done`.

The evidence rules and report assembly are shared with the single-agent baseline
(ticket 07): `evidence_rules` sets `final_confidence` with `apply_evidence_rules`, and
`reporting` assembles the report with `build_report` (evidence_rules.py, ticket 05).
"""

from __future__ import annotations

import itertools
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Literal

from langgraph.graph import END, START, StateGraph
from langgraph.types import Send

from incident_investigator.agents.critic import follow_up_tasks, review
from incident_investigator.agents.planner import make_plan
from incident_investigator.agents.specialists import (
    TaskOutcome,
    recommend_actions,
    run_investigator,
    run_reasoning,
)
from incident_investigator.blackboard import AgentFailure, Blackboard, Counters, Task, TaskTrace
from incident_investigator.dag import ToolTurns, blocked_tasks, ready_tasks
from incident_investigator.evidence_rules import apply_evidence_rules, build_report
from incident_investigator.limits import GuardedLLM, RunGuard, TerminatedBy
from incident_investigator.llm.base import LLMClient, LLMFatalError
from incident_investigator.llm.router import RoleRouter
from incident_investigator.models_config import ModelsConfig, load_models
from incident_investigator.schemas import ReportMetrics
from incident_investigator.tools import ToolSession


@dataclass
class TaskRun:
    """The `Send` payload: one task, the blackboard as it was when the wave started,
    and the wave's tool-call turnstile."""

    task: Task
    board: Blackboard
    wave: int
    turns: ToolTurns


def _now() -> datetime:
    return datetime.now(UTC)


def build_graph(
    llm: LLMClient,
    tools: ToolSession,
    models: ModelsConfig | None = None,
    *,
    guard: RunGuard | None = None,
) -> Any:
    """Compile the graph for one run. `tools` is that run's ToolSession: the only
    holder of the scenario. A RoleRouter `llm` routes each role to its own client.
    `guard` enforces the run limits (default: `DEFAULT_LIMITS` on the real clock)."""
    if isinstance(llm, RoleRouter):
        router = llm
        models = models or router.models

        def raw_client(role: str) -> LLMClient:
            return router.client_for(role)
    else:

        def raw_client(role: str) -> LLMClient:
            return llm

    run_guard = guard or RunGuard()
    limits = run_guard.limits

    def client(role: str) -> LLMClient:
        return GuardedLLM(raw_client(role), run_guard)

    config = models or load_models()
    waves = itertools.count()

    def plan(state: Blackboard) -> dict[str, Any]:
        result = make_plan(client("planner"), incident=state.incident, model=config.model_for("planner"))
        update: dict[str, Any] = {"tasks": result.tasks, "counters": result.counters}
        if result.failure is not None:
            update["failures"] = [result.failure]
        return update

    def schedule(state: Blackboard) -> dict[str, Any]:
        if run_guard.poll() is not None:
            return {}  # the token budget or the wall clock was hit: dispatch nothing more
        tasks = list(state.tasks)
        changed: list[Task] = []
        while True:
            skip = [t.model_copy(update={"status": "failed"}) for t in blocked_tasks(tasks)]
            if run_guard.tool_calls_left() == 0:
                # No tool call is left: an investigator could only finish empty-handed, so it
                # is skipped. Reasoning needs no tools and still runs on what was found.
                skip += [t.model_copy(update={"status": "failed"}) for t in ready_tasks(tasks) if t.agent != "reasoning"]
            if not skip:
                break
            done = {t.id: t for t in skip}
            tasks = [done.get(t.id, t) for t in tasks]
            changed += skip
        running = [t.model_copy(update={"status": "running"}) for t in ready_tasks(tasks)]
        return {"tasks": changed + running} if changed or running else {}

    def fan_out(state: Blackboard) -> list[Send] | Literal["evidence_rules"]:
        running = [t for t in state.tasks if t.status == "running"]
        if not running:
            return "evidence_rules"
        wave = next(waves)
        turns = ToolTurns([t.id for t in running])
        return [Send("run_task", TaskRun(task=t, board=state, wave=wave, turns=turns)) for t in running]

    def run_task(state: TaskRun) -> dict[str, Any]:
        run = state
        task, started = run.task, _now()
        try:
            outcome = _run(run)
        except LLMFatalError:
            raise  # a rate limit or a rejected key stops the whole run; the eval runner saves nothing for this pair (L25)
        except Exception as exc:  # an agent crash must not stop the DAG (Q16)
            failure = AgentFailure(agent=task.agent, task_id=task.id, reason=f"crash: {exc!r}", attempts=1)
            outcome = TaskOutcome(failure=failure)
        finally:
            run.turns.finish(task.id)
        # A task cut off by a run limit did not finish, but it is not an AgentFailure.
        failed = outcome.failure is not None or outcome.stopped
        status: Literal["done", "failed"] = "failed" if failed else "done"
        trace = TaskTrace(
            task_id=task.id,
            agent=task.agent,
            wave=run.wave,
            started_at=started,
            ended_at=_now(),
            status=status,
            tool_calls=outcome.counters.tool_calls,
            llm_calls=outcome.counters.llm_calls,
        )
        return {
            "tasks": [task.model_copy(update={"status": status})],
            "findings": outcome.findings,
            "hypotheses": outcome.hypotheses,
            "evidence": outcome.evidence,
            "gaps": outcome.gaps,
            "closed_gaps": outcome.closed_gaps,
            "failures": [outcome.failure] if outcome.failure is not None else [],
            "counters": outcome.counters,
            "trace": [trace],
        }

    def _run(run: TaskRun) -> TaskOutcome:
        task, role = run.task, run.task.agent
        model = config.model_for(role)
        if role == "reasoning":
            return run_reasoning(client(role), task=task, board=run.board, model=model, limits=limits)

        def call_tool(name: str, args: dict[str, Any]):  # noqa: ANN202
            # The turnstile fixes the call order; the guard enforces the run's tool budget.
            return run.turns.call(task.id, lambda: run_guard.tool_call(lambda: tools.call(name, args)))

        return run_investigator(
            client(role), task=task, board=run.board, model=model, call_tool=call_tool, limits=limits
        )

    def evidence_rules(state: Blackboard) -> dict[str, Any]:
        # Only final_confidence goes back on the board (for the Critic). The rules always
        # recompute from the agents' raw entries, so they stay idempotent across loops.
        final = {h.id: h.final_confidence for h in apply_evidence_rules(state).hypotheses}
        return {"hypotheses": [h.model_copy(update={"final_confidence": final[h.id]}) for h in state.hypotheses]}

    def _after_evidence_rules(state: Blackboard) -> Literal["critic", "reporting"]:
        if run_guard.poll() is not None or not state.hypotheses:
            return "reporting"  # a run limit was hit, or there is nothing to critique
        if run_guard.tools_spent():
            return "reporting"  # no follow-up could make a tool call: report with tool_budget
        return "critic"

    def critic(state: Blackboard) -> dict[str, Any]:
        answer = review(
            client("critic"),
            board=state,
            model=config.model_for("critic"),
            limits=limits,
            tool_calls_left=run_guard.tool_calls_left(),
        )
        update: dict[str, Any] = {"counters": answer.counters}
        if answer.failure is not None:
            update["failures"] = [answer.failure]
        verdict = answer.value
        if verdict is None:  # the Critic failed or a run limit stopped it: report as is
            return update
        update["critic_reviews"] = [verdict]
        if not verdict.follow_ups:
            return update  # satisfied
        if state.critic_loops >= limits.max_critic_loops:
            update["terminated_by"] = "critic_limit"
            return update
        update["tasks"] = follow_up_tasks(verdict)
        update["critic_loops"] = state.critic_loops + 1
        return update

    def _after_critic(state: Blackboard) -> Literal["schedule", "reporting"]:
        more = any(t.status == "pending" for t in state.tasks)
        return "schedule" if more and run_guard.poll() is None else "reporting"

    def reporting(state: Blackboard) -> dict[str, Any]:
        stop = run_guard.stop
        terminated_by: TerminatedBy = stop or state.terminated_by or "done"
        rules = apply_evidence_rules(state)
        ranked = rules.hypotheses
        actions = (
            recommend_actions(
                raw_client("reporting"), board=state, ranked=ranked, model=config.model_for("reporting")
            )
            if ranked and stop not in ("token_budget", "timeout")
            else None
        )
        spent = actions.counters if actions is not None else Counters()
        counters = state.counters + spent
        new_failures = [actions.failure] if actions is not None and actions.failure is not None else []
        report = build_report(
            state,
            review=rules,
            actions=(actions.value or []) if actions is not None else [],
            metrics=ReportMetrics(
                tool_calls=counters.tool_calls,
                llm_calls=counters.llm_calls,
                tokens_in=counters.tokens_in,
                tokens_out=counters.tokens_out,
                cache_tokens=counters.cache_tokens,
                latency_s=run_guard.elapsed_s(),
            ),
            terminated_by=terminated_by,
        ).model_copy(update={"failures": state.failures + new_failures})
        return {"report": report, "counters": spent, "failures": new_failures, "terminated_by": terminated_by}

    graph = StateGraph(Blackboard)
    graph.add_node("plan", plan)
    graph.add_node("schedule", schedule)
    graph.add_node("run_task", run_task)
    graph.add_node("evidence_rules", evidence_rules)
    graph.add_node("critic", critic)
    graph.add_node("reporting", reporting)
    graph.add_edge(START, "plan")
    graph.add_edge("plan", "schedule")
    graph.add_conditional_edges("schedule", fan_out, ["run_task", "evidence_rules"])
    graph.add_edge("run_task", "schedule")
    graph.add_conditional_edges("evidence_rules", _after_evidence_rules, ["critic", "reporting"])
    graph.add_conditional_edges("critic", _after_critic, ["schedule", "reporting"])
    graph.add_edge("reporting", END)
    return graph.compile()
