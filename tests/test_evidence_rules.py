"""The deterministic evidence rules (PRD step 7, Q16, Q21) on hand-built blackboards. No LLM."""

from datetime import UTC, datetime
from typing import Any

from incident_investigator.blackboard import Blackboard, Task
from incident_investigator.evidence_rules import apply_evidence_rules, build_report
from incident_investigator.schemas import (
    Assumption,
    Evidence,
    EvidenceGap,
    Fact,
    Finding,
    Hypothesis,
    IncidentReport,
    RecommendedAction,
    ReportMetrics,
)

T0 = datetime(2026, 1, 1, 12, 0, tzinfo=UTC)
INCIDENT = IncidentReport(
    title="Checkout errors",
    symptoms=["5xx"],
    affected_service="checkout-svc",
    window_start=T0,
    window_end=T0,
    severity="SEV2",
)
LOG = "log:inc01:0001"
DEPLOY = "deploy:inc01:0001"
METRIC = "metric:inc01:err:0001"
GHOST = "log:inc01:9999"


def evidence(*ids: str) -> list[Evidence]:
    return [Evidence(id=i, kind=i.split(":")[0], payload={}) for i in ids]  # type: ignore[arg-type]


def fact(*ids: str, statement: str = "observed") -> Fact:
    return Fact(statement=statement, evidence_ids=list(ids))


def hyp(hid: str, confidence: float, supporting: list[str], category: str = "bad_deploy", **kw: Any) -> Hypothesis:
    return Hypothesis(
        id=hid,
        service="checkout-svc",
        category=category,  # type: ignore[arg-type]
        summary=hid,
        supporting_ids=supporting,
        proposed_confidence=confidence,
        **kw,
    )


def board(
    *,
    facts: list[Fact] = (),  # type: ignore[assignment]
    hypotheses: list[Hypothesis] = (),  # type: ignore[assignment]
    gaps: list[EvidenceGap] = (),  # type: ignore[assignment]
    assumptions: list[Assumption] = (),  # type: ignore[assignment]
    known: tuple[str, ...] = (LOG, DEPLOY, METRIC),
    closed_gaps: list[str] = (),  # type: ignore[assignment]
) -> Blackboard:
    return Blackboard(
        incident_id="inc01",
        incident=INCIDENT,
        started_at=T0,
        tasks=[Task(id="t1", agent="observability", goal="look")],
        findings=[Finding(task_id="t1", agent="observability", facts=list(facts), assumptions=list(assumptions))],
        evidence=evidence(*known),
        hypotheses=list(hypotheses),
        gaps=list(gaps),
        closed_gaps=list(closed_gaps),
    )


def final(result: Any, hid: str) -> float | None:
    return next(h.final_confidence for h in result.hypotheses if h.id == hid)


# --- Rule 1: Facts and hypotheses cite only evidence a tool returned in this run ---------


def test_a_fact_citing_only_returned_evidence_is_kept():
    result = apply_evidence_rules(board(facts=[fact(LOG, DEPLOY)]))

    assert result.facts == [fact(LOG, DEPLOY)]
    assert result.assumptions == []
    assert result.adjustments == []


def test_a_fact_citing_an_unknown_evidence_id_becomes_an_assumption_and_is_logged():
    result = apply_evidence_rules(board(facts=[fact(LOG, GHOST, statement="deploy broke checkout")]))

    assert result.facts == []
    [assumption] = result.assumptions
    assert assumption.statement == "deploy broke checkout"
    assert GHOST in assumption.basis and LOG in assumption.basis
    assert assumption.how_to_verify
    [log] = result.adjustments
    assert (log.rule, log.subject, log.ids) == ("fact_demoted", "t1/fact[0]", [GHOST])


def test_evidence_that_exists_in_the_dataset_but_was_never_returned_does_not_count():
    # METRIC is a real scenario ID, but no tool returned it during this run.
    result = apply_evidence_rules(board(facts=[fact(METRIC)], known=(LOG,)))

    assert result.facts == []
    assert [a.rule for a in result.adjustments] == ["fact_demoted"]


def test_agent_assumptions_are_kept_apart_from_facts():
    guess = Assumption(statement="maybe CPU", basis="past incident", how_to_verify="check cpu")

    result = apply_evidence_rules(board(facts=[fact(LOG)], assumptions=[guess]))

    assert result.facts == [fact(LOG)]
    assert result.assumptions == [guess]


def test_unknown_ids_are_dropped_from_hypotheses_and_logged():
    h = hyp("h1", 0.8, [LOG, GHOST], contradicting_ids=[GHOST, DEPLOY])

    result = apply_evidence_rules(board(facts=[fact(LOG)], hypotheses=[h]))

    [out] = result.hypotheses
    assert (out.supporting_ids, out.contradicting_ids) == ([LOG], [DEPLOY])
    assert [(a.rule, a.subject, a.ids) for a in result.adjustments] == [("hypothesis_ids_dropped", "h1", [GHOST])]


# --- Rule 2: no supporting Fact caps confidence at 0.5 -----------------------------------


def test_a_hypothesis_backed_by_a_kept_fact_keeps_its_proposed_confidence():
    result = apply_evidence_rules(board(facts=[fact(DEPLOY)], hypotheses=[hyp("h1", 0.8, [DEPLOY])]))

    assert final(result, "h1") == 0.8
    assert result.adjustments == []


def test_a_hypothesis_with_no_supporting_fact_is_capped_at_half():
    # DEPLOY was returned by a tool, but no Fact cites it.
    result = apply_evidence_rules(board(facts=[fact(LOG)], hypotheses=[hyp("h1", 0.8, [DEPLOY])]))

    assert final(result, "h1") == 0.5
    [log] = result.adjustments
    assert (log.rule, log.subject, log.confidence_before, log.confidence_after) == ("unsupported_cap", "h1", 0.8, 0.5)


def test_a_fact_demoted_to_an_assumption_does_not_support_a_hypothesis():
    result = apply_evidence_rules(board(facts=[fact(DEPLOY, GHOST)], hypotheses=[hyp("h1", 0.9, [DEPLOY])]))

    assert final(result, "h1") == 0.5
    assert [a.rule for a in result.adjustments] == ["fact_demoted", "unsupported_cap"]


def test_the_cap_never_raises_a_low_confidence():
    result = apply_evidence_rules(board(hypotheses=[hyp("h1", 0.3, [DEPLOY])]))

    assert final(result, "h1") == 0.3
    assert result.adjustments == []


# --- Rule 3: each open EvidenceGap affecting a hypothesis lowers it by 0.1 --------------


def test_each_gap_that_names_a_hypothesis_lowers_it_by_a_tenth():
    gaps = [
        EvidenceGap(tool="past_incidents", reason="partial", affects_hypotheses=["h1"]),
        EvidenceGap(tool="past_incidents", reason="timeout", affects_hypotheses=["h1"]),
    ]
    h1, h2 = hyp("h1", 0.5, [DEPLOY]), hyp("h2", 0.5, [DEPLOY], category="capacity")

    result = apply_evidence_rules(board(facts=[fact(DEPLOY)], hypotheses=[h1, h2], gaps=gaps))

    assert final(result, "h1") == 0.3
    assert final(result, "h2") == 0.5
    assert [(a.rule, a.subject, a.confidence_after) for a in result.adjustments] == [
        ("gap_penalty", "h1", 0.4),
        ("gap_penalty", "h1", 0.3),
    ]


def test_a_closed_gap_lowers_nothing_is_logged_and_leaves_the_report():
    gaps = [
        EvidenceGap(id="gap:1", tool="metrics", reason="timeout after 3 attempts", service="pricing-db"),
        EvidenceGap(id="gap:2", tool="logs", reason="tool backend error", affects_hypotheses=["h1"]),
    ]
    h1 = hyp("h1", 0.62, [DEPLOY])

    result = apply_evidence_rules(board(facts=[fact(DEPLOY)], hypotheses=[h1], gaps=gaps, closed_gaps=["gap:1"]))

    assert final(result, "h1") == 0.52, "only the open logs gap applies"
    assert [g.id for g in result.gaps] == ["gap:2"]
    closed = [a for a in result.adjustments if a.rule == "gap_closed"]
    assert [(a.subject, a.ids) for a in closed] == [("gap:1", ["metrics gap: timeout after 3 attempts"])]


def test_a_closed_gap_alone_leaves_a_well_supported_hypothesis_a_root_cause():
    gaps = [EvidenceGap(id="gap:1", tool="metrics", reason="timeout after 3 attempts")]
    h1 = hyp("h1", 0.62, [DEPLOY])

    review = apply_evidence_rules(board(facts=[fact(DEPLOY)], hypotheses=[h1], gaps=gaps, closed_gaps=["gap:1"]))

    assert final(review, "h1") == 0.62


def test_the_penalty_applies_after_the_cap_and_floors_at_zero():
    gaps = [EvidenceGap(tool="logs", reason="timeout", affects_hypotheses=["h1"]) for _ in range(6)]

    result = apply_evidence_rules(board(hypotheses=[hyp("h1", 0.9, [DEPLOY])], gaps=gaps))

    assert final(result, "h1") == 0.0


def test_a_gap_without_named_hypotheses_affects_those_its_tool_could_confirm_or_refute():
    # deploys covers deploy/config/flag/migration causes; logs and metrics cover every cause;
    # dependencies covers causes outside the service; past_incidents is history, not evidence.
    gaps = [
        EvidenceGap(tool="deploys", reason="timeout"),
        EvidenceGap(tool="metrics", reason="timeout"),
        EvidenceGap(tool="past_incidents", reason="partial"),
    ]
    deploy = hyp("h1", 0.8, [DEPLOY])
    dep = hyp("h2", 0.8, [DEPLOY], category="dependency_failure")

    result = apply_evidence_rules(board(facts=[fact(DEPLOY)], hypotheses=[deploy, dep], gaps=gaps))

    assert final(result, "h1") == 0.6
    assert final(result, "h2") == 0.7
    assert [g.affects_hypotheses for g in result.gaps] == [["h1"], ["h1", "h2"], []]


def test_a_gap_naming_an_unknown_hypothesis_affects_nothing():
    gaps = [EvidenceGap(tool="logs", reason="timeout", affects_hypotheses=["h9"])]

    result = apply_evidence_rules(board(facts=[fact(DEPLOY)], hypotheses=[hyp("h1", 0.8, [DEPLOY])], gaps=gaps))

    assert final(result, "h1") == 0.8
    assert result.gaps[0].affects_hypotheses == []


# --- Rules 4 and 5: the report -----------------------------------------------------------


def report_of(b: Blackboard, actions: list[RecommendedAction] = ()):  # type: ignore[assignment]
    return build_report(b, actions=list(actions), metrics=ReportMetrics(), terminated_by="done")


def test_a_hypothesis_at_exactly_0_6_gives_a_root_cause():
    b = board(facts=[fact(DEPLOY)], hypotheses=[hyp("h1", 0.6, [DEPLOY]), hyp("h2", 0.2, [LOG], category="capacity")])

    assert report_of(b).outcome == "ROOT_CAUSE"


def test_when_no_hypothesis_reaches_0_6_the_outcome_is_inconclusive_with_hypotheses_and_gaps():
    gap = EvidenceGap(tool="logs", reason="logs dropped")
    h1, h2 = hyp("h1", 0.65, [DEPLOY]), hyp("h2", 0.4, [LOG], category="capacity")
    b = board(facts=[fact(DEPLOY), fact(LOG)], hypotheses=[h2, h1], gaps=[gap])

    report = report_of(b)

    assert report.outcome == "INCONCLUSIVE"
    assert [(h.id, h.final_confidence) for h in report.ranked_hypotheses] == [("h1", 0.55), ("h2", 0.3)]
    assert [(g.tool, g.affects_hypotheses) for g in report.gaps] == [("logs", ["h2", "h1"])]


def test_the_report_lists_kept_facts_and_assumptions_separately_and_logs_every_rule_change():
    guess = Assumption(statement="maybe CPU", basis="past incident", how_to_verify="check cpu")
    b = board(facts=[fact(DEPLOY), fact(GHOST, statement="ghost")], assumptions=[guess],
              hypotheses=[hyp("h1", 0.9, [GHOST])])  # fmt: skip

    report = report_of(b)

    assert report.facts == [fact(DEPLOY)]
    assert [a.statement for a in report.assumptions] == ["ghost", "maybe CPU"]
    assert [a.rule for a in report.rule_adjustments] == ["fact_demoted", "hypothesis_ids_dropped", "unsupported_cap"]


def test_every_recommended_action_points_at_a_reported_hypothesis():
    b = board(facts=[fact(DEPLOY)], hypotheses=[hyp("h1", 0.8, [DEPLOY])])
    actions = [
        RecommendedAction(action="Roll back", rationale="r", hypothesis_id="h1"),
        RecommendedAction(action="Restart the world", rationale="r", hypothesis_id="h7"),
    ]

    report = report_of(b, actions)

    assert [a.action for a in report.recommended_actions] == ["Roll back"]
    [log] = report.rule_adjustments
    assert (log.rule, log.subject) == ("action_dropped", "action[1]")
    assert "h7" in log.detail
