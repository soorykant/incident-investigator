"""HTTP API over the same `investigate()` the CLI uses (ticket 11, Q19). Swagger at `/docs`.

`uv run api` serves it on 127.0.0.1:8000. Investigations run in a thread pool; their
state is kept in memory and in one JSON file per investigation under the results
folder (no database), so finished results are served again after a restart.
"""

from __future__ import annotations

import argparse
import threading
import traceback
import uuid
from collections.abc import AsyncIterator, Callable
from concurrent.futures import ThreadPoolExecutor
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Literal, Self

from fastapi import FastAPI, HTTPException, status
from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, ValidationError, model_validator

from incident_investigator.blackboard import Blackboard
from incident_investigator.dataset import list_incident_ids, load_scenario
from incident_investigator.investigate import (
    INLINE_INCIDENT_ID,
    Investigation,
    Mode,
    investigate,
    investigate_report,
)
from incident_investigator.limits import utc_now
from incident_investigator.llm.base import LLMClient
from incident_investigator.llm.router import RoleRouter
from incident_investigator.models_config import load_models
from incident_investigator.schemas import IncidentReport, InvestigationReport
from incident_investigator.tools import DEFAULT_BACKOFF_S

DEFAULT_RESULTS_DIR = Path("runs") / "api"
MAX_PARALLEL_RUNS = 2
"""Runs at once. Each run already makes up to 3 LLM calls in parallel."""

Status = Literal["pending", "running", "done", "failed"]


def default_llm() -> LLMClient:
    """Each role's backend and model from models.toml, as the CLI does without `--llm`."""
    return RoleRouter(load_models(), route_by_model=False)


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class StartInvestigation(_Strict):
    """Give exactly one of `incident_id` and `incident_report`."""

    incident_id: str | None = Field(default=None, description="a dataset incident, e.g. inc01 (see GET /incidents)")
    incident_report: IncidentReport | None = Field(
        default=None,
        description="an incident report not in the dataset. No telemetry stands behind it: "
        "every tool returns no evidence, so the run ends INCONCLUSIVE.",
    )
    mode: Mode = Field(default="multi", description="multi: the multi-agent graph. single: the baseline.")

    @model_validator(mode="after")
    def _exactly_one_incident(self) -> Self:
        if (self.incident_id is None) == (self.incident_report is None):
            raise ValueError("give exactly one of incident_id and incident_report")
        return self


class Started(_Strict):
    id: str


class InvestigationState(_Strict):
    id: str
    status: Status
    mode: Mode
    incident_id: str
    """The dataset incident ID, or `inline` for an inline incident report."""
    created_at: AwareDatetime
    finished_at: AwareDatetime | None = None
    report: InvestigationReport | None = None
    """Set when `status` is `done`."""
    blackboard: Blackboard | None = None
    """The full blackboard (tasks, findings, evidence, task trace...), set when `status` is `done`."""
    error: str | None = None
    """Set when `status` is `failed`."""


class IncidentSummary(_Strict):
    id: str
    title: str
    affected_service: str
    severity: str


class _Store:
    """In-memory investigation states, each mirrored to `<results_dir>/<id>.json`."""

    def __init__(self, results_dir: Path) -> None:
        self.results_dir = results_dir
        self._lock = threading.Lock()
        self._states: dict[str, InvestigationState] = {}

    def load(self) -> None:
        """Read earlier results. A run cut off by a restart can never finish, so it is failed."""
        if not self.results_dir.is_dir():
            return
        for path in self.results_dir.glob("*.json"):
            try:
                state = InvestigationState.model_validate_json(path.read_text())
            except ValidationError:
                continue
            if state.status in ("pending", "running"):
                state = state.model_copy(
                    update={"status": "failed", "error": "the server restarted before the run finished"}
                )
                self.put(state)
            self._states[state.id] = state

    def get(self, run_id: str) -> InvestigationState | None:
        with self._lock:
            return self._states.get(run_id)

    def put(self, state: InvestigationState) -> None:
        with self._lock:
            self._states[state.id] = state
            self.results_dir.mkdir(parents=True, exist_ok=True)
            tmp = self.results_dir / f"{state.id}.json.tmp"
            tmp.write_text(state.model_dump_json(indent=2))
            tmp.replace(self.results_dir / f"{state.id}.json")

    def update(self, run_id: str, **changes: object) -> None:
        current = self.get(run_id)
        assert current is not None
        self.put(current.model_copy(update=changes))


def create_app(
    *,
    llm_factory: Callable[[], LLMClient] = default_llm,
    results_dir: Path = DEFAULT_RESULTS_DIR,
    tool_backoff_s: float = DEFAULT_BACKOFF_S,
    max_parallel_runs: int = MAX_PARALLEL_RUNS,
) -> FastAPI:
    """Build the app. `llm_factory` makes one LLM client per run (tests pass the mock);
    `tool_backoff_s` is passed to `investigate()` (tests pass 0)."""
    store = _Store(results_dir)
    store.load()
    pool = ThreadPoolExecutor(max_workers=max_parallel_runs, thread_name_prefix="investigation")

    @asynccontextmanager
    async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
        yield
        pool.shutdown(wait=True, cancel_futures=True)

    app = FastAPI(
        title="Incident investigator",
        description="Start multi-agent (or single-agent baseline) investigations of production "
        "incidents and poll for their status, blackboard trace and report.",
        lifespan=lifespan,
    )

    def run(run_id: str, request: StartInvestigation) -> None:
        store.update(run_id, status="running")
        try:
            llm = llm_factory()
            llm.preflight()
            result: Investigation
            if request.incident_report is not None:
                result = investigate_report(
                    request.incident_report, llm, mode=request.mode, tool_backoff_s=tool_backoff_s
                )
            else:
                assert request.incident_id is not None
                result = investigate(request.incident_id, llm, mode=request.mode, tool_backoff_s=tool_backoff_s)
        except Exception as exc:  # any crash must end as `failed`, never a hang
            traceback.print_exc()
            store.update(run_id, status="failed", finished_at=utc_now(), error=f"{type(exc).__name__}: {exc}")
            return
        store.update(run_id, status="done", finished_at=utc_now(), report=result.report, blackboard=result.blackboard)

    @app.post("/investigations", status_code=status.HTTP_202_ACCEPTED, response_model=Started)
    def start_investigation(request: StartInvestigation) -> Started:
        """Start an investigation in the background and return its ID right away.
        An unknown `incident_id` returns 404 and starts no run."""
        if request.incident_id is not None and request.incident_id not in list_incident_ids():
            known = ", ".join(list_incident_ids())
            raise HTTPException(404, f"unknown incident {request.incident_id!r} (known: {known})")
        state = InvestigationState(
            id=uuid.uuid4().hex,
            status="pending",
            mode=request.mode,
            incident_id=request.incident_id or INLINE_INCIDENT_ID,
            created_at=utc_now(),
        )
        store.put(state)
        pool.submit(run, state.id, request)
        return Started(id=state.id)

    @app.get("/investigations/{investigation_id}", response_model=InvestigationState)
    def get_investigation(investigation_id: str) -> InvestigationState:
        """Status (`pending`, `running`, `done`, `failed`), and the report and blackboard
        trace once done, or the error once failed."""
        state = store.get(investigation_id)
        if state is None:
            raise HTTPException(404, f"unknown investigation {investigation_id!r}")
        return state

    @app.get("/incidents", response_model=list[IncidentSummary])
    def list_incidents() -> list[IncidentSummary]:
        """The incidents in the dataset: the incident report only, never an answer key."""
        summaries: list[IncidentSummary] = []
        for incident_id in list_incident_ids():
            report = load_scenario(incident_id).report
            summaries.append(
                IncidentSummary(
                    id=incident_id,
                    title=report.title,
                    affected_service=report.affected_service,
                    severity=report.severity,
                )
            )
        return summaries

    return app


def main(argv: list[str] | None = None) -> int:
    import uvicorn

    parser = argparse.ArgumentParser(prog="api", description="Serve the investigation API (Swagger at /docs).")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--results-dir", type=Path, default=DEFAULT_RESULTS_DIR, help="one JSON file per investigation")
    args = parser.parse_args(argv)
    uvicorn.run(create_app(results_dir=args.results_dir), host=args.host, port=args.port)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

