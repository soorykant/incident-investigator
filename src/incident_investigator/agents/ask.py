"""One structured LLM answer (no tools), with the same 2-failed-attempt rule as the agent loop.

Used by the planner and the Reporting agent, whose replies are not call_tool/finish actions.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field

from incident_investigator.agents.loop import MAX_FAILED_ATTEMPTS
from incident_investigator.blackboard import AgentFailure, Counters
from incident_investigator.json_text import extract_json
from incident_investigator.limits import RunStopped
from incident_investigator.llm.base import LLMClient, LLMError, LLMFatalError


class AnswerError(ValueError):
    """The reply is not acceptable; the message is shown to the model on the retry."""




def strip_fence(text: str) -> str:
    """The first JSON object in the reply; text and code fences around it are ignored."""
    return extract_json(text)


@dataclass
class Answer[T]:
    value: T | None = None
    failure: AgentFailure | None = None
    counters: Counters = field(default_factory=Counters)


def ask[T](
    llm: LLMClient,
    *,
    agent: str,
    task_id: str,
    model: str,
    system: str,
    prompt: str,
    parse: Callable[[str], T],
    retry_hint: str = "Your last reply was not valid",
    max_failed_attempts: int = MAX_FAILED_ATTEMPTS,
) -> Answer[T]:
    """Ask until `parse` accepts the reply (it raises AnswerError otherwise).

    A run limit (`RunStopped`) ends the asking with no value and no failure."""
    answer: Answer[T] = Answer()
    transcript = prompt
    failed = 0
    while True:
        try:
            try:
                response = llm.complete(system=system, prompt=transcript, model=model)
            except RunStopped:
                return answer
            except LLMError:
                answer.counters = answer.counters.add(llm_calls=1)
                raise
            answer.counters = answer.counters.add(llm_calls=1)
            answer.counters = answer.counters.add(usage=response.usage, llm_latency_s=response.latency_s)
            answer.value = parse(strip_fence(response.text))
            return answer
        except LLMFatalError:
            raise  # a rate limit or a rejected key: not a failed attempt, the whole run must stop (L25)
        except (LLMError, AnswerError) as exc:
            failed += 1
            reason = f"{type(exc).__name__}: {exc}"
            if failed >= max_failed_attempts:
                answer.failure = AgentFailure(agent=agent, task_id=task_id, reason=reason, attempts=failed)
                return answer
            transcript = f"{prompt}\n\n{retry_hint} ({reason}). Reply again with one JSON object."
