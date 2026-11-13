import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import anthropic
import pytest

from incident_investigator import cli, models_config
from incident_investigator.blackboard import Blackboard
from incident_investigator.cli import main
from incident_investigator.dataset import load_scenario
from incident_investigator.investigate import investigate
from incident_investigator.schemas import InvestigationReport


@pytest.fixture(autouse=True)
def no_tool_backoff(monkeypatch: pytest.MonkeyPatch) -> None:
    """In-process CLI runs skip the real retry backoff of timed-out tool calls (speed only)."""

    def fast(*args: Any, **kwargs: Any) -> Any:
        return investigate(*args, **kwargs, tool_backoff_s=0)

    monkeypatch.setattr(cli, "investigate", fast)


def test_investigate_inc01_with_mock_llm_writes_a_valid_report(tmp_path: Path):
    exit_code = main(["inc01", "--llm", "mock", "--out", str(tmp_path)])

    assert exit_code == 0
    report = InvestigationReport.model_validate_json((tmp_path / "inc01__multi.json").read_text())
    assert report.incident_id == "inc01"
    assert report.terminated_by == "done"
    # Mock plan: observability (logs, metrics, deploys) || dependency (dependencies,
    # past_incidents) -> reasoning; then the Critic asks for one observability follow-up
    # and a second reasoning pass, then is satisfied. LLM calls: 1 plan + 4 + 3 +
    # 1 reasoning + 1 critic + 4 follow-up + 1 reasoning + 1 critic + 1 reporting.
    assert report.metrics.tool_calls == 5 + 3
    assert report.metrics.llm_calls == 17
    assert len(report.ranked_hypotheses) >= 2
    assert report.recommended_actions


def test_the_full_blackboard_trace_is_saved_next_to_the_report(tmp_path: Path):
    main(["inc01", "--llm", "mock", "--out", str(tmp_path)])

    board = Blackboard.model_validate_json((tmp_path / "inc01__multi.trace.json").read_text())
    assert [(t.id, t.agent, t.status) for t in board.tasks] == [
        ("t1", "observability", "done"),
        ("t2", "dependency", "done"),
        ("t3", "reasoning", "done"),
        # The mock Critic asks for one follow-up, then is satisfied.
        ("c1.1", "observability", "done"),
        ("c1.r", "reasoning", "done"),
    ]
    assert [(t.origin, t.loop) for t in board.tasks[3:]] == [("critic", 1)] * 2
    assert board.critic_loops == 1 and len(board.critic_reviews) == 2
    spans = {t.task_id: (t.started_at, t.ended_at) for t in board.trace}
    assert spans["t1"][0] < spans["t2"][1] and spans["t2"][0] < spans["t1"][1], "t1 and t2 overlap"
    assert spans["t3"][0] >= max(spans["t1"][1], spans["t2"][1])
    assert {f.agent for f in board.findings} >= {"observability", "dependency"}
    assert board.report is not None and board.report.incident_id == "inc01"


def test_report_only_cites_evidence_that_exists_in_the_scenario(tmp_path: Path):
    main(["inc01", "--llm", "mock", "--out", str(tmp_path)])
    report = InvestigationReport.model_validate_json((tmp_path / "inc01__multi.json").read_text())

    cited = {i for f in report.facts for i in f.evidence_ids}
    cited |= {i for h in report.ranked_hypotheses for i in h.supporting_ids + h.contradicting_ids}
    assert cited, "the mock run should cite the log evidence it was given"
    assert cited <= load_scenario("inc01").evidence_ids()


def test_unknown_incident_exits_non_zero_with_a_clear_message_and_no_report(tmp_path: Path, capsys):
    exit_code = main(["inc99", "--llm", "mock", "--out", str(tmp_path)])

    assert exit_code != 0
    assert "unknown incident 'inc99'" in capsys.readouterr().err
    assert list(tmp_path.iterdir()) == []


def test_investigate_console_script_runs(tmp_path: Path):
    script = Path(sys.executable).parent / "investigate"

    done = subprocess.run(
        [str(script), "inc01", "--llm", "mock", "--out", str(tmp_path)],
        capture_output=True,
        text=True,
        timeout=60,
    )

    assert done.returncode == 0, done.stderr
    assert (tmp_path / "inc01__multi.json").is_file()


class GarbageMessages:
    """A fake anthropic `messages` endpoint whose model never replies with a valid action."""

    def __init__(self) -> None:
        self.models: list[str] = []

    def create(self, **kwargs: Any) -> SimpleNamespace:
        self.models.append(kwargs["model"])
        return SimpleNamespace(
            content=[SimpleNamespace(type="text", text="Sorry, I cannot help with that.")],
            usage=SimpleNamespace(input_tokens=7, output_tokens=3, cache_creation_input_tokens=0, cache_read_input_tokens=50),
            stop_reason="end_turn",
        )


def test_an_agent_that_replies_garbage_twice_still_gets_a_report(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test")
    messages = GarbageMessages()
    monkeypatch.setattr(anthropic, "Anthropic", lambda **kwargs: SimpleNamespace(messages=messages))

    exit_code = main(["inc01", "--llm", "anthropic_api", "--out", str(tmp_path)])

    assert exit_code == 0
    report = InvestigationReport.model_validate_json((tmp_path / "inc01__multi.json").read_text())
    assert report.outcome == "INCONCLUSIVE"
    assert report.ranked_hypotheses == []
    # Planner x2 (then the default plan), observability x2 || dependency x2, reasoning x2.
    # No hypotheses, so the Reporting agent has nothing to recommend and is not called.
    assert report.metrics.llm_calls == 8
    assert report.metrics.tool_calls == 0
    assert report.metrics.tokens_in == 8 * 7
    assert report.metrics.tokens_out == 8 * 3
    assert report.metrics.cache_tokens == 8 * 50
    opus, haiku = "claude-opus-5-5", "claude-haiku-5-5"
    assert messages.models[:2] == [opus, opus]
    assert sorted(messages.models[2:6]) == [haiku] * 4
    assert messages.models[6:] == [opus, opus]


# --- Backend choice: the models config by default, --llm as a global override (ticket 12) ---


def _models_toml(tmp_path: Path, monkeypatch, body: str) -> None:
    """Point every load_models() call (CLI and graph) at a temporary models.toml."""
    path = tmp_path / "models.toml"
    path.write_text(body)
    monkeypatch.setattr(models_config, "DEFAULT_PATH", path)


ALL_ROLES = ["planner", "reasoning", "critic", "baseline", "judge", "observability", "dependency", "reporting"]


def test_without_llm_the_backend_comes_from_the_models_config(tmp_path: Path, monkeypatch):
    roles = "\n".join(f'{r} = {{ backend = "mock", model = "claude-haiku-5-5" }}' for r in ALL_ROLES)
    _models_toml(tmp_path, monkeypatch, f"[roles]\n{roles}\n")
    out = tmp_path / "out"

    exit_code = main(["inc01", "--out", str(out)])

    assert exit_code == 0
    report = InvestigationReport.model_validate_json((out / "inc01__multi.json").read_text())
    assert report.metrics.llm_calls == 17


def test_the_default_config_uses_the_anthropic_api_and_fails_fast_without_a_key(tmp_path: Path, capsys, monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    out = tmp_path / "out"

    exit_code = main(["inc01", "--out", str(out)])

    assert exit_code == 3
    assert "ANTHROPIC_API_KEY is not set" in capsys.readouterr().err
    assert not out.exists()


def test_the_subscription_cli_backend_is_no_longer_a_choice(tmp_path: Path, capsys):
    with pytest.raises(SystemExit) as info:
        main(["inc01", "--llm", "claude_cli", "--out", str(tmp_path / "out")])

    assert info.value.code == 2
    assert "invalid choice: 'claude_cli'" in capsys.readouterr().err


def test_an_unknown_backend_in_the_models_config_fails_at_startup(tmp_path: Path, capsys, monkeypatch):
    roles = "\n".join(f'{r} = {{ backend = "gpt-cloud", model = "x" }}' for r in ALL_ROLES)
    _models_toml(tmp_path, monkeypatch, f"[roles]\n{roles}\n")
    out = tmp_path / "out"

    exit_code = main(["inc01", "--out", str(out)])

    assert exit_code == 3
    assert "unknown backend 'gpt-cloud'" in capsys.readouterr().err
    assert not out.exists()


def test_anthropic_api_without_a_key_fails_fast(tmp_path: Path, capsys, monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    out = tmp_path / "out"

    exit_code = main(["inc01", "--llm", "anthropic_api", "--out", str(out)])

    assert exit_code == 3
    assert "ANTHROPIC_API_KEY is not set" in capsys.readouterr().err
    assert not out.exists()


def test_openai_compat_with_no_server_fails_fast(tmp_path: Path, capsys, monkeypatch):
    # Port 9 (discard) on loopback: nothing listens, so the connection is refused at once.
    monkeypatch.setenv("OPENAI_COMPAT_BASE_URL", "http://127.0.0.1:9/v1")
    out = tmp_path / "out"

    exit_code = main(["inc01", "--llm", "openai_compat", "--out", str(out)])

    assert exit_code == 3
    err = capsys.readouterr().err
    assert "no OpenAI-compatible endpoint reachable at http://127.0.0.1:9/v1" in err
    assert not out.exists()


def test_a_rejected_api_key_exits_3_and_writes_no_report(tmp_path: Path, capsys, monkeypatch):
    import httpx2

    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-wrong")
    request = httpx2.Request("POST", "https://api.anthropic.com/v1/messages")

    class Rejecting:
        calls = 0

        def create(self, **kwargs: Any) -> Any:
            Rejecting.calls += 1
            raise anthropic.AuthenticationError(
                "Invalid bearer token", response=httpx2.Response(401, request=request), body={}
            )

    monkeypatch.setattr(anthropic, "Anthropic", lambda **kwargs: SimpleNamespace(messages=Rejecting()))
    out = tmp_path / "out"

    exit_code = main(["inc01", "--llm", "anthropic_api", "--out", str(out)])

    assert exit_code == 3
    assert "ANTHROPIC_API_KEY" in capsys.readouterr().err
    assert not out.exists()
    assert Rejecting.calls == 1, "the first rejected call stops the run"
