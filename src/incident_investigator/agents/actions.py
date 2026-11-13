"""The JSON action protocol agents speak (Q15).

Every model reply must be exactly one JSON object: either
  {"action": "call_tool", "tool": "<tool name>", "args": {...}}
or
  {"action": "finish", "facts": [...], "assumptions": [...], "hypotheses": [...]}
"""

from __future__ import annotations

from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, TypeAdapter, ValidationError, model_validator

from incident_investigator.blackboard import AgentFailure
from incident_investigator.json_text import extract_json
from incident_investigator.schemas import Assumption, Fact, Hypothesis, ToolName

__all__ = [
    "PROTOCOL",
    "ActionParseError",
    "AgentFailure",
    "CallToolAction",
    "FinishAction",
    "parse_action",
]


class CallToolAction(BaseModel):
    model_config = ConfigDict(extra="forbid")

    action: Literal["call_tool"]
    tool: ToolName
    args: dict[str, Any] = {}


class FinishAction(BaseModel):
    model_config = ConfigDict(extra="forbid")

    action: Literal["finish"]
    facts: list[Fact] = []
    assumptions: list[Assumption] = []
    hypotheses: list[Hypothesis] = []

    @model_validator(mode="before")
    @classmethod
    def _uncited_facts_are_assumptions(cls, data: Any) -> Any:
        """A "fact" with no evidence IDs is kept as an Assumption instead of rejecting the
        whole finish (and every tool call that led to it). The basis says why."""
        if not isinstance(data, dict) or not isinstance(data.get("facts"), list):
            return data
        facts: list[Any] = []
        moved: list[dict[str, str]] = []
        for fact in data["facts"]:
            if isinstance(fact, dict) and not fact.get("evidence_ids") and isinstance(fact.get("statement"), str):
                moved.append({
                    "statement": fact["statement"],
                    "basis": "Stated as a fact, but it cites no evidence IDs, so it is kept as an assumption.",
                    "how_to_verify": "Find a tool result that shows it and cite that row's evidence ID.",
                })  # fmt: skip
            else:
                facts.append(fact)
        if not moved:
            return data
        given = data.get("assumptions")
        assumptions: list[Any] = given if isinstance(given, list) else []
        return {**data, "facts": facts, "assumptions": [*assumptions, *moved]}


Action = Annotated[CallToolAction | FinishAction, Field(discriminator="action")]
_ACTION: TypeAdapter[CallToolAction | FinishAction] = TypeAdapter(Action)


class ActionParseError(ValueError):
    """The model's reply is not one valid action."""



PROTOCOL = (
    "Reply with exactly one JSON object and nothing else. Either call a tool:\n"
    '{"action": "call_tool", "tool": "<tool name>", "args": {...}}\n'
    "or finish:\n"
    '{"action": "finish", "facts": [{"statement": str, "evidence_ids": [str, ...]}], '
    '"assumptions": [{"statement": str, "basis": str, "how_to_verify": str}], '
    '"hypotheses": [{"id": str, "service": str, "category": one of bad_deploy | config_change | '
    "dependency_failure | capacity | cert_dns | db_migration | retry_storm | feature_flag | "
    'third_party_limit, "summary": str, "supporting_ids": [str], "contradicting_ids": [str], '
    '"proposed_confidence": number 0..1}]}\n'
    "Every fact must cite at least one evidence ID returned by a tool; a statement with no evidence ID "
    "is an assumption. Never invent evidence IDs."
)


def parse_action(text: str) -> CallToolAction | FinishAction:
    """The first JSON object in the reply (text around it is ignored), as an action."""
    try:
        return _ACTION.validate_json(extract_json(text))
    except ValidationError as exc:
        raise ActionParseError(_short(exc)) from exc


def _short(exc: ValidationError) -> str:
    errors = exc.errors(include_url=False)[:3]
    return "; ".join(f"{'.'.join(map(str, e['loc'])) or 'reply'}: {e['msg']}" for e in errors)
