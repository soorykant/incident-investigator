"""Deterministic, pure scoring of one InvestigationReport against its answer key (Q12, Q17).

The LLM judge (ticket 10) never decides accuracy: that stays here, so a judge
mistake cannot change the main metric.

**Accuracy.** The expected outcome is the answer key's `expected_outcome`
(`root_cause` / `inconclusive`) mapped case-insensitively onto the report's
`ROOT_CAUSE` / `INCONCLUSIVE`.
- Inconclusive by design: correct only when the outcome is INCONCLUSIVE.
- Root cause expected: correct only when the outcome is ROOT_CAUSE *and* the
  top-ranked hypothesis matches the key's service and category (case- and
  whitespace-insensitive). An INCONCLUSIVE answer is incorrect, even when the
  top hypothesis would have matched.

**Grounding.** The share of evidence-ID citations that exist in the scenario.
A citation is one ID in a Fact's `evidence_ids` or a ranked hypothesis's
`supporting_ids` / `contradicting_ids`, counted once per place it appears. The
evidence rules strip IDs no tool returned *before* the report is built, so the
report alone would always look fully grounded; the IDs they removed
(`fact_demoted` and `hypothesis_ids_dropped` adjustments) are therefore counted as
citations too. A report with zero citations has `grounding = None` ("n/a"): it made
no evidence claims, grounded or not, and averages skip it.
"""

from __future__ import annotations

from collections.abc import Collection
from typing import Literal

from pydantic import BaseModel, ConfigDict

from incident_investigator.dataset import AnswerKey
from incident_investigator.schemas import Category, InvestigationReport

Outcome = Literal["ROOT_CAUSE", "INCONCLUSIVE"]
AccuracyReason = Literal[
    "match", "outcome_mismatch", "no_hypothesis", "wrong_service", "wrong_category", "wrong_service_and_category"
]
# Adjustments whose `ids` are citations the evidence rules removed from the report.
_REMOVED_CITATIONS = {"fact_demoted", "hypothesis_ids_dropped"}
_GROUNDING_DECIMALS = 3


class Accuracy(BaseModel):
    model_config = ConfigDict(extra="forbid")

    correct: bool
    reason: AccuracyReason


class Grounding(BaseModel):
    model_config = ConfigDict(extra="forbid")

    citations: int
    grounded: int
    grounding: float | None
    """grounded / citations, rounded to 3 decimals; None when there are no citations."""
    unknown_ids: list[str]
    """Distinct cited IDs that are not in the scenario, sorted."""


class DeterministicScores(BaseModel):
    """The deterministic half of an EvalResult. Cost is read from `report.metrics`."""

    model_config = ConfigDict(extra="forbid")

    expected_outcome: Outcome
    expected_service: str | None
    expected_category: Category | None
    correct: bool
    accuracy_reason: AccuracyReason
    citations: int
    grounded: int
    grounding: float | None
    unknown_ids: list[str]


def _norm(text: str) -> str:
    return text.strip().casefold()


def expected_outcome(key: AnswerKey) -> Outcome:
    return "INCONCLUSIVE" if _norm(key.expected_outcome) == "inconclusive" else "ROOT_CAUSE"


def score_accuracy(report: InvestigationReport, key: AnswerKey) -> Accuracy:
    expected = expected_outcome(key)
    if _norm(report.outcome) != _norm(expected):
        return Accuracy(correct=False, reason="outcome_mismatch")
    if expected == "INCONCLUSIVE":
        return Accuracy(correct=True, reason="match")
    if not report.ranked_hypotheses:
        return Accuracy(correct=False, reason="no_hypothesis")
    top = report.ranked_hypotheses[0]
    service_ok = _norm(top.service) == _norm(key.service or "")
    category_ok = _norm(top.category) == _norm(key.category or "")
    if service_ok and category_ok:
        return Accuracy(correct=True, reason="match")
    if category_ok:
        return Accuracy(correct=False, reason="wrong_service")
    if service_ok:
        return Accuracy(correct=False, reason="wrong_category")
    return Accuracy(correct=False, reason="wrong_service_and_category")


def cited_ids(report: InvestigationReport) -> list[str]:
    """Every citation in the report, plus the ones the evidence rules removed."""
    cited = [i for fact in report.facts for i in fact.evidence_ids]
    for h in report.ranked_hypotheses:
        cited += [*h.supporting_ids, *h.contradicting_ids]
    cited += [i for adj in report.rule_adjustments if adj.rule in _REMOVED_CITATIONS for i in adj.ids]
    return cited


def score_grounding(report: InvestigationReport, scenario_ids: Collection[str]) -> Grounding:
    cited = cited_ids(report)
    grounded = sum(1 for i in cited if i in scenario_ids)
    return Grounding(
        citations=len(cited),
        grounded=grounded,
        grounding=round(grounded / len(cited), _GROUNDING_DECIMALS) if cited else None,
        unknown_ids=sorted({i for i in cited if i not in scenario_ids}),
    )


def score(report: InvestigationReport, key: AnswerKey, scenario_ids: Collection[str]) -> DeterministicScores:
    accuracy = score_accuracy(report, key)
    grounding = score_grounding(report, scenario_ids)
    return DeterministicScores(
        expected_outcome=expected_outcome(key),
        expected_service=key.service,
        expected_category=key.category,
        correct=accuracy.correct,
        accuracy_reason=accuracy.reason,
        citations=grounding.citations,
        grounded=grounding.grounded,
        grounding=grounding.grounding,
        unknown_ids=grounding.unknown_ids,
    )
