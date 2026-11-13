"""The HTTP API (ticket 11): FastAPI over the same `investigate()` the CLI uses."""

import json
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from incident_investigator.api import create_app
from incident_investigator.blackboard import Blackboard
from incident_investigator.cli import main as cli_main
from incident_investigator.dataset import list_incident_ids, load_scenario
from incident_investigator.llm.base import LLMResponse
from incident_investigator.llm.mock import MockLLM
from incident_investigator.schemas import InvestigationReport


def _mock() -> MockLLM:
    return MockLLM(delay_s=0.0)


@pytest.fixture
def client(tmp_path: Path) -> Iterator[TestClient]:
    app = create_app(llm_factory=_mock, results_dir=tmp_path, tool_backoff_s=0)
    with TestClient(app) as c:
        yield c


def _wait(client: TestClient, run_id: str, timeout_s: float = 20.0) -> dict[str, Any]:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        body = client.get(f"/investigations/{run_id}").json()
        if body["status"] in ("done", "failed"):
            return body
        time.sleep(0.02)
    raise AssertionError(f"investigation {run_id} did not finish in {timeout_s}s")


def test_post_returns_an_id_and_polling_reaches_done_with_report_and_trace(client: TestClient, tmp_path: Path):
    response = client.post("/investigations", json={"incident_id": "inc01", "mode": "multi"})

    assert response.status_code == 202
    run_id = response.json()["id"]
    assert client.get(f"/investigations/{run_id}").json()["status"] in ("pending", "running", "done")

    body = _wait(client, run_id)
    assert body["status"] == "done"
    assert body["error"] is None
    report = InvestigationReport.model_validate(body["report"])
    assert report.incident_id == "inc01" and report.terminated_by == "done"
    board = Blackboard.model_validate(body["blackboard"])
    assert {"t1", "t2", "t3"} <= {t.task_id for t in board.trace}
    assert board.report == report
    # Kept in memory plus one JSON file per investigation.
    saved = json.loads((tmp_path / f"{run_id}.json").read_text())
    assert saved["status"] == "done" and saved["report"] == body["report"]


def test_single_mode_runs_the_baseline(client: TestClient):
    run_id = client.post("/investigations", json={"incident_id": "inc01", "mode": "single"}).json()["id"]

    body = _wait(client, run_id)
    assert body["status"] == "done" and body["mode"] == "single"
    assert [t.task_id for t in Blackboard.model_validate(body["blackboard"]).trace] == ["baseline"]


def test_the_api_and_the_cli_produce_the_same_report_shape(client: TestClient, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    from incident_investigator import cli
    from incident_investigator.investigate import investigate

    monkeypatch.setattr(cli, "investigate", lambda *a, **k: investigate(*a, **k, tool_backoff_s=0))
    out = tmp_path / "cli"
    assert cli_main(["inc01", "--llm", "mock", "--out", str(out)]) == 0
    from_cli = json.loads((out / "inc01__multi.json").read_text())

    from_api = _wait(client, client.post("/investigations", json={"incident_id": "inc01"}).json()["id"])["report"]

    def timeless(report: dict[str, Any]) -> dict[str, Any]:
        return {**report, "metrics": {**report["metrics"], "latency_s": 0}}

    assert timeless(from_api) == timeless(from_cli)


def test_get_incidents_lists_the_ten_incidents_without_answer_keys(client: TestClient):
    response = client.get("/incidents")

    assert response.status_code == 200
    incidents = response.json()
    assert [i["id"] for i in incidents] == list_incident_ids()
    assert len(incidents) == 10
    first = load_scenario("inc01").report
    assert incidents[0] == {
        "id": "inc01",
        "title": first.title,
        "affected_service": first.affected_service,
        "severity": first.severity,
    }


def test_swagger_renders_at_docs(client: TestClient):
    assert client.get("/docs").status_code == 200
    paths = client.get("/openapi.json").json()["paths"]
    assert {"/investigations", "/investigations/{investigation_id}", "/incidents"} <= set(paths)


def test_unknown_incident_is_404_and_starts_no_run(client: TestClient, tmp_path: Path):
    response = client.post("/investigations", json={"incident_id": "inc99"})

    assert response.status_code == 404
    assert "inc99" in response.json()["detail"]
    assert list(tmp_path.iterdir()) == []


@pytest.mark.parametrize(
    "body",
    [
        {},
        {"incident_id": "inc01", "mode": "both"},
        {"incident_id": 1},
        {"incident_id": "inc01", "incident_report": {"title": "x"}},
        {"incident_report": {"title": "missing fields"}},
        {"incident_id": "inc01", "unexpected": True},
    ],
)
def test_invalid_body_is_422(client: TestClient, tmp_path: Path, body: dict[str, Any]):
    assert client.post("/investigations", json=body).status_code == 422
    assert list(tmp_path.iterdir()) == []


def test_unknown_investigation_id_is_404(client: TestClient):
    assert client.get("/investigations/does-not-exist").status_code == 404


class _Exploding:
    def preflight(self) -> None:
        return None

    def complete(self, *, system: str, prompt: str, model: str) -> LLMResponse:
        raise RuntimeError("model backend exploded")


def test_a_crashed_run_ends_failed_with_an_error_message(tmp_path: Path):
    app = create_app(llm_factory=_Exploding, results_dir=tmp_path, tool_backoff_s=0)
    with TestClient(app) as client:
        run_id = client.post("/investigations", json={"incident_id": "inc01"}).json()["id"]
        body = _wait(client, run_id)

    assert body["status"] == "failed"
    assert body["report"] is None
    assert body["error"]
    assert json.loads((tmp_path / f"{run_id}.json").read_text())["status"] == "failed"


def test_a_backend_that_cannot_be_built_also_ends_failed(tmp_path: Path):
    def broken() -> MockLLM:
        raise RuntimeError("no models.toml")

    app = create_app(llm_factory=broken, results_dir=tmp_path, tool_backoff_s=0)
    with TestClient(app) as client:
        body = _wait(client, client.post("/investigations", json={"incident_id": "inc01"}).json()["id"])

    assert body["status"] == "failed" and "no models.toml" in body["error"]


def test_an_inline_incident_report_runs_with_no_data_and_ends_inconclusive(client: TestClient):
    report = load_scenario("inc01").report.model_dump(mode="json")
    response = client.post("/investigations", json={"incident_report": report, "mode": "multi"})

    assert response.status_code == 202
    body = _wait(client, response.json()["id"])
    assert body["status"] == "done"
    assert body["incident_id"] == "inline"
    result = InvestigationReport.model_validate(body["report"])
    assert result.outcome == "INCONCLUSIVE"
    board = Blackboard.model_validate(body["blackboard"])
    assert board.incident.title == report["title"]
    assert board.evidence == []


def test_results_on_disk_are_served_after_a_restart(tmp_path: Path):
    with TestClient(create_app(llm_factory=_mock, results_dir=tmp_path, tool_backoff_s=0)) as client:
        run_id = client.post("/investigations", json={"incident_id": "inc02"}).json()["id"]
        first = _wait(client, run_id)

    with TestClient(create_app(llm_factory=_mock, results_dir=tmp_path, tool_backoff_s=0)) as client:
        assert client.get(f"/investigations/{run_id}").json() == first


def test_a_rejected_api_key_ends_the_run_failed_not_done(tmp_path: Path):
    from incident_investigator.llm.base import LLMAuthError

    class BadKey(MockLLM):
        def complete(self, *, system: str, prompt: str, model: str):  # type: ignore[override]
            raise LLMAuthError("the API rejected ANTHROPIC_API_KEY (401)")

    app = create_app(llm_factory=lambda: BadKey(delay_s=0.0), results_dir=tmp_path, tool_backoff_s=0)
    with TestClient(app) as client:
        run_id = client.post("/investigations", json={"incident_id": "inc01"}).json()["id"]
        body = _wait(client, run_id)

    assert body["status"] == "failed"
    assert body["report"] is None
    assert "ANTHROPIC_API_KEY" in body["error"]
