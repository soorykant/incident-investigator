"""The Critic loop and the run limits, driven through `investigate()` with a scripted LLM
(PRD steps 5 and 6, L26, D5, Q16)."""

from datetime import UTC, datetime, timedelta
from typing import Any

from test_investigation import INCIDENT, SERVICE, WINDOW, RoleLLM, tool_calls_then_finish

from incident_investigator.investigate import investigate
from incident_investigator.limits import DEFAULT_LIMITS, RunLimits
from incident_investigator.llm.base import LLMResponse, Usage

SATISFIED: dict[str, Any] = {"critique": "The evidence is enough.", "follow_ups": []}


def asks_for(*follow_ups: dict[str, Any]) -> Any:
    return lambda _: {"critique": "Check more.", "follow_ups": list(follow_ups)}


METRICS_FOLLOW_UP = {
    "agent": "observability",
    "goal": f"Check metrics for {SERVICE} in the first 15 minutes",
    "params": {"service": SERVICE, **WINDOW},
}


# --- The limits live in one shared config ------------------------------------


def test_the_limit_values_live_in_one_config_object():
    assert DEFAULT_LIMITS == RunLimits(
        max_tool_calls_per_task=6,
        max_tool_calls_per_run=30,
        max_tokens_per_run=300_000,
        wall_clock_s=600,
        max_critic_loops=2,
        max_failed_attempts=2,
    )


# --- Critic loop (step 5) -----------------------------------------------------


def test_a_satisfied_critic_ends_the_run_with_done():
    llm = RoleLLM(critic=lambda _: SATISFIED)

    run = investigate(INCIDENT, llm)

    assert len(llm.prompts["critic"]) == 1
    assert run.blackboard.critic_loops == 0
    assert run.report.terminated_by == "done"
    assert {t.origin for t in run.blackboard.tasks} == {"plan"}


def test_critic_follow_up_tasks_carry_origin_critic_and_their_loop_number():
    replies = iter([asks_for(METRICS_FOLLOW_UP)(""), SATISFIED])
    llm = RoleLLM(critic=lambda _: next(replies))

    run = investigate(INCIDENT, llm)

    board = run.blackboard
    follow_ups = [t for t in board.tasks if t.origin == "critic"]
    assert [(t.agent, t.loop, t.status) for t in follow_ups] == [
        ("observability", 1, "done"),
        ("reasoning", 1, "done"),
    ]
    obs, reasoning = follow_ups
    assert obs.goal == METRICS_FOLLOW_UP["goal"] and obs.params == METRICS_FOLLOW_UP["params"]
    assert reasoning.depends_on == [obs.id]
    assert board.critic_loops == 1
    assert run.report.terminated_by == "done"
    # The follow-up investigator really ran, and the second Reasoning pass saw its finding
    # and the Critic's critique.
    assert len(llm.prompts["reasoning"]) == 2
    assert "Check more." in llm.prompts["reasoning"][1]
    # The Critic reviewed hypotheses and findings from the blackboard.
    assert '"h1"' in llm.prompts["critic"][0] and "OBS-FINDING" in llm.prompts["critic"][0]


def test_a_critic_that_always_asks_for_more_gets_exactly_two_loops_then_reporting():
    llm = RoleLLM(critic=asks_for(METRICS_FOLLOW_UP))

    run = investigate(INCIDENT, llm)

    board = run.blackboard
    assert board.critic_loops == 2
    assert sorted({t.loop for t in board.tasks if t.origin == "critic"}) == [1, 2]
    assert len(llm.prompts["reasoning"]) == 3, "the plan's pass and one per loop"
    assert len(llm.prompts["critic"]) == 3, "the third review still wants more"
    assert len(llm.prompts["reporting"]) == 1
    assert run.report.terminated_by == "critic_limit"
    assert run.report.ranked_hypotheses


def test_an_invalid_critic_reply_twice_is_an_agent_failure_and_the_run_reports():
    run = investigate(INCIDENT, RoleLLM(critic=lambda _: "no idea"))

    assert [(f.agent, f.attempts) for f in run.report.failures] == [("critic", 2)]
    assert run.report.terminated_by == "done"


# --- Limits (step 6) ----------------------------------------------------------

LOGS = {"tool": "logs", "args": {"service": SERVICE, **WINDOW}}


def always_calls_logs(_: str) -> Any:
    return {"action": "call_tool", **LOGS}


def many_investigators(n: int) -> dict[str, Any]:
    ids = [f"t{i}" for i in range(1, n + 1)]
    tasks = [{"id": i, "agent": "observability", "goal": "logs", "depends_on": []} for i in ids]
    return {"tasks": [*tasks, {"id": "r", "agent": "reasoning", "goal": "compare", "depends_on": ids}]}


def test_a_task_that_always_calls_tools_is_cut_off_at_six_calls():
    llm = RoleLLM(delay_s=0.0, observability=always_calls_logs, critic=lambda _: SATISFIED)

    run = investigate(INCIDENT, llm)

    t1 = next(t for t in run.blackboard.trace if t.task_id == "t1")
    assert t1.tool_calls == 6
    assert [(f.task_id, f.attempts) for f in run.report.failures] == [("t1", 2)]
    assert "tool call limit" in run.report.failures[0].reason
    assert run.report.terminated_by == "done"


def test_agents_that_always_call_tools_stop_the_run_at_thirty_tool_calls():
    llm = RoleLLM(delay_s=0.0, planner=lambda _: many_investigators(6), observability=always_calls_logs)

    run = investigate(INCIDENT, llm)

    report = run.report
    assert report.terminated_by == "tool_budget"
    assert report.metrics.tool_calls == 30
    assert run.blackboard.counters.tool_calls == 30
    assert all(t.tool_calls <= 6 for t in run.blackboard.trace)
    # The tool budget stops tool calls, not thinking: Reasoning is still dispatched. (Here it
    # has no findings to cite, so its hypotheses are rejected; that is the agents' fault.)
    assert run.blackboard.task("r").status != "pending"
    assert llm.prompts["reasoning"], "Reasoning still runs after the tool budget is spent"
    assert llm.prompts["critic"] == [], "no tool call is left for a follow-up"
    # Investigators that keep calling tools after the budget is spent fail.
    assert {f.task_id for f in report.failures if f.agent == "observability"} == {f"t{i}" for i in range(1, 7)}


def test_the_tool_budget_comes_from_the_config():
    llm = RoleLLM(delay_s=0.0, observability=always_calls_logs)
    limits = RunLimits(max_tool_calls_per_run=4)

    run = investigate(INCIDENT, llm, limits=limits)

    assert run.report.terminated_by == "tool_budget"
    assert run.report.metrics.tool_calls == 4


class TokenHeavyLLM(RoleLLM):
    """Every call reports `tokens_in` input tokens and a large cache read."""

    def __init__(self, tokens_in: int, **overrides: Any):
        super().__init__(delay_s=0.0, **overrides)
        self.tokens_in = tokens_in

    def complete(self, *, system: str, prompt: str, model: str) -> LLMResponse:
        response = super().complete(system=system, prompt=prompt, model=model)
        usage = Usage(tokens_in=self.tokens_in, tokens_out=1_000, cache_tokens=1_000_000)
        return response.model_copy(update={"usage": usage})


ONE_INVESTIGATOR = {
    "tasks": [
        {"id": "t1", "agent": "observability", "goal": "logs", "depends_on": []},
        {"id": "t2", "agent": "reasoning", "goal": "compare", "depends_on": ["t1"]},
    ]
}


def test_the_token_budget_ends_the_run_with_token_budget_and_a_report():
    llm = TokenHeavyLLM(tokens_in=99_000, planner=lambda _: ONE_INVESTIGATOR)

    run = investigate(INCIDENT, llm)

    report = run.report
    assert report.terminated_by == "token_budget"
    # plan (100k) + 2 observability calls (300k): the budget is spent; no new call starts.
    assert report.metrics.tokens_in + report.metrics.tokens_out == 300_000
    assert llm.prompts["reasoning"] == []
    assert llm.prompts["reporting"] == [], "no LLM call after the token budget is spent"
    assert report.outcome == "INCONCLUSIVE"


def test_cache_reads_do_not_count_toward_the_token_budget():
    run = investigate(INCIDENT, TokenHeavyLLM(tokens_in=10, critic=lambda _: SATISFIED))

    assert run.report.metrics.cache_tokens > 300_000
    assert run.report.terminated_by == "done"


class FakeClock:
    def __init__(self) -> None:
        self.now = datetime(2026, 1, 1, tzinfo=UTC)

    def __call__(self) -> datetime:
        return self.now


def test_the_wall_clock_ends_the_run_with_timeout_and_a_report():
    clock = FakeClock()
    finish = tool_calls_then_finish([LOGS], "OBS-FINDING")

    def slow_observability(prompt: str) -> Any:
        clock.now += timedelta(minutes=11)
        return finish(prompt)

    llm = RoleLLM(delay_s=0.0, observability=slow_observability)

    run = investigate(INCIDENT, llm, clock=clock)

    assert run.report.terminated_by == "timeout"
    assert run.report.metrics.latency_s >= 600
    assert llm.prompts["reasoning"] == []
    assert llm.prompts["reporting"] == []


def test_a_run_within_every_limit_ends_with_done():
    run = investigate(INCIDENT, RoleLLM(critic=lambda _: SATISFIED), clock=FakeClock())

    assert run.report.terminated_by == "done"
    assert run.report.metrics.latency_s == 0


# --- Agent failures (error states) --------------------------------------------


def test_an_agent_that_fails_twice_is_in_the_report_and_the_dag_continues():
    llm = RoleLLM(observability=lambda _: "not json", critic=lambda _: SATISFIED)

    run = investigate(INCIDENT, llm)

    assert [(f.agent, f.task_id, f.attempts) for f in run.report.failures] == [("observability", "t1", 2)]
    assert run.blackboard.task("t3").status == "done"
    assert run.report.ranked_hypotheses
    assert run.report.terminated_by == "done"


# --- The offline mock plays the Critic ----------------------------------------


def test_the_mock_runs_one_critic_loop_on_every_incident_and_finishes_done():
    from incident_investigator.dataset import list_incident_ids
    from incident_investigator.llm.mock import MockLLM

    for incident_id in list_incident_ids():
        run = investigate(incident_id, MockLLM(delay_s=0.0), tool_backoff_s=0)
        assert run.report.terminated_by == "done", incident_id
        assert run.blackboard.critic_loops == 1, incident_id
        assert run.report.metrics.tool_calls <= DEFAULT_LIMITS.max_tool_calls_per_run


# --- The Critic plans within the tool budget (inc04 live run, 2026-11-13) --------------


def test_the_critic_is_told_how_many_tool_calls_the_run_has_left():
    llm = RoleLLM(delay_s=0.0)

    investigate(INCIDENT, llm)

    # The default plan makes 2 tool calls (logs, dependencies) before the Critic runs.
    assert "Tool calls left in this run: 28 of 30" in llm.prompts["critic"][0]
    assert "at most 6 tool calls" in llm.prompts["critic"][0]


def test_when_the_tool_budget_is_spent_the_critic_is_skipped_and_the_run_reports_tool_budget():
    llm = RoleLLM(delay_s=0.0)

    run = investigate(INCIDENT, llm, limits=RunLimits(max_tool_calls_per_run=2))

    assert run.report.metrics.tool_calls == 2
    assert llm.prompts["critic"] == [], "no follow-up could run, so the Critic is not asked"
    assert run.report.terminated_by == "tool_budget"
    assert run.report.ranked_hypotheses and len(llm.prompts["reporting"]) == 1


def test_an_investigator_cut_off_by_the_run_budget_keeps_its_finding_and_reasoning_still_runs():
    def dependency(prompt: str) -> Any:
        if "tool budget is spent" in prompt:
            return {"action": "finish", "facts": [], "assumptions": [
                {"statement": "DEP-CUT-OFF", "basis": "no tool calls left", "how_to_verify": "query the graph"}]}
        return {"action": "call_tool", "tool": "dependencies", "args": {"service": SERVICE}}

    llm = RoleLLM(delay_s=0.0, dependency=dependency)

    # One tool call in the run: t1 (first in the turnstile) gets it, t2 is refused.
    run = investigate(INCIDENT, llm, limits=RunLimits(max_tool_calls_per_run=1))

    board = run.blackboard
    assert board.counters.tool_calls == 1
    assert [t.status for t in board.tasks] == ["done", "done", "done"]
    assert any(a.statement == "DEP-CUT-OFF" for f in board.findings for a in f.assumptions)
    assert len(llm.prompts["reasoning"]) == 1
    assert run.report.terminated_by == "tool_budget"
    assert run.report.failures == []


def test_follow_ups_that_never_started_are_skipped_and_their_reasoning_task_still_runs():
    # The Critic asks for 2 follow-ups; the budget leaves room for the first only.
    second = {**METRICS_FOLLOW_UP, "goal": "Check logs again"}
    critic_calls = iter([{"critique": "Check more.", "follow_ups": [METRICS_FOLLOW_UP, second]}])
    llm = RoleLLM(delay_s=0.0, critic=lambda _: next(critic_calls, SATISFIED))

    run = investigate(INCIDENT, llm, limits=RunLimits(max_tool_calls_per_run=3))

    status = {t.id: t.status for t in run.blackboard.tasks}
    assert status["c1.r"] == "done", "the loop's Reasoning task runs on what the follow-ups found"
    assert run.report.terminated_by == "tool_budget"
