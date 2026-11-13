"""The eval runner through its public function and CLI, with the mock LLM (ticket 09, L25, step 10)."""

from pathlib import Path
from typing import Any

import pytest

from incident_investigator.agents.prompts import role_of
from incident_investigator.dataset import list_incident_ids
from incident_investigator.evaluation import runner
from incident_investigator.evaluation.runner import EvalResult, main, run_eval
from incident_investigator.investigate import investigate
from incident_investigator.llm.base import LLMAuthError, LLMRateLimitError, LLMResponse
from incident_investigator.llm.mock import MockLLM

IDS = list_incident_ids()


@pytest.fixture(autouse=True)
def no_tool_backoff(monkeypatch: pytest.MonkeyPatch) -> None:
    """Skip the real retry backoff of timed-out tool calls (speed only)."""

    def fast(*args: Any, **kwargs: Any) -> Any:
        return investigate(*args, **{**kwargs, "tool_backoff_s": 0})

    monkeypatch.setattr(runner, "investigate", fast)


class FailingOnRole:
    """MockLLM that raises `error` on the `nth` call (1-based) of `role` and every one after it."""

    def __init__(self, role: str, error: BaseException, nth: int = 1) -> None:
        self.inner = MockLLM(delay_s=0.0)
        self.role, self.error, self.nth = role, error, nth
        self.seen = 0

    def preflight(self) -> None:
        return None

    def complete(self, *, system: str, prompt: str, model: str) -> LLMResponse:
        if role_of(system) == self.role:
            self.seen += 1
            if self.seen >= self.nth:
                raise self.error
        return self.inner.complete(system=system, prompt=prompt, model=model)


class NeverCalled:
    def preflight(self) -> None:
        return None

    def complete(self, *, system: str, prompt: str, model: str) -> LLMResponse:
        raise AssertionError("a pair that already has a result must not be re-run")


def result_files(run_dir: Path) -> list[str]:
    return sorted(p.name for p in run_dir.glob("*.json") if not p.name.endswith(".trace.json"))


def test_eval_all_with_the_mock_writes_twenty_results_and_a_summary(tmp_path: Path):
    exit_code = main(["--all", "--llm", "mock", "--results-dir", str(tmp_path), "--run", "r1"])

    run_dir = tmp_path / "r1"
    assert exit_code == 0
    assert result_files(run_dir) == sorted(f"{i}__{s}.json" for i in IDS for s in ("multi", "single"))
    for name in result_files(run_dir):
        result = EvalResult.model_validate_json((run_dir / name).read_text())
        assert f"{result.incident_id}__{result.system}.json" == name
        assert result.run == "r1"
        assert result.llm == "mock"
        assert result.judge is not None and result.judge.status == "ok"  # the judge runs by default (ticket 10)
        assert result.report.incident_id == result.incident_id
        assert result.scores.citations > 0
    summary = (run_dir / "summary.md").read_text()
    for heading in ["Accuracy", "Grounding", "Tool calls", "LLM calls", "Tokens in", "Tokens out", "Cache tokens",
                    "Latency", "Judge", "once per incident"]:  # fmt: skip
        assert heading in summary, heading
    assert "20 of 20" in summary
    assert not list(run_dir.glob("*.tmp"))


def test_rerunning_skips_pairs_whose_result_exists(tmp_path: Path):
    first = run_eval(MockLLM(delay_s=0.0), tmp_path, incident_ids=IDS[:2], llm_label="mock")
    before = {p: p.stat().st_mtime_ns for p in first.written}

    second = run_eval(NeverCalled(), tmp_path, incident_ids=IDS[:2], llm_label="mock")

    assert len(first.written) == 4
    assert second.written == []
    assert sorted(second.skipped) == sorted(first.written)
    assert {p: p.stat().st_mtime_ns for p in first.written} == before
    assert (tmp_path / "summary.md").is_file()


def test_rerunning_after_a_partial_run_completes_only_the_missing_pairs(tmp_path: Path):
    run_eval(MockLLM(delay_s=0.0), tmp_path, incident_ids=IDS[:1], systems=["multi"], llm_label="mock")

    resumed = run_eval(MockLLM(delay_s=0.0), tmp_path, incident_ids=IDS[:1], llm_label="mock")

    assert [p.name for p in resumed.skipped] == [f"{IDS[0]}__multi.json"]
    assert [p.name for p in resumed.written] == [f"{IDS[0]}__single.json"]


def test_a_pair_cut_off_midway_leaves_no_file_and_keeps_the_completed_ones(tmp_path: Path):
    killed = FailingOnRole("baseline", KeyboardInterrupt(), nth=3)

    with pytest.raises(KeyboardInterrupt):
        run_eval(killed, tmp_path, incident_ids=IDS[:2], llm_label="mock")

    assert result_files(tmp_path) == [f"{IDS[0]}__multi.json"]
    assert not list(tmp_path.glob("*.tmp"))


def test_an_investigation_that_raises_leaves_no_file_for_that_pair(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    real = runner.investigate

    def crash_on_second_single(incident_id: str, llm: Any, **kwargs: Any) -> Any:
        if incident_id == IDS[1] and kwargs.get("mode") == "single":
            raise RuntimeError("crashed midway")
        return real(incident_id, llm, **kwargs)

    monkeypatch.setattr(runner, "investigate", crash_on_second_single)

    with pytest.raises(RuntimeError, match="crashed midway"):
        run_eval(MockLLM(delay_s=0.0), tmp_path, incident_ids=IDS[:3], llm_label="mock")

    assert result_files(tmp_path) == [f"{IDS[0]}__multi.json", f"{IDS[0]}__single.json", f"{IDS[1]}__multi.json"]


def test_a_rate_limit_stops_cleanly_and_keeps_the_completed_pairs(tmp_path: Path):
    capped = FailingOnRole("baseline", LLMRateLimitError("You've hit your limit"))

    run = run_eval(capped, tmp_path, incident_ids=IDS[:2], llm_label="mock")

    assert run.rate_limited is not None and "hit your limit" in run.rate_limited
    assert [p.name for p in run.written] == [f"{IDS[0]}__multi.json"]
    assert result_files(tmp_path) == [f"{IDS[0]}__multi.json"]
    assert not list(tmp_path.glob("*.tmp"))
    assert "1 of 4" in (tmp_path / "summary.md").read_text()


def test_the_cli_prints_the_exact_resume_command_and_exits_non_zero_on_a_rate_limit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
):
    monkeypatch.setattr(runner, "build_llm", lambda name: FailingOnRole("baseline", LLMRateLimitError("429")))
    argv = ["--incidents", f"{IDS[0]},{IDS[1]}", "--llm", "mock", "--results-dir", str(tmp_path), "--run", "r2"]

    exit_code = main(argv)

    out = capsys.readouterr()
    assert exit_code == 4
    resume = f"uv run eval --incidents {IDS[0]},{IDS[1]} --llm mock --results-dir {tmp_path} --run r2"
    assert resume in out.out + out.err
    assert result_files(tmp_path / "r2") == [f"{IDS[0]}__multi.json"]


def test_the_resume_command_names_the_default_run_so_rerunning_it_resumes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
):
    monkeypatch.setattr(runner, "build_llm", lambda name: FailingOnRole("baseline", LLMRateLimitError("usage limit")))
    monkeypatch.chdir(tmp_path)

    exit_code = main(["--all", "--systems", "single"])

    out = capsys.readouterr()
    assert exit_code == 4
    assert "uv run eval --all --systems single --run live" in out.out + out.err
    assert (tmp_path / "eval" / "results" / "live" / "summary.md").is_file()


def test_an_unknown_incident_exits_2_before_any_run(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(runner, "build_llm", lambda name: NeverCalled())

    assert main(["--incidents", "inc99", "--results-dir", str(tmp_path)]) == 2
    assert not any(tmp_path.rglob("*.json"))


def test_a_rejected_api_key_stops_the_eval_without_writing_fake_results(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
):
    bad_key = FailingOnRole("baseline", LLMAuthError("the API rejected ANTHROPIC_API_KEY (401)"))
    monkeypatch.setattr(runner, "build_llm", lambda name: bad_key)
    argv = ["--incidents", f"{IDS[0]},{IDS[1]}", "--llm", "mock", "--results-dir", str(tmp_path), "--run", "r3"]

    exit_code = main(argv)

    out = capsys.readouterr()
    assert exit_code == 3
    assert "ANTHROPIC_API_KEY" in out.err
    resume = f"uv run eval --incidents {IDS[0]},{IDS[1]} --llm mock --results-dir {tmp_path} --run r3"
    assert resume in out.out + out.err
    # The multi run before the baseline finished; nothing is written for the rejected pair.
    assert result_files(tmp_path / "r3") == [f"{IDS[0]}__multi.json"]
