"""Input checks shared by the tools. Bad input becomes a ToolResult, never an exception."""

from __future__ import annotations

from datetime import datetime

from incident_investigator.schemas import Scenario, ToolResult


def invalid(reason: str) -> ToolResult:
    return ToolResult(status="error", error=f"invalid input: {reason}")


def check_service(scenario: Scenario, service: str | None) -> ToolResult | None:
    if service is not None and service not in scenario.services():
        known = ", ".join(sorted(scenario.services()))
        return invalid(f"unknown service {service!r} (known: {known})")
    return None


def check_window(start: datetime, end: datetime) -> ToolResult | None:
    if end < start:
        return invalid(
            f"time window ends ({end.isoformat()}) before it starts ({start.isoformat()})"
        )
    return None
