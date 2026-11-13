"""The LLM judge (ticket 10, Q17): a qualitative second layer of evaluation.

An Opus judge (the `judge` role in models.toml) gets the answer key, the report and
every cited evidence row resolved from the scenario. It scores evidence support,
fact/assumption separation and recommended-action quality (1-5 each, with a short
rationale) and says whether the system fell for a planted trap (yes/no).

**It never decides root-cause accuracy.** That stays in `scoring.py`; the judge's
output is stored in `EvalResult.judge` and nothing reads it back into the
deterministic scores.

**Retry policy.** At most `MAX_JUDGE_ATTEMPTS` (2) calls per pair: the first, plus one
retry whose prompt names what was wrong. A reply that is not one valid `JudgeVerdict`
and an ordinary `LLMError` (timeout, crash) are both failed attempts. After the last
one the pair is recorded as a judge failure (`status = "failed"`) and the run goes on.
`LLMFatalError` (a rate limit or a rejected key) is not a failed attempt: it propagates so the runner can stop
cleanly and print the resume command.

**Cost.** The judge's LLM calls, tokens and latency are kept in `JudgeScores`, never in
the report's metrics, so they are not charged to either system (PRD D6).

**Bias (PRD R5).** The judge shares a model family with the systems it grades and may
favour their style. That is one reason it never decides accuracy.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any, Literal

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, ValidationError

from incident_investigator.dataset import AnswerKey
from incident_investigator.evaluation.scoring import cited_ids
from incident_investigator.json_text import extract_json
from incident_investigator.llm.base import LLMClient, LLMError, LLMFatalError, Usage
from incident_investigator.llm.router import RoleRouter
from incident_investigator.models_config import load_models
from incident_investigator.schemas import InvestigationReport, Scenario

ROLE = "judge"
MAX_JUDGE_ATTEMPTS = 2
NOT_IN_SCENARIO = "NOT IN SCENARIO"
_MAX_ERROR_CHARS = 500


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Score(_Strict):
    score: int = Field(ge=1, le=5)
    rationale: str = Field(min_length=1)


class TrapVerdict(_Strict):
    answer: Literal["yes", "no"]
    rationale: str = Field(min_length=1)


class JudgeVerdict(_Strict):
    """What the judge must reply with, exactly."""

    evidence_support: Score
    fact_assumption_separation: Score
    recommended_actions: Score
    fell_for_trap: TrapVerdict


class JudgeScores(_Strict):
    """`EvalResult.judge`: the verdict, or a recorded failure, plus the judge's own cost."""

    status: Literal["ok", "failed"]
    verdict: JudgeVerdict | None = None
    error: str | None = None
    """Why the last attempt failed, when `status == "failed"`."""
    model: str
    attempts: int = Field(ge=1)
    llm_calls: int = Field(ge=0)
    usage: Usage
    """Judge tokens only. Never added to the report's metrics (D6)."""
    latency_s: float
    judged_at: AwareDatetime


JUDGE_SYSTEM = (
    f"Role: {ROLE}\n"
    "You are an impartial reviewer of production incident investigation reports. You see the "
    "answer key (ground truth the investigating system never saw), the report under review, and "
    "every evidence row the report cites, resolved from the incident data. IDs marked "
    f'"{NOT_IN_SCENARIO}" do not exist: the system invented them.\n\n'
    "You do NOT decide whether the root cause is correct; that is scored separately and "
    "deterministically. Judge only the quality of the reasoning:\n"
    "- evidence_support (1-5): do the cited rows actually say what each fact and hypothesis claims? "
    "5 = every claim is backed by the rows it cites; 1 = citations are missing, invented or unrelated.\n"
    "- fact_assumption_separation (1-5): are observations kept as facts and inferences stated as "
    "assumptions (with a basis and a way to verify)? 5 = cleanly separated; 1 = guesses stated as facts.\n"
    "- recommended_actions (1-5): are the recommended actions safe, specific and sensible for what the "
    "evidence shows, given the answer key? 5 = what a good on-call engineer would do; 1 = none, harmful "
    "or irrelevant.\n"
    "- fell_for_trap (yes/no): the answer key lists planted misleading evidence. Answer yes if the "
    "report's top-ranked hypothesis, or its conclusion, relies on that misleading evidence or the "
    "wrong story it suggests; no if the report avoided it or explicitly ruled it out.\n"
    "Give each a short rationale (one or two sentences) that names evidence IDs where it helps.\n\n"
    "Reply with exactly one JSON object and nothing else:\n"
    '{"evidence_support": {"score": 1-5, "rationale": str}, '
    '"fact_assumption_separation": {"score": 1-5, "rationale": str}, '
    '"recommended_actions": {"score": 1-5, "rationale": str}, '
    '"fell_for_trap": {"answer": "yes" | "no", "rationale": str}}'
)


def judge_client(llm: LLMClient) -> tuple[LLMClient, str]:
    """The client and model for the judge role: the router's judge backend, or `llm` itself."""
    if isinstance(llm, RoleRouter):
        return llm.client_for(ROLE), llm.models.model_for(ROLE)
    return llm, load_models().model_for(ROLE)


def _scenario_rows(scenario: Scenario) -> dict[str, dict[str, Any]]:
    groups: list[tuple[str, list[Any]]] = [
        ("log", scenario.logs),
        ("metric", scenario.metrics),
        ("deploy", scenario.deploys),
        ("config", scenario.config_changes),
        ("dependency", scenario.dependencies),
        ("past", scenario.past_incidents),
    ]
    return {row.id: {"kind": kind, **row.model_dump(mode="json")} for kind, rows in groups for row in rows}


def _cited_evidence(report: InvestigationReport, scenario: Scenario) -> dict[str, Any]:
    rows = _scenario_rows(scenario)
    return {i: rows.get(i, NOT_IN_SCENARIO) for i in dict.fromkeys(cited_ids(report))}


def _compact(value: Any) -> str:
    return json.dumps(value, separators=(", ", ": "), ensure_ascii=False)


def build_prompt(report: InvestigationReport, answer_key: AnswerKey, scenario: Scenario) -> str:
    under_review = report.model_dump(mode="json", exclude={"metrics"})
    return (
        f"Incident report:\n{_compact(scenario.report.model_dump(mode='json'))}\n\n"
        f"Answer key (ground truth):\n{_compact(answer_key.model_dump(mode='json'))}\n\n"
        f"Report under review:\n{_compact(under_review)}\n\n"
        f"Cited evidence (each cited ID resolved from the incident data):\n"
        f"{_compact(_cited_evidence(report, scenario))}\n"
    )


def _parse(text: str) -> JudgeVerdict:
    return JudgeVerdict.model_validate_json(extract_json(text))


def _short(exc: ValidationError) -> str:
    errors = exc.errors(include_url=False)[:3]
    return "; ".join(f"{'.'.join(map(str, e['loc'])) or 'reply'}: {e['msg']}" for e in errors)


def judge_report(
    llm: LLMClient, *, report: InvestigationReport, answer_key: AnswerKey, scenario: Scenario, model: str
) -> JudgeScores:
    """Judge one report. Never raises for bad judge output; raises `LLMFatalError` (rate limit or rejected key)."""
    prompt = build_prompt(report, answer_key, scenario)
    usage, latency, calls = Usage(), 0.0, 0
    error = ""
    attempt = 0
    for attempt in range(1, MAX_JUDGE_ATTEMPTS + 1):
        text = prompt
        if error:
            text += (
                f"\nYour previous reply was invalid: {error}\n"
                "Reply again with exactly one JSON object in the required format."
            )
        calls += 1
        try:
            response = llm.complete(system=JUDGE_SYSTEM, prompt=text, model=model)
        except LLMFatalError:
            raise
        except LLMError as exc:
            error = f"LLM call failed: {exc}"[:_MAX_ERROR_CHARS]
            continue
        usage += response.usage
        latency += response.latency_s
        try:
            verdict = _parse(response.text)
        except ValidationError as exc:
            error = _short(exc)[:_MAX_ERROR_CHARS]
            continue
        return JudgeScores(
            status="ok", verdict=verdict, model=model, attempts=attempt, llm_calls=calls, usage=usage,
            latency_s=round(latency, 3), judged_at=datetime.now(UTC),
        )  # fmt: skip
    return JudgeScores(
        status="failed", error=error, model=model, attempts=attempt, llm_calls=calls, usage=usage,
        latency_s=round(latency, 3), judged_at=datetime.now(UTC),
    )  # fmt: skip
