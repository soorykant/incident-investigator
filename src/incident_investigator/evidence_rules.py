"""The deterministic evidence rules between the investigation and the report (ticket 05,
PRD step 7, Q16, Q21). Code, never the LLM, decides what counts as a Fact and how far
each Hypothesis's confidence may go.

`apply_evidence_rules(board)` reads only the blackboard (never the Scenario), so the
single-agent baseline (ticket 07) calls it on its own blackboard. It is pure: it reads
the agents' raw entries (`proposed_confidence`, the cited IDs) and never its own output,
so running it twice gives the same result.

Definitions:
- An evidence ID **exists** when a tool returned that row during this run, i.e. it is
  in `board.evidence`. An ID that is in the dataset but was never returned does not count.
- A Fact that cites any ID that does not exist is not kept as a Fact. It becomes an
  Assumption whose basis names the invalid and valid IDs. Nothing is dropped silently.
- Hypothesis supporting/contradicting IDs that do not exist are dropped.
- A Hypothesis has a **supporting Fact** when at least one kept Fact (after the check
  above) cites at least one of its (cleaned) `supporting_ids`. Without one, its
  confidence is capped at 0.5 (`UNSUPPORTED_CAP`).
- An EvidenceGap is **open** unless its ID is in `board.closed_gaps`: a later query to
  the same tool for the same service returned `status=ok` (tools/registry.py). A
  closed gap lowers nothing, is logged as a `gap_closed` RuleAdjustment and is left
  out of the report's gaps.
- A gap **affects** a Hypothesis when the gap names it in `affects_hypotheses`. A gap
  that names none affects every Hypothesis whose category its tool could confirm or
  refute (`TOOL_COVERS`): logs and metrics cover every cause; deploys (deployments
  and config changes) covers change-driven causes; dependencies covers causes outside
  the service; past_incidents covers none, because history is not evidence about this
  incident. The report's gaps carry the resolved `affects_hypotheses`.
- Each affecting open gap lowers the confidence by 0.1 (`GAP_PENALTY`), after the cap,
  with a floor of 0. Confidences are rounded to 2 decimals.
- `final_confidence` = `proposed_confidence`, then the cap, then the gap penalties.
- The outcome is ROOT_CAUSE when some hypothesis's final confidence is at least 0.6
  (`CONFIDENCE_THRESHOLD`), else INCONCLUSIVE. Either way the report lists the ranked
  hypotheses and the gaps.
- A recommended action whose `hypothesis_id` is not a reported hypothesis is dropped.
- Every such change is a `RuleAdjustment` in `EvidenceReview.adjustments` and in the
  report's `rule_adjustments`.

Both modes finish the same way: fill a Blackboard, call `apply_evidence_rules(board)`
to get the ranked hypotheses (for the Reporting LLM), then
`build_report(board, review=..., actions=..., metrics=..., terminated_by=...)`.
"""

from __future__ import annotations

from typing import Literal, get_args

from pydantic import BaseModel, ConfigDict

from incident_investigator.blackboard import Blackboard
from incident_investigator.schemas import (
    Assumption,
    Category,
    EvidenceGap,
    Fact,
    Hypothesis,
    InvestigationReport,
    RecommendedAction,
    ReportMetrics,
    RuleAdjustment,
    ToolName,
)

TerminatedBy = Literal["done", "tool_budget", "token_budget", "timeout", "critic_limit"]

CONFIDENCE_THRESHOLD = 0.6
"""A hypothesis must reach this final confidence for a ROOT_CAUSE outcome (Q16)."""
UNSUPPORTED_CAP = 0.5
GAP_PENALTY = 0.1

_ALL: frozenset[Category] = frozenset(get_args(Category))
TOOL_COVERS: dict[ToolName, frozenset[Category]] = {
    "logs": _ALL,
    "metrics": _ALL,
    "deploys": frozenset({"bad_deploy", "config_change", "feature_flag", "db_migration"}),
    "dependencies": frozenset({"dependency_failure", "retry_storm", "third_party_limit", "cert_dns"}),
    "past_incidents": frozenset(),
}
"""Which hypothesis categories a tool's data could confirm or refute (used for gaps
that name no hypothesis)."""


class EvidenceReview(BaseModel):
    """The blackboard after the rules: what the report may present."""

    model_config = ConfigDict(extra="forbid")

    facts: list[Fact] = []
    assumptions: list[Assumption] = []
    hypotheses: list[Hypothesis] = []
    """Cleaned, `final_confidence` set, ranked highest first."""
    gaps: list[EvidenceGap] = []
    """Every open gap (closed ones are left out), with the hypotheses it affects resolved."""
    adjustments: list[RuleAdjustment] = []


def _review_facts(
    board: Blackboard, known: set[str], log: list[RuleAdjustment]
) -> tuple[list[Fact], list[Assumption]]:
    facts: list[Fact] = []
    assumptions: list[Assumption] = []
    for finding in board.findings:
        for n, fact in enumerate(finding.facts):
            invalid = [i for i in fact.evidence_ids if i not in known]
            if not invalid:
                facts.append(fact)
                continue
            valid = [i for i in fact.evidence_ids if i in known]
            basis = (
                f"Stated as a fact by {finding.agent} ({finding.task_id}), but cites evidence IDs "
                f"no tool returned in this run: {invalid}."
            )
            basis += f" Valid citations: {valid}." if valid else " No valid citations."
            assumptions.append(
                Assumption(
                    statement=fact.statement,
                    basis=basis,
                    how_to_verify="Re-run the tool query that should return these rows and check the "
                    "statement against the evidence IDs it actually returns.",
                )
            )
            log.append(
                RuleAdjustment(
                    rule="fact_demoted",
                    subject=f"{finding.task_id}/fact[{n}]",
                    detail=f"Moved to assumptions: cites unknown evidence IDs {invalid}",
                    ids=invalid,
                )
            )
        assumptions.extend(finding.assumptions)
    return facts, assumptions


def _clean_ids(h: Hypothesis, known: set[str], log: list[RuleAdjustment]) -> Hypothesis:
    dropped = sorted({i for i in h.supporting_ids + h.contradicting_ids if i not in known})
    if dropped:
        log.append(
            RuleAdjustment(
                rule="hypothesis_ids_dropped",
                subject=h.id,
                detail=f"Dropped evidence IDs no tool returned in this run: {dropped}",
                ids=dropped,
            )
        )
    return h.model_copy(
        update={
            "supporting_ids": [i for i in h.supporting_ids if i in known],
            "contradicting_ids": [i for i in h.contradicting_ids if i in known],
        }
    )


def _resolve_gap(gap: EvidenceGap, hypotheses: list[Hypothesis]) -> EvidenceGap:
    if gap.affects_hypotheses:
        named = set(gap.affects_hypotheses)
        affected = [h.id for h in hypotheses if h.id in named]
    else:
        affected = [h.id for h in hypotheses if h.category in TOOL_COVERS[gap.tool]]
    return gap.model_copy(update={"affects_hypotheses": affected})


def _gap_label(gap: EvidenceGap) -> str:
    where = f" in {gap.task_id}" if gap.task_id else ""
    return f"{gap.tool} gap{where}: {gap.reason}"


def _score(h: Hypothesis, facts: list[Fact], gaps: list[EvidenceGap], log: list[RuleAdjustment]) -> Hypothesis:
    confidence = h.proposed_confidence
    cited = {i for f in facts for i in f.evidence_ids}
    if not cited & set(h.supporting_ids) and confidence > UNSUPPORTED_CAP:
        log.append(
            RuleAdjustment(
                rule="unsupported_cap",
                subject=h.id,
                detail=f"No kept Fact cites any of its supporting IDs; capped at {UNSUPPORTED_CAP}",
                ids=list(h.supporting_ids),
                confidence_before=confidence,
                confidence_after=UNSUPPORTED_CAP,
            )
        )
        confidence = UNSUPPORTED_CAP
    for gap in gaps:
        if h.id not in gap.affects_hypotheses:
            continue
        lowered = round(max(0.0, confidence - GAP_PENALTY), 2)
        log.append(
            RuleAdjustment(
                rule="gap_penalty",
                subject=h.id,
                detail=f"Open evidence gap lowers confidence by {GAP_PENALTY} (floor 0)",
                ids=[_gap_label(gap)],
                confidence_before=confidence,
                confidence_after=lowered,
            )
        )
        confidence = lowered
    return h.model_copy(update={"final_confidence": confidence})


def apply_evidence_rules(board: Blackboard) -> EvidenceReview:
    """Apply every rule to the blackboard and return what the report may present."""
    known = {e.id for e in board.evidence}
    log: list[RuleAdjustment] = []
    facts, assumptions = _review_facts(board, known, log)
    hypotheses = [_clean_ids(h, known, log) for h in board.hypotheses]
    for gap in board.gaps:
        if gap not in board.open_gaps():
            log.append(
                RuleAdjustment(
                    rule="gap_closed",
                    subject=gap.id or "",
                    detail="A later successful query to the same tool for the same service closed this gap",
                    ids=[_gap_label(gap)],
                )
            )
    gaps = [_resolve_gap(g, hypotheses) for g in board.open_gaps()]
    hypotheses = [_score(h, facts, gaps, log) for h in hypotheses]
    return EvidenceReview(
        facts=facts,
        assumptions=assumptions,
        hypotheses=rank(hypotheses),
        gaps=gaps,
        adjustments=log,
    )


def link_actions(
    actions: list[RecommendedAction], hypotheses: list[Hypothesis]
) -> tuple[list[RecommendedAction], list[RuleAdjustment]]:
    """Keep the actions that point at a reported hypothesis; log the rest."""
    known = {h.id for h in hypotheses}
    kept: list[RecommendedAction] = []
    log: list[RuleAdjustment] = []
    for n, action in enumerate(actions):
        if action.hypothesis_id in known:
            kept.append(action)
            continue
        log.append(
            RuleAdjustment(
                rule="action_dropped",
                subject=f"action[{n}]",
                detail=f"Dropped {action.action!r}: it points at unknown hypothesis {action.hypothesis_id!r}",
            )
        )
    return kept, log


def build_report(
    board: Blackboard,
    *,
    actions: list[RecommendedAction],
    metrics: ReportMetrics,
    terminated_by: TerminatedBy,
    review: EvidenceReview | None = None,
) -> InvestigationReport:
    """The shared report assembly for both investigation modes.

    Applies the evidence rules (or uses `review`, the result of `apply_evidence_rules`
    on this same board, to avoid computing it twice), links every action to a reported
    hypothesis, and sets the outcome: ROOT_CAUSE when some hypothesis's final
    confidence reaches `CONFIDENCE_THRESHOLD`, else INCONCLUSIVE."""
    review = review or apply_evidence_rules(board)
    kept, dropped = link_actions(actions, review.hypotheses)
    top = max((h.final_confidence or 0.0 for h in review.hypotheses), default=0.0)
    return InvestigationReport(
        incident_id=board.incident_id,
        outcome="ROOT_CAUSE" if top >= CONFIDENCE_THRESHOLD else "INCONCLUSIVE",
        ranked_hypotheses=review.hypotheses,
        facts=review.facts,
        assumptions=review.assumptions,
        recommended_actions=kept,
        gaps=review.gaps,
        metrics=metrics,
        terminated_by=terminated_by,
        rule_adjustments=review.adjustments + dropped,
    )


def rank(hypotheses: list[Hypothesis]) -> list[Hypothesis]:
    """Highest final confidence first; ties keep the Reasoning agent's order."""
    return sorted(hypotheses, key=lambda h: h.final_confidence or 0.0, reverse=True)
