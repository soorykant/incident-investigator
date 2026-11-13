"""The agent loop: ask the model for an action, run tools in Python, repeat until finish.

The CLI backend has no native tool calling (Q15), so each step re-sends the whole
transcript as one prompt. Limits (from `limits.RunLimits`): 6 tool calls per task,
2 failed attempts (invalid reply, LLM error or timeout, tool call over the limit)
-> AgentFailure. After each tool result the agent sees how many calls it has left. The
first call over the limit is only a warning, so an agent that ran out of calls gets
one finish-only turn and keeps its work; later calls over the limit are failed attempts.
The run's tool budget works the same way: a refused call gives a finish-only turn.
The other run limits (`RunStopped` for the token budget or the wall clock) end the loop
cleanly: the run keeps the agent's evidence and usage, with no finish and no failure.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import Any

from incident_investigator.agents.actions import (
    ActionParseError,
    AgentFailure,
    CallToolAction,
    FinishAction,
    parse_action,
)
from incident_investigator.json_text import reply_start
from incident_investigator.limits import DEFAULT_LIMITS, RunStopped
from incident_investigator.llm.base import LLMClient, LLMError, LLMFatalError, Usage
from incident_investigator.schemas import Evidence, ToolResult

MAX_TOOL_CALLS_PER_TASK = DEFAULT_LIMITS.max_tool_calls_per_task
MAX_FAILED_ATTEMPTS = DEFAULT_LIMITS.max_failed_attempts

Tool = Callable[[dict[str, Any]], ToolResult]


@dataclass
class AgentRun:
    finish: FinishAction | None = None
    failure: AgentFailure | None = None
    evidence: list[Evidence] = field(default_factory=list[Evidence])
    tool_calls: int = 0
    llm_calls: int = 0
    usage: Usage = field(default_factory=Usage)
    llm_latency_s: float = 0.0
    stopped: bool = False
    """A run limit stopped this task before it finished."""


def run_agent(
    *,
    llm: LLMClient,
    agent: str,
    task_id: str,
    model: str,
    system: str,
    prompt: str,
    tools: Mapping[str, Tool],
    max_tool_calls: int | None = MAX_TOOL_CALLS_PER_TASK,
    max_failed_attempts: int = MAX_FAILED_ATTEMPTS,
    check_finish: Callable[[FinishAction], str | None] | None = None,
) -> AgentRun:
    """Run one agent task to a finish action or an AgentFailure.

    `check_finish` may reject a finish action with a reason (for example, too few
    hypotheses); a rejection counts as a failed attempt and the reason is shown to
    the model. `max_tool_calls=None` sets no per-task cap: only the run budget (the
    RunGuard behind the tools) can stop tool calls (the single-agent baseline)."""
    run = AgentRun()
    transcript = prompt
    failed = 0
    warned = False
    tools_spent = False
    """The run's tool budget refused a call: from now on the agent may only finish."""
    while True:
        try:
            response = llm.complete(system=system, prompt=transcript, model=model)
        except RunStopped:
            run.stopped = True
            return run
        except LLMFatalError:
            raise  # a rate limit or a rejected key: not a failed attempt, the whole run must stop (L25)
        except LLMError as exc:
            response = exc
        run.llm_calls += 1
        try:
            if isinstance(response, LLMError):
                raise response
            run.usage = run.usage + response.usage
            run.llm_latency_s += response.latency_s
            try:
                action = parse_action(response.text)
            except ActionParseError as exc:
                raise ActionParseError(f"{exc} (reply began: {reply_start(response.text)!r})") from exc
            if isinstance(action, CallToolAction):
                if tools_spent:
                    raise _OverLimit("the run's tool budget is spent; finish now")
                if max_tool_calls is not None and run.tool_calls >= max_tool_calls:
                    raise _OverLimit(f"tool call limit of {max_tool_calls} reached; finish now")
            if isinstance(action, FinishAction) and check_finish is not None:
                problem = check_finish(action)
                if problem is not None:
                    raise _Rejected(problem)
        except (LLMError, ActionParseError, _OverLimit, _Rejected) as exc:
            if isinstance(exc, _OverLimit) and not warned:
                warned = True  # the first call over a limit is a warning: one finish-only turn
                transcript += f"\n\nYou have no tool calls left ({exc}). Reply with a finish action."
                continue
            failed += 1
            reason = f"{type(exc).__name__}: {exc}"
            if failed >= max_failed_attempts:
                run.failure = AgentFailure(agent=agent, task_id=task_id, reason=reason, attempts=failed)
                return run
            transcript += f"\n\nYour last reply was not a valid action ({reason}). Reply again."
            continue

        if isinstance(action, FinishAction):
            run.finish = action
            return run

        try:
            result = _call(tools, action)
        except RunStopped as stop:
            if stop.reason != "tool_budget":
                run.stopped = True  # the token budget or the wall clock: stop at once
                return run
            # The tool budget stops tool calls, not the agent's answer: one finish-only turn.
            tools_spent = warned = True
            transcript += (
                "\n\nThe run's tool budget is spent: that call was not made, and no tool calls are "
                "left. Reply with a finish action now, from the evidence you already have."
            )
            continue
        run.tool_calls += 1
        run.evidence += result.evidence
        transcript += (
            f"\n\nYou called: {json.dumps(action.model_dump())}\n"
            f"Tool result: {result.model_dump_json(exclude_none=True)}"
        )
        if max_tool_calls is not None:
            left = max_tool_calls - run.tool_calls
            transcript += (
                f"\nTool calls left in this task: {left} of {max_tool_calls}."
                if left
                else "\nNo tool calls left in this task: your next reply must be a finish action."
            )


class _OverLimit(Exception):
    pass


class _Rejected(Exception):
    pass


def _call(tools: Mapping[str, Tool], action: CallToolAction) -> ToolResult:
    tool = tools.get(action.tool)
    if tool is None:
        return ToolResult(
            status="error",
            error=f"tool {action.tool!r} is not available to this agent; available: {sorted(tools)}",
        )
    return tool(action.args)
