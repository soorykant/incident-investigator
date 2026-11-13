"""Logs tool: log rows for one service in a time window, as Evidence."""

from __future__ import annotations

from typing import Literal

from pydantic import AwareDatetime, BaseModel, ConfigDict

from incident_investigator.schemas import Evidence, Scenario, ToolResult
from incident_investigator.tools._common import check_service, check_window


class LogsQuery(BaseModel):
    model_config = ConfigDict(extra="forbid")

    service: str
    start: AwareDatetime
    end: AwareDatetime
    level: Literal["DEBUG", "INFO", "WARN", "ERROR"] | None = None


def query_logs(scenario: Scenario, query: LogsQuery) -> ToolResult:
    if bad := check_service(scenario, query.service) or check_window(query.start, query.end):
        return bad
    rows = sorted(
        (
            row
            for row in scenario.logs
            if row.service == query.service
            and query.start <= row.timestamp <= query.end
            and (query.level is None or row.level == query.level)
        ),
        key=lambda row: (row.timestamp, row.id),
    )
    evidence = [
        Evidence(
            id=row.id,
            kind="log",
            timestamp=row.timestamp,
            payload={"service": row.service, "level": row.level, "message": row.message},
        )
        for row in rows
    ]
    return ToolResult(status="ok", evidence=evidence)
