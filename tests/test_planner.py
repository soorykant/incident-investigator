"""The Orchestrator's one planning call: a mocked LLM reply becomes a validated task DAG (Q9)."""

import json
from typing import Any

import pytest

from incident_investigator.agents.planner import make_plan
from incident_investigator.dataset import load_scenario
from incident_investigator.llm.base import LLMResponse, Usage

INCIDENT = load_scenario("inc01").report

GOOD_PLAN = {
    "tasks": [
        {"id": "t1", "agent": "observability", "goal": "Check logs and metrics", "depends_on": []},
        {"id": "t2", "agent": "dependency", "goal": "Check downstream services", "depends_on": []},
        {"id": "t3", "agent": "reasoning", "goal": "Compare hypotheses", "depends_on": ["t1", "t2"]},
    ]
}


class ScriptedLLM:
    def __init__(self, *replies: Any):
        self.replies = [r if isinstance(r, str) else json.dumps(r) for r in replies]
        self.calls: list[dict[str, str]] = []

    def preflight(self) -> None:
        return None

    def complete(self, *, system: str, prompt: str, model: str) -> LLMResponse:
        self.calls.append({"system": system, "prompt": prompt, "model": model})
        return LLMResponse(text=self.replies.pop(0), usage=Usage(tokens_in=100, tokens_out=20))


def test_a_valid_plan_becomes_pending_plan_tasks_with_dependency_edges():
    llm = ScriptedLLM(GOOD_PLAN)

    result = make_plan(llm, incident=INCIDENT, model="claude-opus-5-5")

    assert result.failure is None
    assert [(t.id, t.agent, t.depends_on) for t in result.tasks] == [
        ("t1", "observability", []),
        ("t2", "dependency", []),
        ("t3", "reasoning", ["t1", "t2"]),
    ]
    assert {t.status for t in result.tasks} == {"pending"}
    assert {t.origin for t in result.tasks} == {"plan"}
    assert {t.loop for t in result.tasks} == {0}
    assert result.counters.llm_calls == 1
    assert result.counters.tokens_in == 100
    assert llm.calls[0]["model"] == "claude-opus-5-5"


def test_the_planner_sees_the_incident_report_but_no_scenario_data():
    llm = ScriptedLLM(GOOD_PLAN)

    make_plan(llm, incident=INCIDENT, model="claude-opus-5-5")

    prompt = llm.calls[0]["prompt"]
    assert INCIDENT.title in prompt
    for row in load_scenario("inc01").logs:
        assert row.message not in prompt
        assert row.id not in prompt


@pytest.mark.parametrize(
    ("bad_plan", "problem"),
    [
        (
            {"tasks": [
                {"id": "t1", "agent": "observability", "goal": "a", "depends_on": ["t2"]},
                {"id": "t2", "agent": "reasoning", "goal": "b", "depends_on": ["t1"]},
            ]},
            "cycle",
        ),
        (
            {"tasks": [
                {"id": "t1", "agent": "observability", "goal": "a", "depends_on": ["t9"]},
                {"id": "t2", "agent": "reasoning", "goal": "b", "depends_on": ["t1"]},
            ]},
            "unknown",
        ),
        (
            {"tasks": [
                {"id": "t1", "agent": "observability", "goal": "a"},
                {"id": "t1", "agent": "reasoning", "goal": "b"},
            ]},
            "duplicate",
        ),
        ({"tasks": [{"id": "t1", "agent": "observability", "goal": "a"}]}, "reasoning"),
        ({"tasks": [{"id": "t1", "agent": "critic", "goal": "a"}]}, "agent"),
        ("Here is my plan: look at the logs.", "JSON"),
    ],
)
def test_an_invalid_plan_is_sent_back_with_the_problem_and_a_valid_retry_is_used(bad_plan: Any, problem: str):
    llm = ScriptedLLM(bad_plan, GOOD_PLAN)

    result = make_plan(llm, incident=INCIDENT, model="claude-opus-5-5")

    assert result.failure is None
    assert [t.id for t in result.tasks] == ["t1", "t2", "t3"]
    assert len(llm.calls) == 2
    assert "not a valid plan" in llm.calls[1]["prompt"]
    assert problem in llm.calls[1]["prompt"]


def test_two_invalid_plans_fall_back_to_the_default_plan_and_record_a_planner_failure():
    llm = ScriptedLLM("no plan", {"tasks": []})

    result = make_plan(llm, incident=INCIDENT, model="claude-opus-5-5")

    assert result.failure is not None
    assert result.failure.agent == "planner"
    assert result.failure.attempts == 2
    by_agent = {t.agent: t for t in result.tasks}
    assert set(by_agent) == {"observability", "dependency", "reasoning"}
    assert by_agent["observability"].depends_on == []
    assert by_agent["dependency"].depends_on == []
    assert set(by_agent["reasoning"].depends_on) == {
        by_agent["observability"].id,
        by_agent["dependency"].id,
    }
    assert result.counters.llm_calls == 2
