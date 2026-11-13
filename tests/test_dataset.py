from typing import get_args

import pytest
from pydantic import ValidationError

from incident_investigator.dataset import (
    UnknownIncidentError,
    list_incident_ids,
    load_answer_key,
    load_scenario,
)
from incident_investigator.schemas import Category, Fact, Hypothesis, Scenario
from incident_investigator.tools import TOOL_SPECS


def test_inc01_scenario_loads_with_its_incident_report():
    scenario = load_scenario("inc01")

    assert scenario.id == "inc01"
    assert scenario.report.affected_service == "checkout-svc"
    assert scenario.logs, "inc01 needs log rows for the logs tool"


def test_unknown_incident_raises_a_clear_error():
    with pytest.raises(UnknownIncidentError, match="inc99"):
        load_scenario("inc99")


def test_incident_ids_are_listed_from_the_data_folder():
    assert "inc01" in list_incident_ids()


def test_answer_key_is_stored_separately_and_cites_existing_evidence():
    scenario = load_scenario("inc01")
    key = load_answer_key("inc01")

    assert key.incident_id == "inc01"
    known = scenario.evidence_ids()
    assert set(key.key_evidence_ids) <= known
    for trap in key.traps:
        assert set(trap.misleading_evidence_ids) <= known


def test_scenario_does_not_carry_answer_key_fields():
    raw = load_scenario("inc01").model_dump()
    for leaked in ("expected_outcome", "category", "key_evidence_ids", "traps"):
        assert leaked not in raw


def test_scenario_rejects_evidence_ids_from_another_incident():
    raw = load_scenario("inc01").model_dump(mode="json")
    raw["logs"][0]["id"] = "log:inc02:0001"
    with pytest.raises(ValidationError, match="inc02"):
        Scenario.model_validate(raw)


def test_scenario_rejects_duplicate_evidence_ids():
    raw = load_scenario("inc01").model_dump(mode="json")
    raw["logs"][1]["id"] = raw["logs"][0]["id"]
    with pytest.raises(ValidationError, match="duplicate"):
        Scenario.model_validate(raw)


def test_a_fact_must_cite_at_least_one_evidence_id():
    with pytest.raises(ValidationError):
        Fact(statement="errors rose", evidence_ids=[])


def test_hypothesis_confidence_must_be_between_0_and_1():
    with pytest.raises(ValidationError):
        Hypothesis(
            id="h1",
            service="checkout-svc",
            category="bad_deploy",
            summary="bad deploy",
            proposed_confidence=1.2,
        )


def test_scenario_rejects_evidence_ids_without_a_numbered_row():
    raw = load_scenario("inc01").model_dump(mode="json")
    raw["logs"][0]["id"] = "log:inc01:x"
    with pytest.raises(ValidationError, match="log:inc01:x"):
        Scenario.model_validate(raw)


def test_dependency_edges_are_evidence_rows_with_checked_ids():
    scenario = load_scenario("inc01")
    assert "dependency:inc01:0001" in scenario.evidence_ids()

    raw = scenario.model_dump(mode="json")
    raw["dependencies"][1]["id"] = raw["dependencies"][0]["id"]
    with pytest.raises(ValidationError, match="duplicate"):
        Scenario.model_validate(raw)
    raw["dependencies"][1]["id"] = "dep:inc01:0002"
    with pytest.raises(ValidationError, match="dep:inc01:0002"):
        Scenario.model_validate(raw)


# --- The full 10-incident evaluation set (ticket 08) ---------------------------

ALL_IDS = [f"inc{n:02d}" for n in range(1, 11)]
FAILURE_MODES = {"timeout", "partial", "error"}

# Words that would hand an investigator the category outright if the incident
# report used them. The report describes symptoms; finding the cause is the job.
GIVEAWAYS: dict[str, list[str]] = {
    "bad_deploy": ["deploy", "release", "rollback", "rolled back"],
    "config_change": ["config", "connection pool", "pool size"],
    "dependency_failure": ["dependency", "downstream", "outage"],
    "capacity": ["capacity", "memory leak", "out of memory", "oom"],
    "cert_dns": ["certificate", "cert", "x509", "dns", "tls", "expir"],
    "db_migration": ["migration", "schema", "index"],
    "retry_storm": ["retry", "retries", "storm", "amplif"],
    "feature_flag": ["feature flag", "flag", "rollout"],
    "third_party_limit": ["rate limit", "rate-limit", "429", "quota", "third-party", "third party"],
}


def test_the_dataset_has_exactly_the_ten_planned_incidents():
    assert list_incident_ids() == ALL_IDS


@pytest.mark.parametrize("incident_id", ALL_IDS)
def test_every_scenario_and_answer_key_validates_and_cites_real_evidence(incident_id: str):
    scenario = load_scenario(incident_id)
    key = load_answer_key(incident_id)

    assert scenario.id == key.incident_id == incident_id
    known = scenario.evidence_ids()
    assert key.key_evidence_ids, "every key names the evidence an investigator should cite"
    assert set(key.key_evidence_ids) <= known
    for trap in key.traps:
        assert trap.description.strip()
        assert set(trap.misleading_evidence_ids) <= known
        assert not set(trap.misleading_evidence_ids) & set(key.key_evidence_ids)


@pytest.mark.parametrize("incident_id", ALL_IDS)
def test_every_incident_has_two_or_three_traps_and_a_failure_schedule(incident_id: str):
    scenario = load_scenario(incident_id)
    assert 2 <= len(load_answer_key(incident_id).traps) <= 3
    assert scenario.failure_schedule, "every incident injects at least one tool failure"
    assert scenario.dependencies and scenario.past_incidents


@pytest.mark.parametrize("incident_id", ALL_IDS)
def test_a_solvable_root_cause_names_a_service_the_tools_know(incident_id: str):
    key = load_answer_key(incident_id)
    if key.expected_outcome == "root_cause":
        assert key.service in load_scenario(incident_id).services()
        assert key.category is not None


def test_across_the_set_every_tool_fails_and_every_failure_mode_appears():
    seen = {(f.tool, f.mode) for i in ALL_IDS for f in load_scenario(i).failure_schedule}

    assert {tool for tool, _ in seen} == set(TOOL_SPECS)
    assert {mode for _, mode in seen} == FAILURE_MODES
    # Stronger than the ticket asks: every tool fails in every mode somewhere.
    assert seen == {(tool, mode) for tool in TOOL_SPECS for mode in FAILURE_MODES}


def test_inc09_is_inconclusive_by_design():
    key = load_answer_key("inc09")
    assert key.expected_outcome == "inconclusive"
    assert key.service is None and key.category is None


def test_inc10_is_a_third_party_rate_limit_behind_a_deploy_red_herring():
    key = load_answer_key("inc10")
    assert key.expected_outcome == "root_cause"
    assert key.category == "third_party_limit"
    deploys = {d.id for d in load_scenario("inc10").deploys}
    assert any(set(t.misleading_evidence_ids) & deploys for t in key.traps)


def test_the_nine_solvable_incidents_use_all_nine_categories():
    categories = {load_answer_key(i).category for i in ALL_IDS} - {None}
    assert categories == set(get_args(Category))


@pytest.mark.parametrize("incident_id", ALL_IDS)
def test_the_incident_report_does_not_give_away_its_answer(incident_id: str):
    report = load_scenario(incident_id).report
    key = load_answer_key(incident_id)
    text = " ".join([report.title, *report.symptoms]).lower()

    if key.category is not None:
        assert key.category.replace("_", " ") not in text.replace("_", " ")
        leaked = [w for w in GIVEAWAYS[key.category] if w in text]
        assert leaked == [], f"the report names its cause: {leaked}"
    if key.service is not None and key.service != report.affected_service:
        assert key.service not in text, "the report names the real culprit service"
