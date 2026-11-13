"""Deterministic scoring on hand-built reports and answer keys, no LLM (ticket 09, Q12, Q17)."""

from typing import Any

import pytest

from incident_investigator.dataset import AnswerKey
from incident_investigator.evaluation.scoring import score, score_accuracy, score_grounding
from incident_investigator.schemas import (
    Fact,
    Hypothesis,
    InvestigationReport,
    ReportMetrics,
    RuleAdjustment,
)

KEY = AnswerKey(
    incident_id="incXX",
    expected_outcome="root_cause",
    service="checkout-svc",
    category="bad_deploy",
    key_evidence_ids=["log:incXX:0001"],
)
INCONCLUSIVE_KEY = AnswerKey(
    incident_id="incXX",
    expected_outcome="inconclusive",
    service=None,
    category=None,
    key_evidence_ids=[],
)
KNOWN = {"log:incXX:0001", "log:incXX:0002", "deploy:incXX:0001", "metric:incXX:cpu:0001"}


def hyp(hid: str, service: str, category: Any, *, supporting: list[str], contradicting: list[str] | None = None) -> Hypothesis:
    return Hypothesis(
        id=hid,
        service=service,
        category=category,
        summary=f"{service} {category}",
        supporting_ids=supporting,
        contradicting_ids=contradicting or [],
        proposed_confidence=0.7,
        final_confidence=0.7,
    )


def report(
    outcome: Any = "ROOT_CAUSE",
    hypotheses: list[Hypothesis] | None = None,
    facts: list[Fact] | None = None,
    adjustments: list[RuleAdjustment] | None = None,
) -> InvestigationReport:
    if hypotheses is None:
        hypotheses = [
            hyp("h1", "checkout-svc", "bad_deploy", supporting=["deploy:incXX:0001"]),
            hyp("h2", "payments-svc", "cert_dns", supporting=["log:incXX:0002"]),
        ]
    return InvestigationReport(
        incident_id="incXX",
        outcome=outcome,
        ranked_hypotheses=hypotheses,
        facts=facts if facts is not None else [Fact(statement="deploy at 10:00", evidence_ids=["deploy:incXX:0001"])],
        assumptions=[],
        metrics=ReportMetrics(),
        terminated_by="done",
        rule_adjustments=adjustments or [],
    )


# --- Accuracy ------------------------------------------------------------------


def test_top_hypothesis_matching_service_and_category_is_correct():
    result = score_accuracy(report(), KEY)

    assert result.correct is True
    assert result.reason == "match"


def test_service_and_category_match_ignores_case_and_surrounding_space():
    top = hyp("h1", " Checkout-SVC ", "bad_deploy", supporting=["deploy:incXX:0001"])

    assert score_accuracy(report(hypotheses=[top]), KEY).correct is True


def test_wrong_service_is_incorrect():
    top = hyp("h1", "payments-svc", "bad_deploy", supporting=["deploy:incXX:0001"])

    result = score_accuracy(report(hypotheses=[top]), KEY)

    assert result.correct is False
    assert result.reason == "wrong_service"


def test_wrong_category_is_incorrect():
    top = hyp("h1", "checkout-svc", "config_change", supporting=["deploy:incXX:0001"])

    result = score_accuracy(report(hypotheses=[top]), KEY)

    assert result.correct is False
    assert result.reason == "wrong_category"


def test_wrong_service_and_category_is_incorrect():
    top = hyp("h1", "payments-svc", "cert_dns", supporting=["log:incXX:0002"])

    assert score_accuracy(report(hypotheses=[top]), KEY).reason == "wrong_service_and_category"


def test_only_the_top_ranked_hypothesis_counts():
    right_but_second = [
        hyp("h1", "payments-svc", "cert_dns", supporting=["log:incXX:0002"]),
        hyp("h2", "checkout-svc", "bad_deploy", supporting=["deploy:incXX:0001"]),
    ]

    assert score_accuracy(report(hypotheses=right_but_second), KEY).correct is False


def test_a_root_cause_incident_answered_inconclusive_is_incorrect_even_if_the_top_hypothesis_matches():
    result = score_accuracy(report(outcome="INCONCLUSIVE"), KEY)

    assert result.correct is False
    assert result.reason == "outcome_mismatch"


def test_a_root_cause_report_with_no_hypotheses_is_incorrect():
    result = score_accuracy(report(hypotheses=[]), KEY)

    assert result.correct is False
    assert result.reason == "no_hypothesis"


def test_an_inconclusive_by_design_incident_answered_inconclusive_is_correct():
    result = score_accuracy(report(outcome="INCONCLUSIVE"), INCONCLUSIVE_KEY)

    assert result.correct is True
    assert result.reason == "match"


def test_an_inconclusive_by_design_incident_answered_root_cause_is_incorrect():
    result = score_accuracy(report(outcome="ROOT_CAUSE"), INCONCLUSIVE_KEY)

    assert result.correct is False
    assert result.reason == "outcome_mismatch"


# --- Grounding -------------------------------------------------------------------


def test_every_citation_existing_in_the_scenario_grounds_fully():
    result = score_grounding(report(), KNOWN)

    # fact: deploy:0001; h1 supports deploy:0001; h2 supports log:0002
    assert result.citations == 3
    assert result.grounded == 3
    assert result.grounding == 1.0
    assert result.unknown_ids == []


def test_fake_ids_count_against_grounding():
    hypotheses = [
        hyp("h1", "checkout-svc", "bad_deploy", supporting=["deploy:incXX:0001", "log:incXX:9999"],
            contradicting=["metric:incXX:cpu:0001"]),
        hyp("h2", "payments-svc", "cert_dns", supporting=["made-up-id"]),
    ]  # fmt: skip
    facts = [Fact(statement="s", evidence_ids=["log:incXX:0001", "log:incXX:9999"])]

    result = score_grounding(report(hypotheses=hypotheses, facts=facts), KNOWN)

    # citations: fact 2 + h1 3 + h2 1 = 6; unknown: log:9999 twice, made-up-id once
    assert result.citations == 6
    assert result.grounded == 3
    assert result.grounding == 0.5
    assert result.unknown_ids == ["log:incXX:9999", "made-up-id"]


def test_ids_the_evidence_rules_removed_still_count_as_citations():
    """The rules strip IDs no tool returned before the report is built, so the report
    alone would always look fully grounded. The removed IDs are in rule_adjustments."""
    adjustments = [
        RuleAdjustment(rule="fact_demoted", subject="t1/fact[0]", detail="d", ids=["log:incXX:9999"]),
        RuleAdjustment(rule="hypothesis_ids_dropped", subject="h1", detail="d", ids=["log:incXX:0002"]),
        RuleAdjustment(rule="unsupported_cap", subject="h2", detail="d", ids=["log:incXX:0002"]),
    ]

    result = score_grounding(report(adjustments=adjustments), KNOWN)

    # report: 3 grounded; removed: log:9999 (not in scenario), log:0002 (in scenario, no tool
    # returned it in this run); unsupported_cap does not remove a citation.
    assert result.citations == 5
    assert result.grounded == 4
    assert result.grounding == 0.8
    assert result.unknown_ids == ["log:incXX:9999"]


def test_zero_citations_has_no_grounding_score():
    result = score_grounding(report(hypotheses=[], facts=[]), KNOWN)

    assert result.citations == 0
    assert result.grounded == 0
    assert result.grounding is None


def test_grounding_is_rounded_to_three_decimals():
    facts = [Fact(statement="s", evidence_ids=["log:incXX:0001", "log:incXX:0002", "nope"])]

    result = score_grounding(report(hypotheses=[], facts=facts), KNOWN)

    assert result.grounding == 0.667


# --- Combined ----------------------------------------------------------------------


@pytest.mark.parametrize(("outcome", "expected"), [("INCONCLUSIVE", True), ("ROOT_CAUSE", False)])
def test_score_combines_accuracy_and_grounding_and_records_the_expectation(outcome: Any, expected: bool):
    scores = score(report(outcome=outcome), INCONCLUSIVE_KEY, KNOWN)

    assert scores.correct is expected
    assert scores.expected_outcome == "INCONCLUSIVE"
    assert scores.expected_service is None
    assert scores.grounding == 1.0
    assert scores.citations == 3
