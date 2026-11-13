"""The Critic agent (Q16, PRD step 5): reviews the hypotheses and may ask for more evidence.

It reads the blackboard (incident report, ranked hypotheses after the evidence rules,
findings and gaps) and replies with a critique and up to 3 follow-up investigations.
`follow_up_tasks` turns a review into blackboard Tasks for the Orchestrator's DAG:
one investigator task per follow-up, then a Reasoning task that depends on all of
them and revises the hypotheses. Every one has `origin="critic"` and the loop number.
"""

from __future__ import annotations

import json
from collections.abc import Sequence

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from incident_investigator.agents.ask import Answer, AnswerError, ask
from incident_investigator.agents.prompts import CRITIC_SYSTEM
from incident_investigator.blackboard import Blackboard, CriticReview, FollowUp, Task
from incident_investigator.evidence_rules import rank
from incident_investigator.limits import DEFAULT_LIMITS, RunLimits
from incident_investigator.llm.base import LLMClient

MAX_FOLLOW_UPS = 3


class _Reply(BaseModel):
    model_config = ConfigDict(extra="forbid")

    critique: str
    follow_ups: list[FollowUp] = Field(default=[], max_length=MAX_FOLLOW_UPS)


def critic_task_id(loop: int) -> str:
    return f"critic{loop}"


def review(
    llm: LLMClient,
    *,
    board: Blackboard,
    model: str,
    limits: RunLimits = DEFAULT_LIMITS,
    tool_calls_left: int | None = None,
) -> Answer[CriticReview]:
    """Ask the Critic about the current blackboard. `loop` on the review is the loop it
    would start (board.critic_loops + 1). `tool_calls_left` (from the RunGuard) is shown
    so the Critic asks only for follow-ups the run can still pay for."""
    loop = board.critic_loops + 1

    def parse(text: str) -> CriticReview:
        try:
            reply = _Reply.model_validate_json(text)
        except ValidationError as exc:
            raise AnswerError(str(exc.errors(include_url=False)[:3])) from exc
        return CriticReview(loop=loop, critique=reply.critique, follow_ups=reply.follow_ups)

    def dump(items: Sequence[BaseModel]) -> str:
        return json.dumps([i.model_dump(mode="json") for i in items], indent=1)

    prompt = (
        f"Incident report:\n{board.incident.model_dump_json(indent=1)}\n\n"
        f"Hypotheses, highest final confidence first:\n{dump(list(rank(board.hypotheses)))}\n\n"
        f"Findings on the blackboard:\n{dump(list(board.findings))}\n\n"
        f"Open evidence gaps (data that could not be read):\n{dump(board.open_gaps())}\n\n"
        f"Critic loops used so far: {board.critic_loops} of {limits.max_critic_loops}."
    )
    if tool_calls_left is not None:
        prompt += (
            f"\nTool calls left in this run: {tool_calls_left} of {limits.max_tool_calls_per_run}. "
            f"Each follow-up investigation may use at most {limits.max_tool_calls_per_task} tool calls, "
            "and the run stops when the budget runs out. Ask only for the follow-ups the budget can pay "
            "for, most important first."
        )
    return ask(
        llm,
        agent="critic",
        task_id=critic_task_id(loop),
        model=model,
        system=CRITIC_SYSTEM,
        prompt=prompt,
        parse=parse,
        retry_hint="Your last reply was not a valid review",
        max_failed_attempts=limits.max_failed_attempts,
    )


def follow_up_tasks(critic_review: CriticReview) -> list[Task]:
    """The follow-up investigations, then a Reasoning task that revises the hypotheses."""
    loop = critic_review.loop
    investigators = [
        Task(
            id=f"c{loop}.{i}",
            agent=f.agent,
            goal=f.goal,
            params=f.params,
            origin="critic",
            loop=loop,
        )
        for i, f in enumerate(critic_review.follow_ups, start=1)
    ]
    reasoning = Task(
        id=f"c{loop}.r",
        agent="reasoning",
        goal=f"Revise the hypotheses with the new findings. The Critic said: {critic_review.critique}",
        depends_on=[t.id for t in investigators],
        origin="critic",
        loop=loop,
    )
    return [*investigators, reasoning]
