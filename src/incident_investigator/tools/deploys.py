"""Deploys tool: deployments and config changes in a time window, as Evidence."""

from __future__ import annotations

from pydantic import AwareDatetime, BaseModel, ConfigDict

from incident_investigator.schemas import Evidence, Scenario, ToolResult
from incident_investigator.tools._common import check_service, check_window


class DeploysQuery(BaseModel):
    model_config = ConfigDict(extra="forbid")

    start: AwareDatetime
    end: AwareDatetime
    service: str | None = None
    """None means every service: a change elsewhere can cause the incident."""


def query_deploys(scenario: Scenario, query: DeploysQuery) -> ToolResult:
    if bad := check_service(scenario, query.service) or check_window(query.start, query.end):
        return bad

    def wanted(service: str, at: AwareDatetime) -> bool:
        return (query.service is None or service == query.service) and query.start <= at <= query.end

    evidence = [
        Evidence(
            id=row.id,
            kind="deploy",
            timestamp=row.timestamp,
            payload={"service": row.service, "version": row.version, "description": row.description},
        )
        for row in scenario.deploys
        if wanted(row.service, row.timestamp)
    ] + [
        Evidence(
            id=row.id,
            kind="config",
            timestamp=row.timestamp,
            payload={
                "service": row.service,
                "key": row.key,
                "old_value": row.old_value,
                "new_value": row.new_value,
            },
        )
        for row in scenario.config_changes
        if wanted(row.service, row.timestamp)
    ]
    evidence.sort(key=lambda e: (e.timestamp, e.id))
    return ToolResult(status="ok", evidence=evidence)
