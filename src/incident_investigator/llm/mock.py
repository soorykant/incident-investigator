"""A deterministic stand-in LLM for tests and offline runs.

It plays every role (planner, observability, dependency, reasoning, critic, reporting,
the single-agent baseline and the evaluation judge), read from the `Role: <role>` line each system prompt
starts with. It knows nothing
about any incident: it uses only what the prompt shows it (the incident report,
tool results, findings, hypotheses). Each call sleeps `delay_s`, standing in for
model latency, so parallel tasks visibly overlap in the trace.

Its hypotheses use fixed categories, so mock runs say nothing about accuracy or
grounding quality (see ticket 01 comments).
"""

from __future__ import annotations

import json
import re
import time
from typing import Any

from incident_investigator.llm.base import LLMResponse, Usage

_ROLE = re.compile(r"^Role: (\w+)$", re.MULTILINE)
_EVIDENCE_ID = re.compile(r'"((?:log|metric|deploy|config|past|dependency):[a-z0-9]+:[^"]+)"')
_FIELD = r'"{}":\s*"([^"]*)"'
_TOOL_RESULT = re.compile(r"^Tool result: (.*)$", re.MULTILINE)
_HYPOTHESIS_ID = re.compile(r'^\s*"id":\s*"([^"]+)"', re.MULTILINE)
_WORD = re.compile(r"[a-z]{4,}")
_MAX_CITED = 3
_CRITIC_LOOPS = re.compile(r"Critic loops used so far: (\d+)")
_ID_LIST = r'"{}":\s*\[([^\]]*)\]'
_QUOTED = re.compile(r'"([^"]+)"')
_NOT_IN_SCENARIO = '"NOT IN SCENARIO"'


def _field(name: str, text: str, default: str = "") -> str:
    match = re.search(_FIELD.format(name), text)
    return match.group(1) if match else default


def _tool_results(prompt: str) -> list[dict[str, Any]]:
    results: list[dict[str, Any]] = []
    for raw in _TOOL_RESULT.findall(prompt):
        try:
            results.append(json.loads(raw))
        except json.JSONDecodeError:
            continue
    return results


class MockLLM:
    def __init__(self, delay_s: float = 0.01) -> None:
        self.delay_s = delay_s

    def preflight(self) -> None:
        return None

    def complete(self, *, system: str, prompt: str, model: str) -> LLMResponse:
        role_match = _ROLE.search(system)
        role = role_match.group(1) if role_match else "observability"
        reply = getattr(self, f"_{role}", self._observability)(prompt)
        time.sleep(self.delay_s)
        return LLMResponse(
            text=json.dumps(reply),
            usage=Usage(tokens_in=len(system + prompt) // 4, tokens_out=len(json.dumps(reply)) // 4),
            latency_s=self.delay_s,
        )

    # --- roles --------------------------------------------------------------

    def _planner(self, prompt: str) -> dict[str, Any]:
        service = _field("affected_service", prompt)
        return {
            "tasks": [
                {"id": "t1", "agent": "observability", "goal": f"Logs, metrics and changes for {service}",
                 "params": {"service": service}, "depends_on": []},
                {"id": "t2", "agent": "dependency", "goal": f"Dependencies of {service} and past incidents",
                 "params": {"service": service}, "depends_on": []},
                {"id": "t3", "agent": "reasoning", "goal": "Compare competing root causes",
                 "depends_on": ["t1", "t2"]},
            ]
        }  # fmt: skip

    def _observability(self, prompt: str) -> dict[str, Any]:
        service = _field("affected_service", prompt)
        window = {"start": _field("window_start", prompt), "end": _field("window_end", prompt)}
        steps = [
            {"tool": "logs", "args": {"service": service, **window}},
            {"tool": "metrics", "args": {"service": service, **window}},
            {"tool": "deploys", "args": window},
        ]
        return self._step(prompt, steps)

    def _dependency(self, prompt: str) -> dict[str, Any]:
        service = _field("affected_service", prompt)
        keywords = list(dict.fromkeys(_WORD.findall(_field("title", prompt).lower())))[:3]
        steps = [
            {"tool": "dependencies", "args": {"service": service, "direction": "both"}},
            {"tool": "past_incidents", "args": {"keywords": keywords}},
        ]
        return self._step(prompt, steps)

    def _baseline(self, prompt: str) -> dict[str, Any]:
        """The single agent: the Observability then the Dependency tool calls, then one
        finish with their facts and assumptions plus the Reasoning hypotheses."""
        service = _field("affected_service", prompt)
        window = {"start": _field("window_start", prompt), "end": _field("window_end", prompt)}
        keywords = list(dict.fromkeys(_WORD.findall(_field("title", prompt).lower())))[:3]
        steps = [
            {"tool": "logs", "args": {"service": service, **window}},
            {"tool": "metrics", "args": {"service": service, **window}},
            {"tool": "deploys", "args": window},
            {"tool": "dependencies", "args": {"service": service, "direction": "both"}},
            {"tool": "past_incidents", "args": {"keywords": keywords}},
        ]
        reply = self._step(prompt, steps)
        if reply["action"] == "finish":
            reasoning = self._reasoning(prompt)
            reply["hypotheses"] = reasoning["hypotheses"]
            reply["assumptions"] += reasoning["assumptions"]
        return reply

    def _step(self, prompt: str, steps: list[dict[str, Any]]) -> dict[str, Any]:
        results = _tool_results(prompt)
        if len(results) < len(steps):
            return {"action": "call_tool", **steps[len(results)]}
        facts: list[dict[str, Any]] = []
        assumptions: list[dict[str, Any]] = []
        for step, result in zip(steps, results, strict=False):
            ids = [e["id"] for e in result.get("evidence", [])]
            if ids:
                facts.append({
                    "statement": f"{step['tool']} returned {len(ids)} rows ({result['status']})",
                    "evidence_ids": ids[:_MAX_CITED],
                })  # fmt: skip
            if result.get("status") != "ok":
                assumptions.append({
                    "statement": f"Some {step['tool']} data is missing ({result.get('status')})",
                    "basis": str(result.get("error") or f"{result.get('omitted', 0)} rows omitted"),
                    "how_to_verify": f"Repeat the {step['tool']} query",
                })  # fmt: skip
        return {"action": "finish", "facts": facts, "assumptions": assumptions}

    def _reasoning(self, prompt: str) -> dict[str, Any]:
        service = _field("affected_service", prompt)
        ids = list(dict.fromkeys(_EVIDENCE_ID.findall(prompt)))
        change = [i for i in ids if i.startswith(("deploy:", "config:", "log:"))] or ids
        graph = [i for i in ids if i.startswith(("dependency:", "past:", "metric:"))] or ids
        return {
            "action": "finish",
            "hypotheses": [
                {"id": "h1", "service": service, "category": "bad_deploy",
                 "summary": f"A recent change broke {service}",
                 "supporting_ids": change[:_MAX_CITED], "contradicting_ids": graph[:1],
                 "proposed_confidence": 0.7},
                {"id": "h2", "service": service, "category": "dependency_failure",
                 "summary": f"A service {service} depends on is failing",
                 "supporting_ids": graph[:_MAX_CITED], "contradicting_ids": change[:1],
                 "proposed_confidence": 0.4},
            ],
            "assumptions": [
                {"statement": "Mock reasoning ranks a recent change first",
                 "basis": "Changes inside the incident window are the most common cause",
                 "how_to_verify": "Roll back the change and watch the error rate"},
            ],
        }  # fmt: skip

    def _critic(self, prompt: str) -> dict[str, Any]:
        """First review: one follow-up (metrics for the affected service over the
        incident window), so mock runs exercise the Critic loop. Later reviews: satisfied."""
        match = _CRITIC_LOOPS.search(prompt)
        if match is None or int(match.group(1)) > 0:
            return {"critique": "Mock critic: the evidence is enough.", "follow_ups": []}
        service = _field("affected_service", prompt)
        window = {"start": _field("window_start", prompt), "end": _field("window_end", prompt)}
        return {
            "critique": f"Mock critic: confirm the {service} metrics across the incident window.",
            "follow_ups": [
                {"agent": "observability", "goal": f"Check metrics for {service} across the incident window",
                 "params": {"service": service, **window}},
            ],
        }  # fmt: skip

    def _reporting(self, prompt: str) -> dict[str, Any]:
        section = prompt.split("Ranked hypotheses", 1)[-1].split("Open evidence gaps", 1)[0]
        return {
            "recommended_actions": [
                {"action": f"Verify hypothesis {h} against fresh data", "rationale": "Mock recommendation",
                 "hypothesis_id": h}
                for h in dict.fromkeys(_HYPOTHESIS_ID.findall(section))
            ]
        }  # fmt: skip

    def _judge(self, prompt: str) -> dict[str, Any]:
        """Scores read off the prompt alone: evidence support drops with every cited ID
        the data does not hold, and the trap counts as "fallen for" when the top
        hypothesis's supporting IDs include an ID the prompt lists as misleading."""
        misleading = {
            i for group in re.findall(_ID_LIST.format("misleading_evidence_ids"), prompt)
            for i in _QUOTED.findall(group)
        }  # fmt: skip
        review = prompt.split("Report under review", 1)[-1]
        top = re.search(_ID_LIST.format("supporting_ids"), review)
        trap_ids = sorted(misleading & set(_QUOTED.findall(top.group(1)))) if top else []
        unknown = review.count(_NOT_IN_SCENARIO)
        actions = '"recommended_actions": []' not in review
        return {
            "evidence_support": {"score": max(1, 4 - unknown),
                                 "rationale": f"Mock judge: {unknown} cited IDs are not in the data"},
            "fact_assumption_separation": {"score": 3, "rationale": "Mock judge: fixed score"},
            "recommended_actions": {"score": 3 if actions else 1,
                                    "rationale": "Mock judge: actions present" if actions else "Mock judge: none"},
            "fell_for_trap": {"answer": "yes" if trap_ids else "no",
                              "rationale": f"Mock judge: the top hypothesis cites misleading IDs {trap_ids}"
                              if trap_ids else "Mock judge: the top hypothesis cites no misleading ID"},
        }  # fmt: skip
