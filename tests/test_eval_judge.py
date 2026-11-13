"""The LLM judge through its public function, with scripted fake LLMs and the mock (ticket 10, Q17)."""

import json
from pathlib import Path
from typing import Any

import pytest

from incident_investigator.agents.prompts import role_of
from incident_investigator.dataset import list_incident_ids, load_answer_key, load_scenario
from incident_investigator.evaluation import runner
from incident_investigator.evaluation.judge import MAX_JUDGE_ATTEMPTS, judge_report
from incident_investigator.evaluation.runner import EvalResult, load_results, main, run_eval
from incident_investigator.investigate import investigate
from incident_investigator.llm.base import LLMError, LLMRateLimitError, LLMResponse, Usage
from incident_investigator.llm.mock import MockLLM
from incident_investigator.schemas import InvestigationReport

INCIDENT = list_incident_ids()[0]

VALID = {
    "evidence_support": {"score": 4, "rationale": "Most facts cite rows that say what the fact says."},
    "fact_assumption_separation": {"score": 3, "rationale": "One inference is stated as a fact."},
    "recommended_actions": {"score": 5, "rationale": "Rollback first, then verify."},
    "fell_for_trap": {"answer": "no", "rationale": "The misleading rows are cited as contradicting."},
}


class Scripted:
    """Replies with each text in turn (or raises it, if it is an exception) and records the prompts."""

    def __init__(self, *replies: str | BaseException) -> None:
        self.replies = list(replies)
        self.prompts: list[str] = []
        self.systems: list[str] = []

    def preflight(self) -> None:
        return None

    def complete(self, *, system: str, prompt: str, model: str) -> LLMResponse:
        self.systems.append(system)
        self.prompts.append(prompt)
        reply = self.replies.pop(0)
        if isinstance(reply, BaseException):
            raise reply
        return LLMResponse(text=reply, usage=Usage(tokens_in=100, tokens_out=20, cache_tokens=5), latency_s=0.5)


@pytest.fixture(scope="module")
def report() -> InvestigationReport:
    return investigate(INCIDENT, MockLLM(delay_s=0.0), mode="single", tool_backoff_s=0).report


def judge(llm: Any, report: InvestigationReport):  # noqa: ANN201
    return judge_report(
        llm, report=report, answer_key=load_answer_key(INCIDENT), scenario=load_scenario(INCIDENT), model="opus"
    )


def test_valid_output_gives_scores_and_records_the_judge_cost(report: InvestigationReport):
    llm = Scripted(json.dumps(VALID))

    result = judge(llm, report)

    assert result.status == "ok"
    assert result.verdict is not None
    assert result.verdict.evidence_support.score == 4
    assert result.verdict.fact_assumption_separation.score == 3
    assert result.verdict.recommended_actions.score == 5
    assert result.verdict.fell_for_trap.answer == "no"
    assert result.verdict.fell_for_trap.rationale
    assert (result.attempts, result.llm_calls) == (1, 1)
    assert (result.usage.tokens_in, result.usage.tokens_out, result.usage.cache_tokens) == (100, 20, 5)
    assert result.model == "opus"
    assert llm.systems[0].startswith("Role: judge\n")


def test_the_prompt_holds_the_answer_key_the_report_and_every_cited_row_resolved(report: InvestigationReport):
    llm = Scripted(json.dumps(VALID))
    key, scenario = load_answer_key(INCIDENT), load_scenario(INCIDENT)
    cited = {i for f in report.facts for i in f.evidence_ids} | {
        i for h in report.ranked_hypotheses for i in [*h.supporting_ids, *h.contradicting_ids]
    }
    rows = {r.id: r for r in [*scenario.logs, *scenario.metrics, *scenario.deploys, *scenario.config_changes]}

    judge(llm, report)

    prompt = llm.prompts[0]
    for trap in key.traps:
        assert trap.description in prompt
    assert all(i in prompt for i in cited)
    resolved = [i for i in cited if i in rows]
    assert resolved
    for i in resolved:
        row = rows[i]
        text = getattr(row, "message", None) or getattr(row, "description", None) or getattr(row, "name", None)
        if text:
            assert text in prompt
    assert report.ranked_hypotheses[0].summary in prompt


def test_an_unknown_cited_id_is_marked_as_not_in_the_scenario(report: InvestigationReport):
    fact = report.facts[0].model_copy(update={"evidence_ids": ["log:nowhere:9999"]})
    broken = report.model_copy(update={"facts": [fact, *report.facts[1:]]})
    llm = Scripted(json.dumps(VALID))

    judge(llm, broken)

    assert "log:nowhere:9999" in llm.prompts[0]
    assert "NOT IN SCENARIO" in llm.prompts[0]


def test_fenced_json_is_accepted(report: InvestigationReport):
    result = judge(Scripted(f"```json\n{json.dumps(VALID)}\n```"), report)

    assert result.status == "ok"


def test_invalid_then_valid_output_is_retried_and_the_retry_names_the_problem(report: InvestigationReport):
    out_of_range = {**VALID, "evidence_support": {"score": 9, "rationale": "x"}}
    llm = Scripted(json.dumps(out_of_range), json.dumps(VALID))

    result = judge(llm, report)

    assert result.status == "ok"
    assert (result.attempts, result.llm_calls) == (2, 2)
    assert result.usage.tokens_in == 200
    assert "evidence_support" in llm.prompts[1]
    assert "invalid" in llm.prompts[1].lower()


def test_invalid_output_every_time_is_recorded_as_a_judge_failure_without_raising(report: InvestigationReport):
    llm = Scripted(*["not json at all"] * MAX_JUDGE_ATTEMPTS)

    result = judge(llm, report)

    assert MAX_JUDGE_ATTEMPTS == 2
    assert result.status == "failed"
    assert result.verdict is None
    assert result.error
    assert result.attempts == MAX_JUDGE_ATTEMPTS
    assert llm.replies == []


def test_an_ordinary_llm_error_counts_as_a_failed_attempt(report: InvestigationReport):
    llm = Scripted(LLMError("timed out"), json.dumps(VALID))

    result = judge(llm, report)

    assert result.status == "ok"
    assert result.attempts == 2


def test_a_rate_limit_propagates(report: InvestigationReport):
    with pytest.raises(LLMRateLimitError):
        judge(Scripted(LLMRateLimitError("You've hit your limit")), report)


def test_the_mock_llm_plays_the_judge(report: InvestigationReport):
    result = judge(MockLLM(delay_s=0.0), report)

    assert result.status == "ok"
    assert result.verdict is not None
    assert result.usage.tokens_in > 0


# --- The judge pass in the eval runner: save as you go, resume, judge-only, summary -------------

IDS = list_incident_ids()


@pytest.fixture(autouse=True)
def no_tool_backoff(monkeypatch: pytest.MonkeyPatch) -> None:
    real = runner.investigate

    def fast(*args: Any, **kwargs: Any) -> Any:
        return real(*args, **{**kwargs, "tool_backoff_s": 0})

    monkeypatch.setattr(runner, "investigate", fast)


class JudgeAs:
    """MockLLM for every role; the judge role gets `judge_reply(n)` for its nth call (1-based),
    which may be a dict (sent as JSON), a raw string, an exception to raise, or None for the
    mock's own verdict. With `investigations=False`, any non-judge call fails the test."""

    def __init__(self, judge_reply: Any = None, *, investigations: bool = True) -> None:
        self.inner = MockLLM(delay_s=0.0)
        self.judge_reply = judge_reply
        self.investigations = investigations
        self.judge_calls = 0

    def preflight(self) -> None:
        return None

    def complete(self, *, system: str, prompt: str, model: str) -> LLMResponse:
        if role_of(system) != "judge":
            assert self.investigations, "this run must not re-investigate"
            return self.inner.complete(system=system, prompt=prompt, model=model)
        self.judge_calls += 1
        reply = self.judge_reply(self.judge_calls) if self.judge_reply is not None else None
        if reply is None:
            return self.inner.complete(system=system, prompt=prompt, model=model)
        if isinstance(reply, BaseException):
            raise reply
        text = reply if isinstance(reply, str) else json.dumps(reply)
        return LLMResponse(text=text, usage=Usage(tokens_in=1000, tokens_out=100), latency_s=1.0)


def results(run_dir: Path) -> dict[str, EvalResult]:
    return {f"{r.incident_id}__{r.system}": r for r in load_results(run_dir)}


def with_scores(es: int, fa: int, ra: int, trap: str) -> dict[str, Any]:
    return {
        "evidence_support": {"score": es, "rationale": "r"},
        "fact_assumption_separation": {"score": fa, "rationale": "r"},
        "recommended_actions": {"score": ra, "rationale": "r"},
        "fell_for_trap": {"answer": trap, "rationale": "r"},
    }


def section(text: str, start: str, end: str) -> str:
    return text.split(start, 1)[1].split(end, 1)[0]


def test_eval_all_with_the_mock_judges_every_pair_and_summarises_per_system(tmp_path: Path):
    exit_code = main(["--all", "--llm", "mock", "--results-dir", str(tmp_path), "--run", "r1"])

    assert exit_code == 0
    got = results(tmp_path / "r1")
    assert len(got) == 20
    assert all(r.judge is not None and r.judge.status == "ok" for r in got.values())
    summary = (tmp_path / "r1" / "summary.md").read_text()
    assert "pending (ticket 10)" not in summary
    for text in ["Evidence support", "Fact/assumption", "Actions", "Fell for trap", "Judge failures", "Judge cost",
                 "not charged to either system", "never decides accuracy"]:  # fmt: skip
        assert text in summary, text


def test_judge_scores_land_in_the_result_file_and_the_summary_per_system(tmp_path: Path):
    def reply(n: int) -> dict[str, Any]:
        return with_scores(5, 4, 2, "yes") if n % 2 else with_scores(1, 2, 4, "no")

    run_eval(JudgeAs(reply), tmp_path, incident_ids=IDS[:2], llm_label="mock")

    got = results(tmp_path)
    multi = got[f"{IDS[0]}__multi"].judge
    assert multi is not None and multi.verdict is not None
    assert multi.verdict.evidence_support.score == 5  # judged in pair order: multi first
    assert multi.verdict.fell_for_trap.answer == "yes"
    assert (multi.usage.tokens_in, multi.usage.tokens_out, multi.llm_calls) == (1000, 100, 1)
    judge_section = section((tmp_path / "summary.md").read_text(), "## Judge scores", "## How to read this")
    # multi got (5, 4, 2, yes) twice; single got (1, 2, 4, no) twice.
    assert "| multi | 2 of 2 | 5.00 | 4.00 | 2.00 | 2/2 | 0 |" in judge_section
    assert "| single | 2 of 2 | 1.00 | 2.00 | 4.00 | 0/2 | 0 |" in judge_section
    assert "| multi | 2 | 2,000 | 200 | 0 |" in judge_section


def test_judge_tokens_are_reported_separately_and_system_metrics_do_not_change(tmp_path: Path):
    run_eval(MockLLM(delay_s=0.0), tmp_path / "plain", incident_ids=IDS[:2], llm_label="mock", judge=False)
    run_eval(JudgeAs(lambda n: with_scores(3, 3, 3, "no")), tmp_path / "judged", incident_ids=IDS[:2],
             llm_label="mock")  # fmt: skip

    plain, judged = results(tmp_path / "plain"), results(tmp_path / "judged")
    assert all(r.judge is None for r in plain.values())
    for name, r in judged.items():
        assert r.judge is not None and r.judge.usage.tokens_in == 1000
        a, b = plain[name].report.metrics, r.report.metrics
        assert (a.tool_calls, a.llm_calls, a.tokens_in, a.tokens_out, a.cache_tokens) == (
            b.tool_calls, b.llm_calls, b.tokens_in, b.tokens_out, b.cache_tokens
        )  # fmt: skip

    def cost_columns(run: str) -> list[list[str]]:
        totals = section((tmp_path / run / "summary.md").read_text(), "## Totals", "## Per incident")
        rows = [line.split("|") for line in totals.splitlines() if line.startswith(("| multi", "| single"))]
        return [row[:7] for row in rows]  # everything but latency

    assert cost_columns("judged") == cost_columns("plain")


def test_changing_a_judge_score_does_not_change_the_accuracy_metric(tmp_path: Path):
    run_eval(JudgeAs(lambda n: with_scores(5, 5, 5, "no")), tmp_path / "high", incident_ids=IDS[:3],
             llm_label="mock")  # fmt: skip
    run_eval(JudgeAs(lambda n: with_scores(1, 1, 1, "yes")), tmp_path / "low", incident_ids=IDS[:3],
             llm_label="mock")  # fmt: skip

    high, low = results(tmp_path / "high"), results(tmp_path / "low")
    assert {k: r.judge.verdict for k, r in high.items() if r.judge} != {
        k: r.judge.verdict for k, r in low.items() if r.judge
    }
    assert {k: r.scores for k, r in high.items()} == {k: r.scores for k, r in low.items()}

    def accuracy_cells(run: str) -> list[str]:
        aggregate = section((tmp_path / run / "summary.md").read_text(), "## Aggregate", "## Totals")
        return [line.split("|")[3] for line in aggregate.splitlines() if line.startswith(("| multi", "| single"))]

    assert accuracy_cells("high") == accuracy_cells("low")


def test_rerunning_skips_pairs_already_judged(tmp_path: Path):
    run_eval(JudgeAs(), tmp_path, incident_ids=IDS[:2], llm_label="mock")
    before = {p.name: p.stat().st_mtime_ns for p in tmp_path.glob("*__*.json")}

    again = JudgeAs(lambda n: AssertionError("an already-judged pair must not be judged again"), investigations=False)
    run_eval(again, tmp_path, incident_ids=IDS[:2], llm_label="mock")

    assert again.judge_calls == 0
    assert {p.name: p.stat().st_mtime_ns for p in tmp_path.glob("*__*.json")} == before


def test_no_judge_skips_the_judge_and_judge_only_judges_an_existing_run_without_reinvestigating(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    argv = ["--incidents", IDS[0], "--llm", "mock", "--no-judge", "--results-dir", str(tmp_path), "--run", "r"]
    assert main(argv) == 0
    assert all(r.judge is None for r in results(tmp_path / "r").values())
    assert "pending" in (tmp_path / "r" / "summary.md").read_text()
    judge_only = JudgeAs(investigations=False)
    monkeypatch.setattr(runner, "build_llm", lambda name: judge_only)

    exit_code = main(["--judge-only", "--llm", "mock", "--results-dir", str(tmp_path), "--run", "r"])

    assert exit_code == 0
    assert judge_only.judge_calls == 2
    got = results(tmp_path / "r")
    assert len(got) == 2
    assert all(r.judge is not None and r.judge.status == "ok" for r in got.values())
    judge_section = section((tmp_path / "r" / "summary.md").read_text(), "## Judge scores", "## How to read this")
    assert "pending" not in judge_section


def test_judge_only_on_a_run_with_no_results_is_an_error(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(runner, "build_llm", lambda name: JudgeAs(investigations=False))

    assert main(["--judge-only", "--results-dir", str(tmp_path), "--run", "nothing"]) == 2


def test_a_judge_failure_is_recorded_and_counted_without_breaking_the_run(tmp_path: Path):
    run = run_eval(JudgeAs(lambda n: "garbage" if n <= 2 else None), tmp_path, incident_ids=IDS[:1], llm_label="mock")

    assert run.rate_limited is None
    got = results(tmp_path)
    failed = got[f"{IDS[0]}__multi"].judge
    assert failed is not None and failed.status == "failed" and failed.verdict is None and failed.attempts == 2
    ok = got[f"{IDS[0]}__single"].judge
    assert ok is not None and ok.status == "ok"
    judge_section = section((tmp_path / "summary.md").read_text(), "## Judge scores", "## How to read this")
    assert "| multi | 0 of 1 | n/a | n/a | n/a | n/a | 1 |" in judge_section


def test_a_judge_rate_limit_stops_cleanly_keeps_judged_pairs_and_prints_the_resume_command(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
):
    capped = JudgeAs(lambda n: LLMRateLimitError("You've hit your limit") if n >= 2 else None)
    monkeypatch.setattr(runner, "build_llm", lambda name: capped)

    exit_code = main(["--incidents", IDS[0], "--llm", "mock", "--results-dir", str(tmp_path), "--run", "r"])

    out = capsys.readouterr()
    assert exit_code == 4
    assert f"uv run eval --incidents {IDS[0]} --llm mock --results-dir {tmp_path} --run r" in out.out + out.err
    got = results(tmp_path / "r")
    assert got[f"{IDS[0]}__multi"].judge is not None
    assert got[f"{IDS[0]}__single"].judge is None
    assert not list((tmp_path / "r").glob("*.tmp"))


def test_a_judge_only_rate_limit_prints_a_judge_only_resume_command(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
):
    run_eval(MockLLM(delay_s=0.0), tmp_path / "r", incident_ids=IDS[:1], llm_label="mock", judge=False)
    capped = JudgeAs(lambda n: LLMRateLimitError("429"), investigations=False)
    monkeypatch.setattr(runner, "build_llm", lambda name: capped)

    exit_code = main(["--judge-only", "--results-dir", str(tmp_path), "--run", "r"])

    out = capsys.readouterr()
    assert exit_code == 4
    assert f"uv run eval --judge-only --results-dir {tmp_path} --run r" in out.out + out.err


def test_a_judge_call_cut_off_midway_saves_nothing_for_that_pair(tmp_path: Path):
    killed = JudgeAs(lambda n: KeyboardInterrupt() if n == 2 else None)

    with pytest.raises(KeyboardInterrupt):
        run_eval(killed, tmp_path, incident_ids=IDS[:1], llm_label="mock")

    got = results(tmp_path)
    assert got[f"{IDS[0]}__multi"].judge is not None
    assert got[f"{IDS[0]}__single"].judge is None
    assert not list(tmp_path.glob("*.tmp"))
