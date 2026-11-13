"""The single-agent baseline (ticket 07, Q6): one agent in a loop with the same tools,
limits, failure schedule, action protocol, evidence rules and report schema as the
multi-agent system. Driven through `investigate(mode="single")` and the CLI."""

from datetime import timedelta
from pathlib import Path
from typing import Any

import pytest
from test_critic_and_limits import LOGS, FakeClock, TokenHeavyLLM, always_calls_logs
from test_investigation import _ID, INCIDENT, SERVICE, WINDOW, RoleLLM

from incident_investigator import investigate as investigate_module
from incident_investigator.blackboard import Blackboard
from incident_investigator.cli import main
from incident_investigator.dataset import load_scenario
from incident_investigator.investigate import investigate
from incident_investigator.limits import DEFAULT_LIMITS, RunGuard, RunLimits
from incident_investigator.llm.mock import MockLLM
from incident_investigator.schemas import InvestigationReport

METRICS = {"tool": "metrics", "args": {"service": SERVICE, **WINDOW}}
BOGUS_ID = "log:inc01:does-not-exist"


def baseline_script(calls: list[dict[str, Any]], *, bogus_fact: bool = False) -> Any:
    """Call the tools in order, then finish with a fact and two competing hypotheses
    citing the first evidence ID seen."""

    def reply(prompt: str) -> Any:
        done = prompt.count("Tool result:")
        if done < len(calls):
            return {"action": "call_tool", **calls[done]}
        ids = _ID.findall(prompt)
        facts = [{"statement": "BASELINE-FACT", "evidence_ids": ids[:1]}]
        if bogus_fact:
            facts.append({"statement": "INVENTED", "evidence_ids": [BOGUS_ID]})
        return {
            "action": "finish",
            "facts": facts,
            "assumptions": [],
            "hypotheses": [
                {"id": "h1", "service": SERVICE, "category": "bad_deploy", "summary": "deploy",
                 "supporting_ids": ids[:1], "contradicting_ids": [], "proposed_confidence": 0.8},
                {"id": "h2", "service": SERVICE, "category": "capacity", "summary": "cpu",
                 "supporting_ids": ids[:1], "contradicting_ids": [], "proposed_confidence": 0.3},
            ],
        }  # fmt: skip

    return reply


# --- The offline mock and the CLI --------------------------------------------


def test_single_mode_with_the_mock_writes_a_schema_valid_report_on_inc01():
    run = investigate(INCIDENT, MockLLM(delay_s=0.0), mode="single", tool_backoff_s=0)

    report = InvestigationReport.model_validate_json(run.report.model_dump_json())
    assert report.incident_id == INCIDENT
    assert report.terminated_by == "done"
    assert len(report.ranked_hypotheses) >= 2
    assert report.facts and report.recommended_actions
    # Mock baseline: logs, metrics, deploys, dependencies, past_incidents, then finish
    # (6 LLM calls), then one call for the recommended actions.
    assert report.metrics.tool_calls == 5
    assert report.metrics.llm_calls == 7
    assert report.metrics.tokens_in > 0 and report.metrics.tokens_out > 0
    # The counters live in the same fields as multi mode, on the report and the blackboard.
    board = run.blackboard
    assert board.counters.tool_calls == report.metrics.tool_calls
    assert board.counters.llm_calls == report.metrics.llm_calls
    assert board.report == report
    # The same failure schedule: inc01's first past_incidents call is partial.
    assert [g.tool for g in report.gaps] == ["past_incidents"]
    # One agent, no plan, no Critic.
    assert board.tasks == [] and board.critic_reviews == []
    assert [(t.task_id, t.agent, t.tool_calls) for t in board.trace] == [("baseline", "baseline", 5)]
    assert {f.agent for f in board.findings} == {"baseline"}


def test_the_mock_baseline_finishes_done_on_every_incident():
    from incident_investigator.dataset import list_incident_ids

    for incident_id in list_incident_ids():
        run = investigate(incident_id, MockLLM(delay_s=0.0), mode="single", tool_backoff_s=0)
        assert run.report.terminated_by == "done", incident_id
        assert run.report.failures == [], incident_id
        assert len(run.report.ranked_hypotheses) >= 2, incident_id


def test_cli_single_mode_writes_the_report_and_the_blackboard_trace(tmp_path: Path):
    exit_code = main(["inc01", "--mode", "single", "--llm", "mock", "--out", str(tmp_path)])

    assert exit_code == 0
    report = InvestigationReport.model_validate_json((tmp_path / "inc01__single.json").read_text())
    assert report.incident_id == "inc01" and report.terminated_by == "done"
    board = Blackboard.model_validate_json((tmp_path / "inc01__single.trace.json").read_text())
    assert board.report == report
    cited = {i for f in report.facts for i in f.evidence_ids}
    assert cited and cited <= load_scenario("inc01").evidence_ids()


def test_an_unknown_mode_is_rejected():
    with pytest.raises(ValueError, match="mode"):
        investigate(INCIDENT, MockLLM(delay_s=0.0), mode="solo")  # type: ignore[arg-type]


# --- Same limits --------------------------------------------------------------


def test_both_modes_read_the_same_limit_config_object(monkeypatch: pytest.MonkeyPatch):
    seen: list[RunLimits] = []

    class SpyGuard(RunGuard):
        def __init__(self, limits: RunLimits = DEFAULT_LIMITS, **kwargs: Any) -> None:
            seen.append(limits)
            super().__init__(limits, **kwargs)

    monkeypatch.setattr(investigate_module, "RunGuard", SpyGuard)

    investigate(INCIDENT, MockLLM(delay_s=0.0), mode="multi", tool_backoff_s=0)
    investigate(INCIDENT, MockLLM(delay_s=0.0), mode="single", tool_backoff_s=0)

    multi, single = seen
    assert multi is single is DEFAULT_LIMITS
    assert multi.max_tool_calls_per_run == 30
    assert multi.max_tokens_per_run == 300_000
    assert multi.wall_clock_s == 600


@pytest.mark.parametrize("mode", ["multi", "single"])
def test_a_custom_tool_budget_binds_both_modes_the_same_way(mode: str):
    llm = RoleLLM(delay_s=0.0, observability=always_calls_logs, baseline=always_calls_logs)

    run = investigate(INCIDENT, llm, mode=mode, limits=RunLimits(max_tool_calls_per_run=4))  # type: ignore[arg-type]

    assert run.report.terminated_by == "tool_budget"
    assert run.report.metrics.tool_calls == 4


# --- Hitting a limit ----------------------------------------------------------


def test_the_baseline_stops_at_thirty_tool_calls_with_tool_budget_and_still_reports():
    llm = RoleLLM(delay_s=0.0, baseline=always_calls_logs)

    run = investigate(INCIDENT, llm, mode="single")

    report = run.report
    assert report.terminated_by == "tool_budget"
    assert report.metrics.tool_calls == 30, "the run budget binds, not the per-task cap of 6"
    assert run.blackboard.counters.tool_calls == 30
    assert report.outcome == "INCONCLUSIVE"
    # 30 calls, 1 refused (a finish-only turn), then 2 more tool calls are failed attempts.
    assert len(llm.prompts["baseline"]) == 33
    assert [f.agent for f in report.failures] == ["baseline"]


def test_a_baseline_that_finishes_after_the_budget_is_spent_keeps_its_hypotheses():
    def baseline(prompt: str) -> Any:
        if "tool budget is spent" in prompt:
            seen = _ID.findall(prompt)[:1]
            return {
                "action": "finish",
                "facts": [],
                "hypotheses": [
                    {"id": "h1", "service": SERVICE, "category": "bad_deploy", "summary": "a",
                     "supporting_ids": seen, "proposed_confidence": 0.4},
                    {"id": "h2", "service": SERVICE, "category": "capacity", "summary": "b",
                     "supporting_ids": seen, "proposed_confidence": 0.2},
                ],
            }  # fmt: skip
        return always_calls_logs(prompt)

    run = investigate(INCIDENT, RoleLLM(delay_s=0.0, baseline=baseline), mode="single")

    assert run.report.terminated_by == "tool_budget"
    assert [h.id for h in run.report.ranked_hypotheses] == ["h1", "h2"]
    assert run.report.failures == []


def test_the_baseline_stops_at_the_token_budget_with_token_budget_and_still_reports():
    llm = TokenHeavyLLM(tokens_in=99_000, baseline=always_calls_logs)

    run = investigate(INCIDENT, llm, mode="single")

    report = run.report
    assert report.terminated_by == "token_budget"
    assert report.metrics.tokens_in + report.metrics.tokens_out == 300_000
    assert len(llm.prompts["baseline"]) == 3
    assert llm.prompts["reporting"] == [], "no LLM call after the token budget is spent"


def test_the_baseline_stops_at_the_wall_clock_with_timeout_and_still_reports():
    clock = FakeClock()

    def slow(prompt: str) -> Any:
        clock.now += timedelta(minutes=11)
        return {"action": "call_tool", **LOGS}

    llm = RoleLLM(delay_s=0.0, baseline=slow)

    run = investigate(INCIDENT, llm, mode="single", clock=clock)

    assert run.report.terminated_by == "timeout"
    assert run.report.metrics.latency_s >= 600
    assert run.report.metrics.tool_calls == 0
    assert llm.prompts["reporting"] == []


def test_a_baseline_within_every_limit_ends_with_done_and_recommends_actions():
    llm = RoleLLM(delay_s=0.0, baseline=baseline_script([LOGS]))

    run = investigate(INCIDENT, llm, mode="single", clock=FakeClock())

    report = run.report
    assert report.terminated_by == "done"
    assert report.outcome == "ROOT_CAUSE"
    assert [h.id for h in report.ranked_hypotheses] == ["h1", "h2"]
    assert [a.hypothesis_id for a in report.recommended_actions] == ["h1"]
    assert report.metrics.latency_s == 0
    assert report.metrics.tool_calls == 1 and report.metrics.llm_calls == 2 + 1


def test_a_baseline_that_fails_twice_is_an_agent_failure_and_still_reports():
    run = investigate(INCIDENT, RoleLLM(delay_s=0.0, baseline=lambda _: "not json"), mode="single")

    assert [(f.agent, f.task_id, f.attempts) for f in run.report.failures] == [("baseline", "baseline", 2)]
    assert run.report.terminated_by == "done"
    assert run.report.outcome == "INCONCLUSIVE"


def test_the_baseline_must_give_competing_hypotheses_like_the_reasoning_agent():
    one = {"action": "finish", "hypotheses": [{"id": "h1", "service": SERVICE, "category": "bad_deploy",
           "summary": "s", "supporting_ids": ["x"], "proposed_confidence": 0.9}]}  # fmt: skip
    then = baseline_script([LOGS])

    def baseline(prompt: str) -> Any:
        return one if "Your last reply" not in prompt and "Tool result:" not in prompt else then(prompt)

    llm = RoleLLM(delay_s=0.0, baseline=baseline)
    run = investigate(INCIDENT, llm, mode="single")

    assert "at least 2" in llm.prompts["baseline"][1]
    assert len(run.report.ranked_hypotheses) == 2


# --- The same evidence-rule step ----------------------------------------------


def test_the_evidence_rules_demote_a_baseline_fact_citing_an_unknown_id():
    llm = RoleLLM(delay_s=0.0, baseline=baseline_script([LOGS], bogus_fact=True))

    report = investigate(INCIDENT, llm, mode="single").report

    assert [f.statement for f in report.facts] == ["BASELINE-FACT"]
    assert any(a.statement == "INVENTED" and BOGUS_ID in a.basis for a in report.assumptions)
    assert ("fact_demoted", "baseline/fact[1]") in [(a.rule, a.subject) for a in report.rule_adjustments]


def test_baseline_gaps_lower_confidence_through_the_same_rules():
    # inc01's 2nd metrics call times out: the gap affects every hypothesis (-0.1 each).
    llm = RoleLLM(delay_s=0.0, baseline=baseline_script([METRICS, METRICS]))

    report = investigate(INCIDENT, llm, mode="single", tool_backoff_s=0).report

    assert [(g.tool, g.task_id) for g in report.gaps] == [("metrics", "baseline")]
    assert [(h.id, h.final_confidence) for h in report.ranked_hypotheses] == [("h1", 0.7), ("h2", 0.2)]
