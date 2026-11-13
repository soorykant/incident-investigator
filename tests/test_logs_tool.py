import re
from datetime import datetime, timezone

from incident_investigator.dataset import load_scenario
from incident_investigator.tools.logs import LogsQuery, query_logs


def _utc(hh: int, mm: int) -> datetime:
    return datetime(2026, 11, 10, hh, mm, tzinfo=timezone.utc)


def test_logs_for_a_service_in_a_window_return_stable_evidence_ids():
    scenario = load_scenario("inc01")

    result = query_logs(
        scenario, LogsQuery(service="checkout-svc", start=_utc(10, 0), end=_utc(10, 30))
    )

    assert result.status == "ok"
    assert [e.id for e in result.evidence] == [
        "log:inc01:0002",
        "log:inc01:0003",
        "log:inc01:0004",
        "log:inc01:0006",
    ]
    assert all(re.fullmatch(r"log:inc01:\d{4}", e.id) for e in result.evidence)
    assert all(e.kind == "log" for e in result.evidence)
    assert result.evidence[1].payload["message"].startswith("NullPointerException")


def test_level_filter_keeps_only_matching_rows():
    scenario = load_scenario("inc01")

    result = query_logs(
        scenario,
        LogsQuery(service="checkout-svc", start=_utc(9, 0), end=_utc(12, 0), level="ERROR"),
    )

    assert [e.id for e in result.evidence] == ["log:inc01:0003", "log:inc01:0004", "log:inc01:0006"]


def test_same_query_returns_same_ids_every_time():
    scenario = load_scenario("inc01")
    query = LogsQuery(service="checkout-svc", start=_utc(9, 0), end=_utc(12, 0))

    assert query_logs(scenario, query) == query_logs(scenario, query)


def test_a_query_without_a_timezone_is_rejected_as_invalid_input():
    import pytest
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        LogsQuery(service="checkout-svc", start=datetime(2026, 11, 10, 10), end=_utc(10, 30))
