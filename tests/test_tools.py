"""Tool behaviour at the public seams (PRD step 2)."""

from datetime import datetime, timezone
from typing import Any

import pytest

from incident_investigator.dataset import load_scenario
from incident_investigator.schemas import FailureSpec, Scenario, ToolResult
from incident_investigator.tools import TOOL_SPECS, ToolSession
from incident_investigator.tools.dependencies import DependenciesQuery, query_dependencies
from incident_investigator.tools.deploys import DeploysQuery, query_deploys
from incident_investigator.tools.logs import LogsQuery, query_logs
from incident_investigator.tools.metrics import MetricsQuery, query_metrics
from incident_investigator.tools.past_incidents import PastIncidentsQuery, query_past_incidents


def _utc(hh: int, mm: int) -> datetime:
    return datetime(2026, 11, 10, hh, mm, tzinfo=timezone.utc)


# --- invalid input returns status=error and does not raise --------------------


def test_logs_for_an_unknown_service_is_an_error_not_an_exception():
    result = query_logs(
        load_scenario("inc01"), LogsQuery(service="nope-svc", start=_utc(9, 0), end=_utc(12, 0))
    )

    assert result.status == "error"
    assert result.evidence == []
    assert result.error is not None and "nope-svc" in result.error


def test_logs_with_an_inverted_window_is_an_error_not_an_exception():
    result = query_logs(
        load_scenario("inc01"),
        LogsQuery(service="checkout-svc", start=_utc(12, 0), end=_utc(9, 0)),
    )

    assert result.status == "error"
    assert result.evidence == []
    assert result.error is not None and "window" in result.error


# --- metrics -----------------------------------------------------------------


def test_metrics_for_a_service_in_a_window_return_stable_evidence_ids():
    result = query_metrics(
        load_scenario("inc01"),
        MetricsQuery(service="checkout-svc", start=_utc(9, 45), end=_utc(10, 30)),
    )

    assert result.status == "ok"
    assert [e.id for e in result.evidence] == [
        "metric:inc01:err:0001",
        "metric:inc01:cpu:0001",
        "metric:inc01:err:0002",
    ]
    assert all(e.kind == "metric" for e in result.evidence)
    assert result.evidence[2].payload == {
        "service": "checkout-svc",
        "name": "error_rate_pct",
        "value": 23.5,
    }


def test_metrics_can_be_filtered_by_name():
    result = query_metrics(
        load_scenario("inc01"),
        MetricsQuery(
            service="checkout-svc", start=_utc(9, 45), end=_utc(10, 30), name="error_rate_pct"
        ),
    )

    assert [e.id for e in result.evidence] == ["metric:inc01:err:0001", "metric:inc01:err:0002"]


def test_metrics_reject_unknown_service_and_inverted_window_without_raising():
    scenario = load_scenario("inc01")

    unknown = query_metrics(scenario, MetricsQuery(service="x", start=_utc(9, 0), end=_utc(12, 0)))
    inverted = query_metrics(
        scenario, MetricsQuery(service="checkout-svc", start=_utc(12, 0), end=_utc(9, 0))
    )

    assert (unknown.status, inverted.status) == ("error", "error")


# --- deploys (including config changes) ---------------------------------------


def test_deploys_include_config_changes_in_time_order():
    result = query_deploys(
        load_scenario("inc01"), DeploysQuery(start=_utc(9, 45), end=_utc(10, 30))
    )

    assert result.status == "ok"
    assert [(e.id, e.kind) for e in result.evidence] == [
        ("config:inc01:0001", "config"),
        ("deploy:inc01:0001", "deploy"),
    ]
    assert result.evidence[0].payload == {
        "service": "search-svc",
        "key": "cache.ttl_seconds",
        "old_value": "300",
        "new_value": "60",
    }
    assert result.evidence[1].payload == {
        "service": "checkout-svc",
        "version": "v2.3.1",
        "description": "Discount engine refactor",
    }


def test_deploys_can_be_narrowed_to_one_service():
    result = query_deploys(
        load_scenario("inc01"),
        DeploysQuery(service="checkout-svc", start=_utc(0, 0), end=_utc(23, 0)),
    )

    assert [e.id for e in result.evidence] == ["deploy:inc01:0001"]


def test_deploys_reject_unknown_service_and_inverted_window_without_raising():
    scenario = load_scenario("inc01")

    unknown = query_deploys(scenario, DeploysQuery(service="x", start=_utc(9, 0), end=_utc(12, 0)))
    inverted = query_deploys(scenario, DeploysQuery(start=_utc(12, 0), end=_utc(9, 0)))

    assert (unknown.status, inverted.status) == ("error", "error")


# --- dependencies ---------------------------------------------------------------


def test_dependencies_return_edges_as_evidence_with_stable_ids():
    scenario = load_scenario("inc01")

    downstream = query_dependencies(scenario, DependenciesQuery(service="checkout-svc"))
    upstream = query_dependencies(
        scenario, DependenciesQuery(service="checkout-svc", direction="upstream")
    )
    both = query_dependencies(scenario, DependenciesQuery(service="checkout-svc", direction="both"))

    assert downstream.status == "ok"
    assert [e.id for e in downstream.evidence] == ["dependency:inc01:0001", "dependency:inc01:0002"]
    assert downstream.evidence[1].kind == "dependency"
    assert downstream.evidence[1].payload == {"service": "checkout-svc", "depends_on": "pricing-db"}
    assert [e.id for e in upstream.evidence] == ["dependency:inc01:0004"]
    assert [e.id for e in both.evidence] == [
        "dependency:inc01:0001",
        "dependency:inc01:0002",
        "dependency:inc01:0004",
    ]


def test_dependencies_of_an_unknown_service_is_an_error_not_an_exception():
    result = query_dependencies(load_scenario("inc01"), DependenciesQuery(service="nope-svc"))

    assert result.status == "error"


# --- past incidents (keyword / tag search, no embeddings) -----------------------


def test_past_incidents_are_found_by_keyword_case_insensitively():
    result = query_past_incidents(
        load_scenario("inc01"), PastIncidentsQuery(keywords=["DISCOUNT"])
    )

    assert result.status == "ok"
    assert [e.id for e in result.evidence] == ["past:inc01:0002"]
    assert result.evidence[0].kind == "past"
    assert result.evidence[0].payload["title"] == "Checkout 500s after a pricing release"


def test_past_incidents_are_found_by_tag():
    result = query_past_incidents(load_scenario("inc01"), PastIncidentsQuery(tags=["cpu"]))

    assert [e.id for e in result.evidence] == ["past:inc01:0001"]


def test_past_incidents_matching_more_terms_rank_first():
    result = query_past_incidents(
        load_scenario("inc01"), PastIncidentsQuery(keywords=["checkout", "rolled back"])
    )

    assert [e.id for e in result.evidence] == ["past:inc01:0002", "past:inc01:0001"]


def test_past_incidents_search_without_terms_is_an_error_not_an_exception():
    result = query_past_incidents(load_scenario("inc01"), PastIncidentsQuery())

    assert result.status == "error"


# --- dispatcher: tool name + raw args -> ToolResult -----------------------------

WINDOW = {"start": "2026-11-10T09:45:00Z", "end": "2026-11-10T10:30:00Z"}
CHECKOUT_LOGS = {
    "service": "checkout-svc",
    "start": "2026-11-10T10:00:00Z",
    "end": "2026-11-10T10:30:00Z",
}


def _with_schedule(*specs: dict[str, Any]) -> Scenario:
    return load_scenario("inc01").model_copy(
        update={"failure_schedule": [FailureSpec.model_validate(s) for s in specs]}
    )


def _session(scenario: Scenario, sleeps: list[float] | None = None) -> ToolSession:
    record = sleeps if sleeps is not None else []
    return ToolSession(scenario, backoff_s=0.5, sleep=record.append)


def test_every_tool_is_registered_with_a_typed_input_schema():
    assert set(TOOL_SPECS) == {"logs", "metrics", "deploys", "dependencies", "past_incidents"}
    for spec in TOOL_SPECS.values():
        assert spec.description
        assert "properties" in spec.input_model.model_json_schema()


def test_dispatching_by_name_returns_the_same_evidence_as_the_tool():
    session = _session(_with_schedule())

    result = session.call("logs", CHECKOUT_LOGS)

    assert result.status == "ok"
    assert [e.id for e in result.evidence] == [
        "log:inc01:0002",
        "log:inc01:0003",
        "log:inc01:0004",
        "log:inc01:0006",
    ]
    assert result.gap is None


def test_each_tool_answers_through_the_dispatcher():
    session = _session(_with_schedule())

    assert [e.id for e in session.call("metrics", {**CHECKOUT_LOGS, "name": "node_cpu_pct"}).evidence] == [
        "metric:inc01:cpu:0001"
    ]
    assert [e.id for e in session.call("deploys", {"service": "checkout-svc", **WINDOW}).evidence] == [
        "deploy:inc01:0001"
    ]
    assert [e.id for e in session.call("dependencies", {"service": "web-frontend"}).evidence] == [
        "dependency:inc01:0004"
    ]
    assert [e.id for e in session.call("past_incidents", {"tags": ["rollback"]}).evidence] == [
        "past:inc01:0002"
    ]


@pytest.mark.parametrize(
    ("tool", "args"),
    [
        ("traces", {}),
        ("logs", {"service": "nope-svc", **WINDOW}),
        ("logs", {"service": "checkout-svc", "start": WINDOW["end"], "end": WINDOW["start"]}),
        ("logs", {"service": "checkout-svc", "start": "2026-11-10T09:45:00", "end": WINDOW["end"]}),
        ("logs", {"service": "checkout-svc"}),
        ("logs", {**CHECKOUT_LOGS, "colour": "red"}),
        ("dependencies", {"service": "checkout-svc", "direction": "sideways"}),
    ],
    ids=["unknown-tool", "unknown-service", "inverted-window", "naive-time", "missing", "extra", "bad-enum"],
)
def test_invalid_input_through_the_dispatcher_is_an_error_not_an_exception(
    tool: str, args: dict[str, Any]
):
    result = _session(_with_schedule()).call(tool, args)

    assert result.status == "error"
    assert result.error
    assert result.evidence == []
    assert result.gap is None


# --- failure injection ------------------------------------------------------------


def test_a_scheduled_timeout_is_retried_twice_with_backoff_then_recorded_as_a_gap():
    sleeps: list[float] = []
    session = _session(_with_schedule({"tool": "metrics", "call_index": 2, "mode": "timeout"}), sleeps)

    first = session.call("metrics", CHECKOUT_LOGS)
    second = session.call("metrics", CHECKOUT_LOGS)
    third = session.call("metrics", CHECKOUT_LOGS)

    assert (first.status, second.status, third.status) == ("ok", "timeout", "ok")
    assert second.attempts == 3
    assert second.evidence == []
    assert sleeps == [0.5, 1.0]
    assert second.gap is not None
    assert second.gap.tool == "metrics"
    assert "timeout" in second.gap.reason
    assert third.evidence == first.evidence


def test_a_later_successful_call_for_the_same_service_closes_the_gap():
    session = _session(_with_schedule({"tool": "metrics", "call_index": 1, "mode": "timeout"}))

    failed = session.call("metrics", CHECKOUT_LOGS)
    # A narrower query for the same service still answers the question the timeout left open.
    retried = session.call("metrics", {**CHECKOUT_LOGS, "name": "node_cpu_pct"})
    again = session.call("metrics", CHECKOUT_LOGS)

    assert failed.gap is not None and failed.gap.id and failed.gap.service == "checkout-svc"
    assert retried.status == "ok"
    assert retried.closes_gaps == [failed.gap.id]
    assert again.closes_gaps == [], "a gap closes once"


def test_a_successful_call_for_another_service_or_tool_does_not_close_the_gap():
    session = _session(_with_schedule({"tool": "metrics", "call_index": 1, "mode": "timeout"}))

    failed = session.call("metrics", CHECKOUT_LOGS)
    other_service = session.call("metrics", {**CHECKOUT_LOGS, "service": "payments-svc"})
    other_tool = session.call("logs", CHECKOUT_LOGS)

    assert failed.gap is not None
    assert other_service.status == "ok" and other_service.closes_gaps == []
    assert other_tool.status == "ok" and other_tool.closes_gaps == []


def test_a_partial_result_does_not_close_an_earlier_gap():
    session = _session(
        _with_schedule(
            {"tool": "logs", "call_index": 1, "mode": "error"},
            {"tool": "logs", "call_index": 2, "mode": "partial"},
        )
    )

    failed = session.call("logs", CHECKOUT_LOGS)
    partial = session.call("logs", CHECKOUT_LOGS)
    full = session.call("logs", CHECKOUT_LOGS)

    assert partial.status == "partial" and partial.closes_gaps == []
    assert failed.gap is not None and partial.gap is not None
    assert full.closes_gaps == [failed.gap.id, partial.gap.id]
    assert failed.gap.id != partial.gap.id


def test_a_scheduled_partial_returns_a_marked_subset_of_the_rows():
    session = _session(
        _with_schedule({"tool": "logs", "call_index": 1, "mode": "partial", "partial_fraction": 0.5})
    )

    result = session.call("logs", CHECKOUT_LOGS)

    assert result.status == "partial"
    assert [e.id for e in result.evidence] == ["log:inc01:0002", "log:inc01:0003"]
    assert result.omitted == 2
    assert result.gap is not None and result.gap.tool == "logs"


def test_a_scheduled_error_returns_status_error_and_a_gap():
    session = _session(_with_schedule({"tool": "deploys", "call_index": 1, "mode": "error"}))

    result = session.call("deploys", WINDOW)

    assert result.status == "error"
    assert result.evidence == []
    assert result.gap is not None and result.gap.tool == "deploys"


def test_the_call_counter_is_per_tool():
    session = _session(_with_schedule({"tool": "deploys", "call_index": 2, "mode": "error"}))

    session.call("logs", CHECKOUT_LOGS)
    first_deploys = session.call("deploys", WINDOW)
    second_deploys = session.call("deploys", WINDOW)

    assert (first_deploys.status, second_deploys.status) == ("ok", "error")


def test_invalid_input_does_not_use_up_a_scheduled_failure():
    session = _session(_with_schedule({"tool": "logs", "call_index": 1, "mode": "timeout"}))

    rejected = session.call("logs", {"service": "nope-svc", **WINDOW})
    accepted = session.call("logs", CHECKOUT_LOGS)

    assert (rejected.status, accepted.status) == ("error", "timeout")


def test_typed_queries_go_through_the_same_failure_schedule():
    session = _session(_with_schedule({"tool": "logs", "call_index": 1, "mode": "error"}))

    result = session.run(
        "logs", LogsQuery(service="checkout-svc", start=_utc(9, 45), end=_utc(10, 30))
    )

    assert result.status == "error"
    assert result.gap is not None


def test_the_same_scenario_and_call_sequence_always_produce_the_same_failures():
    scenario = load_scenario("inc01")
    sequence: list[tuple[str, dict[str, Any]]] = [
        ("logs", CHECKOUT_LOGS),
        ("metrics", CHECKOUT_LOGS),
        ("metrics", CHECKOUT_LOGS),
        ("past_incidents", {"keywords": ["checkout"]}),
        ("deploys", WINDOW),
        ("dependencies", {"service": "checkout-svc"}),
    ]

    def run() -> list[ToolResult]:
        session = _session(scenario)
        return [session.call(tool, args) for tool, args in sequence]

    first, second = run(), run()

    assert first == second
    assert {r.status for r in first} >= {"ok", "timeout", "partial"}


def test_inc01_schedules_a_metrics_timeout_and_a_past_incidents_partial():
    session = _session(load_scenario("inc01"))

    metrics = [session.call("metrics", CHECKOUT_LOGS) for _ in range(2)]
    past = session.call("past_incidents", {"keywords": ["checkout"]})

    assert [m.status for m in metrics] == ["ok", "timeout"]
    assert past.status == "partial"
    assert [e.id for e in past.evidence] == ["past:inc01:0001"]
    assert past.omitted == 1
