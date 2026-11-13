"""`eval`: run both systems on the incidents, score each pair, and write a summary (ticket 09, L25).

One `EvalResult` file per (incident, system) pair, `<run_dir>/<incident>__<system>.json`,
is both the result and the resume checkpoint. It is written atomically (temp file +
rename) only after the pair finishes, so a pair cut off midway leaves nothing behind
and a re-run with the same `--run` skips every pair that already has a file.

Then the LLM judge (ticket 10) scores every result whose `judge` field is still
empty, in the same order, and rewrites that pair's result file (atomically) with the
verdict or a recorded judge failure. Its tokens live in `judge`, never in the
report's metrics. A re-run skips judged pairs; a judge call cut off midway saves
nothing. `--no-judge` skips this pass; `--judge-only` runs only this pass on the
results a run already has, without investigating anything.

On a rate limit (`LLMRateLimitError`) the runner stops, keeps the completed pairs,
rewrites the summary, prints the exact resume command and exits 4. On a rejected API
key (`LLMAuthError`) it does the same but exits 3, so a bad key never fills the
results with INCONCLUSIVE reports built from failed calls. Any other exception
propagates after the completed pairs are saved.
"""

from __future__ import annotations

import argparse
import os
import shlex
import sys
import tempfile
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal

from pydantic import AwareDatetime, BaseModel, ConfigDict

from incident_investigator.dataset import list_incident_ids, load_answer_key, load_scenario
from incident_investigator.evaluation.judge import JudgeScores, judge_client, judge_report
from incident_investigator.evaluation.scoring import DeterministicScores, score
from incident_investigator.evaluation.summary import SUMMARY_FILE, render_summary
from incident_investigator.investigate import MODES, Mode, investigate
from incident_investigator.llm.base import LLMAuthError, LLMClient, LLMFatalError, LLMUnavailableError
from incident_investigator.llm.router import RoleRouter, make_client
from incident_investigator.models_config import BACKENDS, ModelsConfigError, load_models
from incident_investigator.schemas import InvestigationReport

DEFAULT_RESULTS_DIR = Path("eval") / "results"
MODELS_TOML_LABEL = "models.toml"
"""`llm` label of a run that routes each role as models.toml says (no `--llm`)."""
DEFAULT_LIVE_RUN = "live"
EXIT_UNKNOWN_INCIDENT = 2
EXIT_PREFLIGHT = 3
EXIT_RATE_LIMITED = 4


class EvalResult(BaseModel):
    """`eval/results/<run>/<incident>__<system>.json` (PRD data model, L25)."""

    model_config = ConfigDict(extra="forbid")

    schema_version: Literal[1] = 1
    run: str
    incident_id: str
    system: Mode
    llm: str
    """`models.toml` (per-role routing) or the backend every role was forced onto."""
    finished_at: AwareDatetime
    report: InvestigationReport
    """The full report; cost (tool/LLM calls, tokens in/out/cache, latency) is `report.metrics`."""
    scores: DeterministicScores
    judge: JudgeScores | None = None
    """LLM-judge verdict or recorded judge failure, plus the judge's own cost (ticket 10).
    None until the judge has looked at this pair. Never read back into `scores`."""


@dataclass
class EvalRun:
    written: list[Path] = field(default_factory=list[Path])
    skipped: list[Path] = field(default_factory=list[Path])
    judged: list[Path] = field(default_factory=list[Path])
    """Result files the judge wrote a verdict or a judge failure into during this call."""
    judge_failed: list[Path] = field(default_factory=list[Path])
    rate_limited: str | None = None
    """The rate-limit message that stopped the run, if one did."""
    auth_failed: str | None = None
    """The rejected-key message that stopped the run, if one did."""

    @property
    def stopped(self) -> bool:
        return self.rate_limited is not None or self.auth_failed is not None
    summary: Path | None = None


def result_path(run_dir: Path, incident_id: str, system: str) -> Path:
    return run_dir / f"{incident_id}__{system}.json"


def write_atomically(path: Path, text: str) -> None:
    """Write via a temp file in the same folder and an atomic rename: the file is whole or absent."""
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w") as handle:
            handle.write(text)
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


def load_results(run_dir: Path) -> list[EvalResult]:
    paths = sorted(p for p in run_dir.glob("*__*.json") if not p.name.endswith(".trace.json"))
    return [EvalResult.model_validate_json(p.read_text()) for p in paths]


def run_eval(
    llm: LLMClient,
    run_dir: Path,
    *,
    incident_ids: Sequence[str] | None = None,
    systems: Sequence[Mode] = MODES,
    llm_label: str,
    judge: bool = True,
    run_systems: bool = True,
    log: Callable[[str], None] = print,
) -> EvalRun:
    """Run every missing (incident, system) pair, in incident order, multi before single,
    then (with `judge`) judge every result of those pairs that has no judge verdict yet.
    `run_systems=False` investigates nothing: it only judges the results already on disk.

    Returns normally when all pairs are done or a rate limit or a rejected key stopped
    the run (`EvalRun.rate_limited`, `EvalRun.auth_failed`); the summary is rewritten
    in every case."""
    ids = list(incident_ids) if incident_ids is not None else list_incident_ids()
    run_dir.mkdir(parents=True, exist_ok=True)
    outcome = EvalRun()
    pairs: list[tuple[str, Mode]] = [(i, s) for i in ids for s in systems]
    if run_systems:
        _investigate_pairs(llm, run_dir, pairs, outcome, llm_label=llm_label, log=log)
    if judge and not outcome.stopped:
        _judge_pairs(llm, run_dir, pairs, outcome, log=log)
    summary = run_dir / SUMMARY_FILE
    write_atomically(summary, render_summary(load_results(run_dir), run=run_dir.name, expected_pairs=pairs))
    outcome.summary = summary
    return outcome


def _investigate_pairs(
    llm: LLMClient,
    run_dir: Path,
    pairs: Sequence[tuple[str, Mode]],
    outcome: EvalRun,
    *,
    llm_label: str,
    log: Callable[[str], None],
) -> None:
    for n, (incident_id, system) in enumerate(pairs, start=1):
        path = result_path(run_dir, incident_id, system)
        if path.exists():
            outcome.skipped.append(path)
            log(f"[{n}/{len(pairs)}] {incident_id} {system}: skipped (result exists)")
            continue
        try:
            run = investigate(incident_id, llm, mode=system)
        except LLMFatalError as exc:
            _stop(outcome, exc, log, f"[{n}/{len(pairs)}] {incident_id} {system}")
            return
        scenario = load_scenario(incident_id)
        result = EvalResult(
            run=run_dir.name,
            incident_id=incident_id,
            system=system,
            llm=llm_label,
            finished_at=datetime.now(UTC),
            report=run.report,
            scores=score(run.report, load_answer_key(incident_id), scenario.evidence_ids()),
        )
        # The trace (all evidence the tools returned) first; the result file is the checkpoint.
        write_atomically(run_dir / f"{incident_id}__{system}.trace.json", run.blackboard.model_dump_json(indent=2))
        write_atomically(path, result.model_dump_json(indent=2))
        outcome.written.append(path)
        s, m = result.scores, run.report.metrics
        log(
            f"[{n}/{len(pairs)}] {incident_id} {system}: {run.report.outcome} correct={s.correct} "
            f"grounding={s.grounding} tool_calls={m.tool_calls} llm_calls={m.llm_calls} latency={m.latency_s}s"
        )


def _stop(outcome: EvalRun, exc: LLMFatalError, log: Callable[[str], None], where: str) -> None:
    if isinstance(exc, LLMAuthError):
        outcome.auth_failed = str(exc)
        log(f"{where}: stopped, the API rejected the key: {exc}")
    else:
        outcome.rate_limited = str(exc)
        log(f"{where}: stopped by a rate limit: {exc}")


def _judge_pairs(
    llm: LLMClient,
    run_dir: Path,
    pairs: Sequence[tuple[str, Mode]],
    outcome: EvalRun,
    *,
    log: Callable[[str], None],
) -> None:
    """Judge each pair that has a result file and no judge verdict; save each as it finishes."""
    client, model = judge_client(llm)
    for n, (incident_id, system) in enumerate(pairs, start=1):
        path = result_path(run_dir, incident_id, system)
        if not path.exists():
            continue
        result = EvalResult.model_validate_json(path.read_text())
        if result.judge is not None:
            continue
        try:
            verdict = judge_report(
                client,
                report=result.report,
                answer_key=load_answer_key(incident_id),
                scenario=load_scenario(incident_id),
                model=model,
            )
        except LLMFatalError as exc:
            _stop(outcome, exc, log, f"[judge {n}/{len(pairs)}] {incident_id} {system}")
            return
        write_atomically(path, result.model_copy(update={"judge": verdict}).model_dump_json(indent=2))
        outcome.judged.append(path)
        if verdict.verdict is None:
            outcome.judge_failed.append(path)
            log(f"[judge {n}/{len(pairs)}] {incident_id} {system}: judge failure ({verdict.error})")
            continue
        v = verdict.verdict
        log(
            f"[judge {n}/{len(pairs)}] {incident_id} {system}: evidence={v.evidence_support.score} "
            f"separation={v.fact_assumption_separation.score} actions={v.recommended_actions.score} "
            f"fell_for_trap={v.fell_for_trap.answer}"
        )


def build_llm(name: str | None) -> LLMClient:
    """`name` forces one backend for every role; None routes each role as models.toml says."""
    models = load_models()
    if name is None:
        return RoleRouter(models, route_by_model=False)
    return make_client(name, models)


def _resume_command(args: argparse.Namespace, run: str) -> str:
    parts = ["uv", "run", "eval"]
    if args.judge_only:
        parts += ["--judge-only"]
    elif args.no_judge:
        parts += ["--no-judge"]
    if args.all:
        parts += ["--all"]
    elif args.incidents is not None:
        parts += ["--incidents", ",".join(args.incidents)]
    if list(args.systems) != list(MODES):
        parts += ["--systems", *args.systems]
    if args.llm is not None:
        parts += ["--llm", args.llm]
    if args.results_dir != DEFAULT_RESULTS_DIR:
        parts += ["--results-dir", str(args.results_dir)]
    parts += ["--run", run]
    return shlex.join(parts)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="eval", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    which = parser.add_mutually_exclusive_group()
    which.add_argument("--all", action="store_true", help="every incident in the dataset")
    which.add_argument(
        "--incidents", type=lambda s: [i for i in s.split(",") if i], help="comma-separated incident IDs"
    )
    judging = parser.add_mutually_exclusive_group()
    judging.add_argument("--no-judge", action="store_true", help="skip the LLM judge (deterministic scores only)")
    judging.add_argument(
        "--judge-only",
        action="store_true",
        help="only judge the results an existing run already has (pick it with --run); investigates nothing. "
        "Without --all/--incidents, every incident.",
    )
    parser.add_argument(
        "--systems", nargs="+", choices=MODES, default=list(MODES), help="systems to run (default: multi single)"
    )
    parser.add_argument(
        "--llm",
        choices=list(BACKENDS),
        default=None,
        help="force one LLM backend for every role (mock for offline runs). "
        "Default: each role's backend and model from models.toml.",
    )
    parser.add_argument(
        "--results-dir", type=Path, default=DEFAULT_RESULTS_DIR, help=f"default: {DEFAULT_RESULTS_DIR}"
    )
    parser.add_argument(
        "--run",
        default=None,
        help="run name: results go to <results-dir>/<run>/, and re-running with the same name resumes. "
        f"Default: {DEFAULT_LIVE_RUN!r} with models.toml, else the --llm backend name.",
    )
    args = parser.parse_args(argv)
    if not (args.all or args.incidents is not None or args.judge_only):
        parser.error("one of the arguments --all --incidents is required (or --judge-only)")
    run = args.run or (args.llm or DEFAULT_LIVE_RUN)
    run_dir: Path = args.results_dir / run

    known = list_incident_ids()
    ids: list[str] = args.incidents if args.incidents is not None else known
    unknown = [i for i in ids if i not in known]
    if unknown or not ids:
        print(f"error: unknown incident(s) {unknown} (known: {', '.join(known)})", file=sys.stderr)
        return EXIT_UNKNOWN_INCIDENT
    if args.judge_only and not any(result_path(run_dir, i, s).exists() for i in ids for s in args.systems):
        print(f"error: --judge-only found no results to judge in {run_dir}", file=sys.stderr)
        return EXIT_UNKNOWN_INCIDENT

    try:
        llm = build_llm(args.llm)
        llm.preflight()
    except (LLMUnavailableError, ModelsConfigError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return EXIT_PREFLIGHT

    outcome = run_eval(
        llm,
        run_dir,
        incident_ids=ids,
        systems=args.systems,
        llm_label=args.llm or MODELS_TOML_LABEL,
        judge=not args.no_judge,
        run_systems=not args.judge_only,
    )
    done = len(outcome.written) + len(outcome.skipped)
    judged = f"{len(outcome.judged)} judged ({len(outcome.judge_failed)} judge failures)"
    if outcome.auth_failed is not None:
        print(
            f"\nerror: stopped because the API rejected the key: {outcome.auth_failed}\n"
            f"No result was saved for the rejected pair; {done} pairs are done and {judged} in this invocation.\n"
            f"Fix ANTHROPIC_API_KEY, then resume with:\n\n    {_resume_command(args, run)}\n",
            file=sys.stderr,
        )
        return EXIT_PREFLIGHT
    if outcome.rate_limited is not None:
        stage = (
            ""
            if args.judge_only
            else f" with {done} of {len(ids) * len(args.systems)} pairs run ({len(outcome.written)} new)"
        )
        print(
            f"\nStopped by a rate limit{stage}; {judged} in this invocation. "
            f"Completed pairs and judge verdicts "
            f"are saved; the interrupted one was discarded.\n"
            f"Resume when the limit resets with:\n\n    {_resume_command(args, run)}\n",
            file=sys.stderr,
        )
        return EXIT_RATE_LIMITED
    print(
        f"\n{len(outcome.written)} pairs run, {len(outcome.skipped)} skipped, {judged}. Summary: {outcome.summary}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
