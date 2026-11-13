# 05: Evidence rules and an INCONCLUSIVE outcome

**What to build:** The report can be trusted. Deterministic code (not the LLM) checks that every Fact cites evidence IDs that really exist, keeps Assumptions apart with their basis and how they could be checked, and adjusts each Hypothesis's confidence. When nothing is strong enough, the system says so with an INCONCLUSIVE outcome instead of guessing. This post-processing is shared, so the baseline (ticket 07) uses exactly the same rules.

**Blocked by:** 04 (planner and parallel investigation)

**Status:** done

Context: PRD step 7, data model (Hypothesis, InvestigationReport). `DECISIONS.md` Q16, Q21, glossary.

- [x] A Fact that cites a non-existent evidence ID is not kept as a Fact. It is moved to Assumptions or dropped, and the change is logged
- [x] A Hypothesis with no supporting Fact has its confidence capped at 0.5
- [x] Each open EvidenceGap that affects a Hypothesis lowers its confidence by 0.1
- [x] If no Hypothesis reaches 0.6 final confidence, the outcome is `INCONCLUSIVE`, and the report still lists the ranked Hypotheses and the gaps
- [x] The report lists Facts and Assumptions separately, and each recommended action is linked to a Hypothesis
- [x] The rules are a reusable step that both investigation modes can call
- [x] Unit tests cover each rule with a hand-built blackboard (no LLM)

## Comments

**2026-11-12, implementation:**
- **Code:** `evidence_rules.py` has `apply_evidence_rules(board) -> EvidenceReview{facts, assumptions, hypotheses (cleaned, final_confidence set, ranked), gaps (affects_hypotheses resolved), adjustments}`, `link_actions(actions, hypotheses)`, and `build_report(board, *, actions, metrics, terminated_by, review=None) -> InvestigationReport`. `CONFIDENCE_THRESHOLD` (0.6) moved here from `graph.py`. `schemas.py` gained `RuleAdjustment{rule, subject, detail, ids, confidence_before, confidence_after}` and `InvestigationReport.rule_adjustments` (last field, default `[]`). Rules: `fact_demoted`, `hypothesis_ids_dropped`, `unsupported_cap`, `gap_penalty`, `action_dropped`. Subjects: `<task_id>/fact[<n>]`, the hypothesis id, `action[<n>]`.
- **Definitions:**
  - An evidence ID **exists** when a tool returned it in this run (`board.evidence`). The rules never read the Scenario. A dataset ID that no tool returned does not count.
  - A Fact citing **any** unknown ID moves to Assumptions (never dropped). The basis names the invalid and the valid IDs, and `how_to_verify` says to re-run the query. Unknown IDs are dropped from hypothesis supporting/contradicting lists. Both are logged.
  - A **supporting Fact** is a kept Fact that cites at least one of the hypothesis's (cleaned) `supporting_ids`. Without one, the confidence is capped at 0.5. A demoted Fact does not support anything.
  - Every gap on the blackboard is **open**. Nothing closes a gap yet.
  - A gap **affects** the hypotheses named in `affects_hypotheses` (unknown names ignored). If the list is empty, the gap affects every hypothesis whose category its tool could confirm or refute (`TOOL_COVERS`): logs and metrics cover every category; deploys covers bad_deploy, config_change, feature_flag and db_migration; dependencies covers dependency_failure, retry_storm, third_party_limit and cert_dns; past_incidents covers none (history is not evidence about this incident). The service is not used: the gap does not record the query args. The report's gaps carry the resolved `affects_hypotheses`.
  - Order: final = proposed, then the 0.5 cap, then minus 0.1 per affecting gap, floor 0, rounded to 2 decimals. ROOT_CAUSE when some final >= 0.6, else INCONCLUSIVE.
  - An action whose `hypothesis_id` is not a reported hypothesis is dropped and logged. The Reporting LLM already gets one retry for unknown IDs (ticket 04), so this is a safety net for the baseline too.
- **Idempotent:** the rules read only the agents' raw entries (`proposed_confidence`, cited IDs). The graph's `evidence_rules` node writes only `final_confidence` back onto `board.hypotheses` (for the Critic, ticket 06). The cited IDs stay as the agents wrote them, so the trace shows the raw proposal and the report shows the cleaned one. `reporting` calls `apply_evidence_rules` again and passes the result to `build_report`. Re-running after a Critic loop gives a full adjustment log again.
- **For 07:** fill a `Blackboard` (findings, evidence = every row the tools returned, hypotheses, gaps), then `review = apply_evidence_rules(board)`, give `review.hypotheses` to the Reporting call, and `build_report(board, review=review, actions=..., metrics=..., terminated_by=...)`.
- **For 06:** `build_report` takes `terminated_by`. A `failures` report field can be set by passing it through `build_report` or with `report.model_copy(update=...)`. `graph.py` changes are limited to the `evidence_rules` and `reporting` node bodies and the imports.
- **Mock runs:** inc01 stays ROOT_CAUSE (h1 0.7: the past_incidents gap affects nothing). inc09 is ROOT_CAUSE at exactly 0.6 (h1 0.7 minus the logs gap), so the mock does not give INCONCLUSIVE there. The end-to-end INCONCLUSIVE test uses the scripted RoleLLM on inc01 (the metrics timeout takes 0.65 to 0.55).
- **Tests:** `tests/test_evidence_rules.py` (17 tests, hand-built blackboards) and one end-to-end test in `tests/test_investigation.py`. 241 tests pass, pyright is clean.
