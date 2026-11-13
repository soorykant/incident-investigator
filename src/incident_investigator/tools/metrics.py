"""Metrics tool: metric samples for one service in a time window, as Evidence."""

from __future__ import annotations

from pydantic import AwareDatetime, BaseModel, ConfigDict

from incident_investigator.schemas import Evidence, Scenario, ToolResult
from incident_investigator.tools._common import check_service, check_window


class MetricsQuery(BaseModel):
    model_config = ConfigDict(extra="forbid")

    service: str
    start: AwareDatetime
    end: AwareDatetime
    name: str | None = None


def query_metrics(scenario: Scenario, query: MetricsQuery) -> ToolResult:
    if bad := check_service(scenario, query.service) or check_window(query.start, query.end):
        return bad
    rows = sorted(
        (
            row
            for row in scenario.metrics
            if row.service == query.service
            and query.start <= row.timestamp <= query.end
            and (query.name is None or row.name == query.name)
        ),
        key=lambda row: (row.timestamp, row.id),
    )
    evidence = [
        Evidence(
            id=row.id,
            kind="metric",
            timestamp=row.timestamp,
            payload={"service": row.service, "name": row.name, "value": row.value},
        )
        for row in rows
    ]
    return ToolResult(status="ok", evidence=evidence)
