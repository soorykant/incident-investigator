"""Loads incident scenarios and, for evaluation only, their answer keys.

This is the only module that knows where answer keys live. Agent, graph and
tool code must never import `load_answer_key` (tests/test_answer_key_isolation.py).
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from incident_investigator.schemas import Category, Scenario

DATA_DIR = Path(__file__).resolve().parents[2] / "data"
_INCIDENT_ID = re.compile(r"^inc\d{2}$")


class UnknownIncidentError(LookupError):
    pass


class Trap(BaseModel):
    model_config = ConfigDict(extra="forbid")

    description: str
    misleading_evidence_ids: list[str] = Field(min_length=1)


class AnswerKey(BaseModel):
    """Evaluation-only ground truth for one incident."""

    model_config = ConfigDict(extra="forbid")

    incident_id: str
    expected_outcome: Literal["root_cause", "inconclusive"]
    service: str | None
    category: Category | None
    key_evidence_ids: list[str]
    traps: list[Trap] = []


def list_incident_ids(data_dir: Path = DATA_DIR) -> list[str]:
    return sorted(p.stem for p in (data_dir / "incidents").glob("inc*.json"))


def _path(folder: str, incident_id: str, data_dir: Path) -> Path:
    path = data_dir / folder / f"{incident_id}.json"
    if not _INCIDENT_ID.match(incident_id) or not path.is_file():
        known = ", ".join(list_incident_ids(data_dir)) or "none"
        raise UnknownIncidentError(f"unknown incident {incident_id!r} (known: {known})")
    return path


def load_scenario(incident_id: str, data_dir: Path = DATA_DIR) -> Scenario:
    return Scenario.model_validate_json(_path("incidents", incident_id, data_dir).read_text())


def load_answer_key(incident_id: str, data_dir: Path = DATA_DIR) -> AnswerKey:
    return AnswerKey.model_validate_json(_path("answer_keys", incident_id, data_dir).read_text())
