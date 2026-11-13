"""The DAG runner over the blackboard, driven through `investigate()` with a scripted LLM
(PRD step 4, Q4, Q9, Q14)."""

import json
import re
import threading
import time
from collections import defaultdict
from collections.abc import Callable
from typing import Any

import pytest

from incident_investigator.agents.prompts import role_of
from incident_investigator.dataset import load_scenario
from incident_investigator.investigate import investigate
from incident_investigator.llm.base import LLMAuthError, LLMError, LLMRateLimitError, LLMResponse, Usage

Reply = Callable[[str], Any]
INCIDENT = "inc01"
SCENARIO = load_scenario(INCIDENT)
SERVICE = SCENARIO.report.affected_service
WINDOW = {
    "start": SCENARIO.report.window_start.isoformat(),
    "end": SCENARIO.report.window_end.isoformat(),
}
_ID = re.compile(r'"id":\s*"([a-z]+:inc\d{2}:[^"]+)"')
_ANY_ID = re.compile(r'"([a-z]+:inc\d{2}:[^"]+)"')

PLAN = {
    "tasks": [
        {"id": "t1", "agent": "observability", "goal": "logs", "depends_on": []},
        {"id": "t2", "agent": "dependency", "goal": "graph", "depends_on": []},
        {"id": "t3", "agent": "reasoning", "goal": "compare", "depends_on": ["t1", "t2"]},
    ]
}


def tool_calls_then_finish(calls: list[dict[str, Any]], marker: str) -> Reply:
    """Call the tools in order, then finish with one fact citing the first ID seen."""

    def reply(prompt: str) -> Any:
        done = prompt.count("Tool result:")
        if done < len(calls):
            return {"action": "call_tool", **calls[done]}
        ids = _ID.findall(prompt)
        facts = [{"statement": marker, "evidence_ids": ids[:1]}] if ids else []
        return {"action": "finish", "facts": facts, "assumptions": []}

    return reply


def reasoning_reply(prompt: str) -> Any:
    ids = sorted(set(_ANY_ID.findall(prompt)))
    return {
        "action": "finish",
        "hypotheses": [
            {"id": "h1", "service": SERVICE, "category": "bad_deploy", "summary": "deploy",
             "supporting_ids": ids[:1], "contradicting_ids": ids[-1:], "proposed_confidence": 0.8},
            {"id": "h2", "service": SERVICE, "category": "capacity", "summary": "cpu",
             "supporting_ids": ids[-1:], "contradicting_ids": ids[:1], "proposed_confidence": 0.3},
        ],
    }  # fmt: skip


def reporting_reply(prompt: str) -> Any:
    return {"recommended_actions": [{"action": "Roll back", "rationale": "r", "hypothesis_id": "h1"}]}


DEFAULT_SCRIPT: dict[str, Reply] = {
    "planner": lambda _: PLAN,
    "observability": tool_calls_then_finish([{"tool": "logs", "args": {"service": SERVICE, **WINDOW}}], "OBS-FINDING"),
    "dependency": tool_calls_then_finish([{"tool": "dependencies", "args": {"service": SERVICE}}], "DEP-FINDING"),
    "reasoning": reasoning_reply,
    "reporting": reporting_reply,
    "critic": lambda _: {"critique": "The evidence is enough.", "follow_ups": []},
}


class RoleLLM:
    """An LLMClient stand-in that answers by role and records every prompt."""

    def __init__(self, delay_s: float = 0.05, **overrides: Reply):
        self.script = {**DEFAULT_SCRIPT, **overrides}
        self.delay_s = delay_s
        self.prompts: dict[str, list[str]] = defaultdict(list)
        self._lock = threading.Lock()

    def preflight(self) -> None:
        return None

    def complete(self, *, system: str, prompt: str, model: str) -> LLMResponse:
        role = role_of(system) or "?"
        with self._lock:
            self.prompts[role].append(prompt)
        time.sleep(self.delay_s)
        reply = self.script[role](prompt)
        text = reply if isinstance(reply, str) else json.dumps(reply)
        return LLMResponse(text=text, usage=Usage(tokens_in=10, tokens_out=5), latency_s=self.delay_s)


def _span(run: Any, task_id: str) -> tuple[Any, Any]:
    entry = next(t for t in run.blackboard.trace if t.task_id == task_id)
    return entry.started_at, entry.ended_at


def test_independent_tasks_run_at_the_same_time():
    run = investigate(INCIDENT, RoleLLM())

    obs_start, obs_end = _span(run, "t1")
    dep_start, dep_end = _span(run, "t2")
    assert obs_start < dep_end and dep_start < obs_end, "t1 and t2 should overlap"


def test_a_task_starts_only_after_every_task_it_depends_on_is_done():
    chain = {
        "tasks": [
            {"id": "a", "agent": "observability", "goal": "first", "depends_on": []},
            {"id": "b", "agent": "dependency", "goal": "after a", "depends_on": ["a"]},
            {"id": "c", "agent": "observability", "goal": "parallel", "depends_on": []},
            {"id": "r", "agent": "reasoning", "goal": "compare", "depends_on": ["b", "c"]},
        ]
    }
    run = investigate(INCIDENT, RoleLLM(planner=lambda _: chain))

    assert {t.id: t.status for t in run.blackboard.tasks} == {"a": "done", "b": "done", "c": "done", "r": "done"}
    assert _span(run, "b")[0] >= _span(run, "a")[1]
    assert _span(run, "r")[0] >= max(_span(run, "b")[1], _span(run, "c")[1])


def test_agents_share_information_only_through_typed_blackboard_entries():
    llm = RoleLLM()

    run = investigate(INCIDENT, llm)

    board = run.blackboard
    assert {(f.task_id, f.agent) for f in board.findings} == {("t1", "observability"), ("t2", "dependency")}
    reasoning_prompt = llm.prompts["reasoning"][0]
    assert "OBS-FINDING" in reasoning_prompt and "DEP-FINDING" in reasoning_prompt
    # Raw tool output (log messages) never reaches the Reasoning agent; only Findings do.
    for row in SCENARIO.logs:
        assert row.message not in reasoning_prompt
    # The scenario itself is never on the blackboard or in a planning prompt.
    dumped = board.model_dump_json()
    assert "failure_schedule" not in dumped
    for row in SCENARIO.logs:
        assert row.message not in llm.prompts["planner"][0]


def test_reasoning_writes_competing_hypotheses_and_reporting_ranks_them():
    run = investigate(INCIDENT, RoleLLM())

    hypotheses = run.report.ranked_hypotheses
    assert len(hypotheses) >= 2
    assert len({(h.service, h.category) for h in hypotheses}) == len(hypotheses)
    assert all(h.supporting_ids and h.contradicting_ids for h in hypotheses)
    assert [h.id for h in hypotheses] == ["h1", "h2"], "ranked by confidence"
    assert run.report.recommended_actions[0].hypothesis_id == "h1"
    assert run.report.outcome == "ROOT_CAUSE"
    assert run.report.terminated_by == "done"


def test_reasoning_with_fewer_than_two_hypotheses_is_asked_again():
    replies = iter([
        {"action": "finish", "hypotheses": [{"id": "h1", "service": SERVICE, "category": "bad_deploy",
         "summary": "s", "supporting_ids": ["log:x:0001"], "proposed_confidence": 0.9}]},
        reasoning_reply,
    ])  # fmt: skip

    def reasoning(prompt: str) -> Any:
        reply = next(replies)
        return reply(prompt) if callable(reply) else reply

    llm = RoleLLM(reasoning=reasoning)
    run = investigate(INCIDENT, llm)

    assert len(llm.prompts["reasoning"]) == 2
    assert "at least 2" in llm.prompts["reasoning"][1]
    assert len(run.report.ranked_hypotheses) == 2


def test_tool_gaps_and_agent_failures_are_recorded_and_the_run_still_reports():
    # inc01's schedule makes the first past_incidents call partial.
    llm = RoleLLM(
        dependency=tool_calls_then_finish([{"tool": "past_incidents", "args": {"keywords": ["checkout"]}}], "DEP"),
        observability=lambda _: "I am not sure what to do.",
    )

    run = investigate(INCIDENT, llm)

    board = run.blackboard
    assert [(g.tool, g.task_id) for g in board.gaps] == [("past_incidents", "t2")]
    assert [(f.agent, f.task_id, f.attempts) for f in board.failures] == [("observability", "t1", 2)]
    assert board.task("t1").status == "failed"
    assert board.task("t3").status == "done", "the DAG continues without the failed agent"
    assert run.report.gaps == board.gaps
    assert board.counters.llm_calls == run.report.metrics.llm_calls == 1 + 2 + 2 + 1 + 1 + 1  # + critic


def test_the_nth_call_to_a_tool_is_counted_in_plan_order_even_when_agents_race():
    # inc01's schedule makes the 2nd metrics call time out. Both investigators call metrics
    # once; the dependency agent (t2) asks first, but plan order puts t1's call first.
    metrics = {"tool": "metrics", "args": {"service": SERVICE, **WINDOW}}
    slow_first = tool_calls_then_finish([metrics], "OBS")

    def observability(prompt: str) -> Any:
        if "Tool result:" not in prompt:
            time.sleep(0.2)
        return slow_first(prompt)

    llm = RoleLLM(observability=observability, dependency=tool_calls_then_finish([metrics], "DEP"))

    run = investigate(INCIDENT, llm, tool_backoff_s=0)

    assert [(g.tool, g.task_id) for g in run.blackboard.gaps] == [("metrics", "t2")]
    assert '"status":"timeout"' in llm.prompts["dependency"][1]
    assert '"status":"ok"' in llm.prompts["observability"][1]


def test_when_gaps_keep_every_hypothesis_below_0_6_the_report_is_inconclusive_but_complete():
    # inc01's 2nd metrics call times out. That gap affects every hypothesis (metrics cover
    # every cause), so a proposed 0.65 ends at 0.55 and nothing reaches 0.6.
    metrics = {"tool": "metrics", "args": {"service": SERVICE, **WINDOW}}

    def timid_reasoning(prompt: str) -> Any:
        reply = reasoning_reply(prompt)
        reply["hypotheses"][0]["proposed_confidence"] = 0.65
        return reply

    llm = RoleLLM(
        observability=tool_calls_then_finish([metrics], "OBS"),
        dependency=tool_calls_then_finish([metrics], "DEP"),
        reasoning=timid_reasoning,
    )

    report = investigate(INCIDENT, llm, tool_backoff_s=0).report

    assert report.outcome == "INCONCLUSIVE"
    assert [(h.id, h.proposed_confidence, h.final_confidence) for h in report.ranked_hypotheses] == [
        ("h1", 0.65, 0.55),
        ("h2", 0.3, 0.2),
    ]
    assert [(g.tool, g.task_id, g.affects_hypotheses) for g in report.gaps] == [("metrics", "t2", ["h1", "h2"])]
    assert [(a.rule, a.subject) for a in report.rule_adjustments] == [("gap_penalty", "h1"), ("gap_penalty", "h2")]
    assert report.facts, "the observability Fact cites a metric row the tool returned"
    assert [a.hypothesis_id for a in report.recommended_actions] == ["h1"]


# --- A rate limit or usage cap is not an ordinary failed attempt (ticket 09, L25) ----


def _rate_limited(_: str) -> Any:
    raise LLMRateLimitError("You've hit your limit")


@pytest.mark.parametrize("role", ["planner", "observability", "dependency", "reasoning", "critic", "reporting"])
def test_a_rate_limit_in_any_multi_agent_role_propagates_out_of_investigate(role: str):
    with pytest.raises(LLMRateLimitError):
        investigate(INCIDENT, RoleLLM(delay_s=0.0, **{role: _rate_limited}), tool_backoff_s=0)


def test_a_rate_limit_in_the_baseline_propagates_out_of_investigate():
    with pytest.raises(LLMRateLimitError):
        investigate(INCIDENT, RoleLLM(delay_s=0.0, baseline=_rate_limited), mode="single", tool_backoff_s=0)


def test_an_ordinary_llm_error_is_still_a_failed_attempt_not_a_crash():
    def broken(_: str) -> Any:
        raise LLMError("bad gateway")

    run = investigate(INCIDENT, RoleLLM(delay_s=0.0, observability=broken), tool_backoff_s=0)

    assert any(f.task_id == "t1" for f in run.report.failures)


# --- Gap closure (2026-11-13): a later successful query for the same service closes the gap ---


def test_a_gap_the_critic_follow_up_recovers_is_closed_and_costs_no_confidence(monkeypatch: pytest.MonkeyPatch):
    from incident_investigator import investigate as investigate_module
    from incident_investigator.llm.mock import MockLLM
    from incident_investigator.schemas import FailureSpec

    # The first metrics call (task t1) times out; the mock Critic's follow-up (c1.1)
    # queries metrics for the same service again, and that call succeeds.
    scenario = load_scenario("inc01").model_copy(
        update={"failure_schedule": [FailureSpec(tool="metrics", call_index=1, mode="timeout")]}
    )
    monkeypatch.setattr(investigate_module, "load_scenario", lambda _id: scenario)

    run = investigate("inc01", MockLLM(delay_s=0.0), tool_backoff_s=0)

    [gap] = run.blackboard.gaps
    assert (gap.tool, gap.task_id, gap.service) == ("metrics", "t1", "checkout-svc")
    assert run.blackboard.closed_gaps == [gap.id]
    assert run.report.gaps == []
    assert not [a for a in run.report.rule_adjustments if a.rule == "gap_penalty"]
    assert [a.subject for a in run.report.rule_adjustments if a.rule == "gap_closed"] == [gap.id]


# --- A rejected API key stops the run (2026-11-13) -------------------------------------


def _bad_key(_: str) -> Any:
    raise LLMAuthError("the API rejected ANTHROPIC_API_KEY (401)")


@pytest.mark.parametrize("role", ["planner", "observability", "dependency", "reasoning", "critic", "reporting"])
def test_a_rejected_api_key_in_any_multi_agent_role_propagates_out_of_investigate(role: str):
    with pytest.raises(LLMAuthError):
        investigate(INCIDENT, RoleLLM(delay_s=0.0, **{role: _bad_key}), tool_backoff_s=0)


def test_a_rejected_api_key_in_the_baseline_propagates_out_of_investigate():
    with pytest.raises(LLMAuthError):
        investigate(INCIDENT, RoleLLM(delay_s=0.0, baseline=_bad_key), mode="single", tool_backoff_s=0)
