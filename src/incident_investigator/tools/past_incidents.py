"""Past-incidents tool: keyword and tag search over earlier incidents. No embeddings (Q3)."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict

from incident_investigator.schemas import Evidence, PastIncidentRow, Scenario, ToolResult
from incident_investigator.tools._common import invalid


class PastIncidentsQuery(BaseModel):
    model_config = ConfigDict(extra="forbid")

    keywords: list[str] = []
    """Case-insensitive substrings matched against title, root cause and resolution."""
    tags: list[str] = []
    """Case-insensitive exact tag matches."""


def _score(row: PastIncidentRow, keywords: list[str], tags: list[str]) -> int:
    text = " ".join([row.title, row.root_cause, row.resolution]).lower()
    row_tags = {t.lower() for t in row.tags}
    return sum(k in text for k in keywords) + sum(t in row_tags for t in tags)


def query_past_incidents(scenario: Scenario, query: PastIncidentsQuery) -> ToolResult:
    keywords = [k.strip().lower() for k in query.keywords if k.strip()]
    tags = [t.strip().lower() for t in query.tags if t.strip()]
    if not keywords and not tags:
        return invalid("give at least one keyword or tag")
    scored = [(_score(row, keywords, tags), row) for row in scenario.past_incidents]
    ranked = sorted((pair for pair in scored if pair[0] > 0), key=lambda p: (-p[0], p[1].id))
    evidence = [
        Evidence(
            id=row.id,
            kind="past",
            payload={
                "title": row.title,
                "tags": row.tags,
                "root_cause": row.root_cause,
                "resolution": row.resolution,
            },
        )
        for _, row in ranked
    ]
    return ToolResult(status="ok", evidence=evidence)
