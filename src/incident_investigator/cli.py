"""`investigate <incident_id>`: run one investigation and write the report as JSON."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from incident_investigator.dataset import UnknownIncidentError
from incident_investigator.investigate import MODES, investigate
from incident_investigator.llm.base import LLMAuthError, LLMClient, LLMUnavailableError
from incident_investigator.llm.router import RoleRouter, make_client
from incident_investigator.models_config import BACKENDS, ModelsConfigError, load_models


def _llm(name: str | None) -> LLMClient:
    """`name` overrides the backend for every role; None routes each role as models.toml says."""
    models = load_models()
    if name is None:
        # The graph asks the router for each role's client, so one model may run on two backends.
        return RoleRouter(models, route_by_model=False)
    return make_client(name, models)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="investigate", description=__doc__)
    parser.add_argument("incident_id", help="incident to investigate, e.g. inc01")
    parser.add_argument(
        "--mode",
        choices=MODES,
        default="multi",
        help="multi: the multi-agent graph (default). single: the single-agent baseline, "
        "with the same tools, limits, evidence rules and report schema.",
    )
    parser.add_argument(
        "--llm",
        choices=list(BACKENDS),
        default=None,
        help="LLM backend for every role, overriding models.toml: anthropic_api (ANTHROPIC_API_KEY), "
        "openai_compat (local model) or mock (offline). "
        "Default: each role's backend from models.toml (anthropic_api for all).",
    )
    parser.add_argument("--out", type=Path, default=Path("runs"), help="report output folder")
    args = parser.parse_args(argv)

    try:
        llm = _llm(args.llm)
        llm.preflight()
    except (LLMUnavailableError, ModelsConfigError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 3
    try:
        run = investigate(args.incident_id, llm, mode=args.mode)
    except UnknownIncidentError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    except LLMAuthError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 3

    report = run.report
    args.out.mkdir(parents=True, exist_ok=True)
    path = args.out / f"{report.incident_id}__{args.mode}.json"
    path.write_text(report.model_dump_json(indent=2))
    # The full blackboard (plan, findings, hypotheses, gaps, failures, task trace) next to it.
    trace = args.out / f"{report.incident_id}__{args.mode}.trace.json"
    trace.write_text(run.blackboard.model_dump_json(indent=2))
    print(f"{report.outcome}: report written to {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
