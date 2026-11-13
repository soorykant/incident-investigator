"""The JSON action protocol (call_tool | finish) and the agent loop, driven through
the anthropic_api backend with a fake SDK client (PRD step 3)."""

import json
from types import SimpleNamespace
from typing import Any

import anthropic
import httpx2
import pytest

from incident_investigator.agents.actions import (
    ActionParseError,
    CallToolAction,
    FinishAction,
    parse_action,
)
from incident_investigator.agents.loop import run_agent
from incident_investigator.llm.anthropic_api import AnthropicAPI
from incident_investigator.schemas import ToolResult

FINISH = {
    "action": "finish",
    "facts": [{"statement": "errors spiked", "evidence_ids": ["log:inc01:0001"]}],
    "hypotheses": [
        {
            "id": "h1",
            "service": "checkout",
            "category": "bad_deploy",
            "summary": "deploy broke checkout",
            "supporting_ids": ["log:inc01:0001"],
            "proposed_confidence": 0.8,
        }
    ],
}
CALL_LOGS = {"action": "call_tool", "tool": "logs", "args": {"service": "checkout"}}


def test_valid_finish_json_parses_into_a_finish_action():
    action = parse_action(json.dumps(FINISH))

    assert isinstance(action, FinishAction)
    assert action.hypotheses[0].id == "h1"


def test_valid_call_tool_json_parses_into_a_call_tool_action():
    action = parse_action(json.dumps(CALL_LOGS))

    assert action == CallToolAction(action="call_tool", tool="logs", args={"service": "checkout"})


def test_a_json_code_fence_around_the_action_is_accepted():
    action = parse_action("```json\n" + json.dumps(CALL_LOGS) + "\n```")

    assert isinstance(action, CallToolAction)


@pytest.mark.parametrize(
    "text",
    [
        "I think the deploy broke it.",
        '{"action": "dance"}',
        '{"action": "call_tool", "tool": "logs", "args": {}, "extra": 1}',
    ],
)
def test_invalid_actions_raise_action_parse_error(text: str):
    with pytest.raises(ActionParseError):
        parse_action(text)


def _message(text: str) -> SimpleNamespace:
    """A Messages API reply as the anthropic SDK returns it."""
    return SimpleNamespace(
        content=[SimpleNamespace(type="text", text=text)],
        usage=SimpleNamespace(input_tokens=10, output_tokens=5, cache_creation_input_tokens=0, cache_read_input_tokens=100),
        stop_reason="end_turn",
    )


class ScriptedAPI:
    """`messages.create` stand-in that replays model replies in order."""

    def __init__(self, *results: str):
        self.results = list(results)
        self.prompts: list[str] = []

    def create(self, **kwargs: Any) -> SimpleNamespace:
        self.prompts.append(kwargs["messages"][0]["content"])
        return _message(self.results.pop(0))


def _api(messages: Any) -> AnthropicAPI:
    return AnthropicAPI(client=SimpleNamespace(messages=messages))


def _run(*results: str, tools: dict[str, Any] | None = None):
    script = ScriptedAPI(*results)
    run = run_agent(
        llm=_api(script),
        agent="observability",
        task_id="t1",
        model="claude-haiku-5-5",
        system="system",
        prompt="Investigate.",
        tools=tools or {},
    )
    return run, script


def test_invalid_json_twice_produces_an_agent_failure_instead_of_a_crash():
    run, _ = _run("not json", "still not json")

    assert run.finish is None
    assert run.failure is not None
    assert run.failure.agent == "observability"
    assert run.failure.task_id == "t1"
    assert run.failure.attempts == 2
    assert run.llm_calls == 2


def test_one_invalid_reply_is_retried_with_the_error_shown_to_the_model():
    run, script = _run("not json", json.dumps(FINISH))

    assert run.failure is None
    assert run.finish is not None
    assert "not a valid action" in script.prompts[1]


def test_call_tool_runs_the_tool_and_feeds_the_result_back():
    seen: list[dict[str, Any]] = []

    def logs(args: dict[str, Any]) -> ToolResult:
        seen.append(args)
        return ToolResult(status="ok")

    run, script = _run(json.dumps(CALL_LOGS), json.dumps(FINISH), tools={"logs": logs})

    assert seen == [{"service": "checkout"}]
    assert run.tool_calls == 1
    assert run.llm_calls == 2
    assert run.usage.tokens_in == 20
    assert run.usage.tokens_out == 10
    assert run.usage.cache_tokens == 200
    assert '"status":"ok"' in script.prompts[1]
    assert run.finish is not None


def test_an_unavailable_tool_returns_an_error_result_to_the_model():
    call_metrics = {"action": "call_tool", "tool": "metrics", "args": {}}

    run, script = _run(json.dumps(call_metrics), json.dumps(FINISH))

    assert run.finish is not None
    assert "not available" in script.prompts[1]


def test_llm_timeouts_count_as_failed_attempts():
    class Hang:
        def create(self, **kwargs: Any) -> Any:
            raise anthropic.APITimeoutError(request=httpx2.Request("POST", "https://api.anthropic.com/v1/messages"))

    run = run_agent(
        llm=_api(Hang()),
        agent="observability",
        task_id="t1",
        model="m",
        system="s",
        prompt="p",
        tools={},
    )

    assert run.failure is not None
    assert run.failure.attempts == 2
    assert "timed out" in run.failure.reason


def test_tool_calls_beyond_the_per_task_limit_count_as_failed_attempts_after_one_warning():
    # 6 calls, then a warning, then 2 failed attempts.
    calls = [json.dumps(CALL_LOGS)] * 9
    run, _ = _run(*calls, tools={"logs": lambda args: ToolResult(status="ok")})

    assert run.tool_calls == 6
    assert run.failure is not None
    assert "tool call limit" in run.failure.reason


# --- Salvage work instead of discarding it (inc04 live run, 2026-11-13) ---------------


def test_after_each_tool_result_the_agent_sees_how_many_tool_calls_it_has_left():
    _, script = _run(json.dumps(CALL_LOGS), json.dumps(FINISH), tools={"logs": lambda args: ToolResult(status="ok")})

    assert "Tool calls left in this task: 5 of 6." in script.prompts[1]


def test_an_agent_at_the_tool_limit_gets_a_finish_only_turn_and_keeps_its_work():
    replies = [json.dumps(CALL_LOGS)] * 7 + [json.dumps(FINISH)]
    run, script = _run(*replies, tools={"logs": lambda args: ToolResult(status="ok")})

    assert run.tool_calls == 6
    assert run.failure is None, "the first call over the limit is a warning, not a failed attempt"
    assert run.finish is not None and run.finish.hypotheses[0].id == "h1"
    assert "No tool calls left" in script.prompts[6]
    assert "finish now" in script.prompts[7]


def test_a_fact_without_evidence_ids_becomes_an_assumption_and_the_finish_is_kept():
    reply = {
        "action": "finish",
        "facts": [
            {"statement": "errors spiked", "evidence_ids": ["log:inc01:0001"]},
            {"statement": "no 502 data was returned", "evidence_ids": []},
            {"statement": "restarts are unverified"},
        ],
        "assumptions": [{"statement": "a", "basis": "b", "how_to_verify": "c"}],
    }

    action = parse_action(json.dumps(reply))

    assert isinstance(action, FinishAction)
    assert [f.statement for f in action.facts] == ["errors spiked"]
    assert [a.statement for a in action.assumptions] == ["a", "no 502 data was returned", "restarts are unverified"]
    assert "cites no evidence IDs" in action.assumptions[1].basis


# --- Replies with text around the JSON (inc04 live run, 2026-11-13) --------------------


@pytest.mark.parametrize(
    "text",
    [
        "Here is my final answer:\n" + json.dumps(FINISH),
        json.dumps(FINISH) + "\n\nThis cites the log row that shows the spike.",
        "Summary below.\n```json\n" + json.dumps(FINISH) + "\n```\nDone.",
    ],
    ids=["prose-before", "prose-after", "fence-inside-prose"],
)
def test_the_first_json_object_in_a_reply_is_the_action(text: str):
    action = parse_action(text)

    assert isinstance(action, FinishAction) and action.hypotheses[0].id == "h1"


def test_a_reply_that_still_fails_shows_its_start_in_the_failure_reason():
    run, _ = _run("I am not sure what to do next.", "Still thinking about it.")

    assert run.failure is not None
    assert "Still thinking about it." in run.failure.reason


# --- The run's tool budget stops tool calls, not the agent's answer --------------------


def test_an_agent_refused_by_the_run_tool_budget_gets_a_finish_only_turn():
    from incident_investigator.limits import RunStopped

    def spent(args: dict[str, Any]) -> ToolResult:
        raise RunStopped("tool_budget")

    run, script = _run(json.dumps(CALL_LOGS), json.dumps(FINISH), tools={"logs": spent})

    assert run.finish is not None and run.failure is None
    assert not run.stopped
    assert run.tool_calls == 0
    assert "tool budget is spent" in script.prompts[1]


def test_a_wall_clock_stop_from_a_tool_still_ends_the_agent_at_once():
    from incident_investigator.limits import RunStopped

    def late(args: dict[str, Any]) -> ToolResult:
        raise RunStopped("timeout")

    run, script = _run(json.dumps(CALL_LOGS), json.dumps(FINISH), tools={"logs": late})

    assert run.stopped and run.finish is None
    assert len(script.prompts) == 1
