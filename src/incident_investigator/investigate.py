"""The single `investigate()` entry point shared by the CLI and (later) the API."""

from __future__ import annotations

from typing import Literal, get_args

from pydantic import BaseModel

from incident_investigator.baseline import run_baseline
from incident_investigator.blackboard import Blackboard
from incident_investigator.dataset import load_scenario
from incident_investigator.graph import build_graph
from incident_investigator.limits import DEFAULT_LIMITS, Clock, RunGuard, RunLimits, utc_now
from incident_investigator.llm.base import LLMClient
from incident_investigator.schemas import IncidentReport, InvestigationReport, Scenario
from incident_investigator.tools import DEFAULT_BACKOFF_S, ToolSession

# Graph steps, not a run limit: a Critic loop takes about 7 steps, and LangGraph's
# default of 25 would cut a long plan with 2 loops short.
RECURSION_LIMIT = 500

Mode = Literal["multi", "single"]
"""`multi`: the multi-agent graph. `single`: the single-agent baseline (ticket 07)."""
MODES: tuple[Mode, ...] = get_args(Mode)


class Investigation(BaseModel):
    """One finished run: the report and the full blackboard (tasks, findings, trace...)."""

    report: InvestigationReport
    blackboard: Blackboard


def investigate(
    incident_id: str,
    llm: LLMClient,
    *,
    mode: Mode = "multi",
    limits: RunLimits = DEFAULT_LIMITS,
    clock: Clock = utc_now,
    tool_backoff_s: float = DEFAULT_BACKOFF_S,
) -> Investigation:
    """Run one investigation in either mode. Raises UnknownIncidentError for an unknown
    ID and ValueError for an unknown mode.

    Both modes get a ToolSession over the same scenario (same tools, same failure
    schedule), one RunGuard over the same `limits` object, the same evidence rules and
    the same report schema. The scenario goes only into the ToolSession; the blackboard
    starts with just the incident report. `limits` and `clock` (for the wall-clock limit
    and the reported latency) are injectable for tests, and so is `tool_backoff_s`, the
    base backoff between retries of a timed-out tool call (0 makes tests fast)."""
    _check_mode(mode)
    scenario = load_scenario(incident_id)
    return _run(scenario.id, scenario, llm, mode=mode, limits=limits, clock=clock, tool_backoff_s=tool_backoff_s)


INLINE_INCIDENT_ID = "inline"
"""The incident ID under which an inline incident report is investigated."""


def investigate_report(
    report: IncidentReport,
    llm: LLMClient,
    *,
    mode: Mode = "multi",
    limits: RunLimits = DEFAULT_LIMITS,
    clock: Clock = utc_now,
    tool_backoff_s: float = DEFAULT_BACKOFF_S,
) -> Investigation:
    """Investigate an incident report that is not in the dataset (the API's inline path).

    No telemetry stands behind such a report, so the tools query an empty scenario:
    every tool returns no evidence, and the evidence rules end the run INCONCLUSIVE.
    Everything else (graph or baseline, limits, rules, report) is what `investigate()`
    runs."""
    _check_mode(mode)
    # model_construct: the `incNN` ID pattern is for dataset files, not for this run.
    scenario = Scenario.model_construct(id=INLINE_INCIDENT_ID, report=report)
    return _run(INLINE_INCIDENT_ID, scenario, llm, mode=mode, limits=limits, clock=clock, tool_backoff_s=tool_backoff_s)


def _check_mode(mode: str) -> None:
    if mode not in MODES:
        raise ValueError(f"unknown mode {mode!r}; choose one of: {', '.join(MODES)}")


def _run(
    incident_id: str,
    scenario: Scenario,
    llm: LLMClient,
    *,
    mode: Mode,
    limits: RunLimits,
    clock: Clock,
    tool_backoff_s: float,
) -> Investigation:
    tools = ToolSession(scenario, backoff_s=tool_backoff_s)
    guard = RunGuard(limits, clock=clock)
    start = Blackboard(incident_id=incident_id, incident=scenario.report, started_at=guard.started_at)
    if mode == "single":
        final = run_baseline(llm, tools, start, guard=guard)
    else:
        graph = build_graph(llm, tools, guard=guard)
        final = Blackboard.model_validate(graph.invoke(start, config={"recursion_limit": RECURSION_LIMIT}))
    assert final.report is not None
    return Investigation(report=final.report, blackboard=final)
