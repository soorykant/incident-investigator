"""Dependencies tool: service-graph edges around one service, as Evidence."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict

from incident_investigator.schemas import Evidence, Scenario, ToolResult
from incident_investigator.tools._common import check_service


class DependenciesQuery(BaseModel):
    model_config = ConfigDict(extra="forbid")

    service: str
    direction: Literal["downstream", "upstream", "both"] = "downstream"
    """downstream: what `service` calls. upstream: what calls `service`."""


def query_dependencies(scenario: Scenario, query: DependenciesQuery) -> ToolResult:
    if bad := check_service(scenario, query.service):
        return bad
    down = query.direction in ("downstream", "both")
    up = query.direction in ("upstream", "both")
    evidence = [
        Evidence(
            id=row.id,
            kind="dependency",
            payload={"service": row.service, "depends_on": row.depends_on},
        )
        for row in sorted(scenario.dependencies, key=lambda row: row.id)
        if (down and row.service == query.service) or (up and row.depends_on == query.service)
    ]
    return ToolResult(status="ok", evidence=evidence)
