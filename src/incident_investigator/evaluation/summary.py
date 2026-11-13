"""`summary.md`: both systems side by side, in aggregate and per incident (ticket 09, step 10)."""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from statistics import mean
from typing import TYPE_CHECKING

from incident_investigator.evaluation.judge import JudgeVerdict

if TYPE_CHECKING:
    from incident_investigator.evaluation.runner import EvalResult

SUMMARY_FILE = "summary.md"
JUDGE_PENDING = "pending"


def _fmt(value: float | None, digits: int = 1) -> str:
    return "n/a" if value is None else f"{value:,.{digits}f}"


def _judge_cell(result: EvalResult) -> str:
    if result.judge is None:
        return "—"
    v = result.judge.verdict
    if v is None:
        return "judge failed"
    return (
        f"ES {v.evidence_support.score} · FA {v.fact_assumption_separation.score} "
        f"· RA {v.recommended_actions.score} · trap {v.fell_for_trap.answer}"
    )


@dataclass
class _JudgeStats:
    pairs: int
    verdicts: list[JudgeVerdict]
    failed: int
    pending: int

    @classmethod
    def of(cls, results: Sequence[EvalResult]) -> _JudgeStats:
        verdicts = [r.judge.verdict for r in results if r.judge is not None and r.judge.verdict is not None]
        failed = sum(1 for r in results if r.judge is not None and r.judge.verdict is None)
        return cls(len(results), verdicts, failed, sum(1 for r in results if r.judge is None))

    def mean(self, pick: Callable[[JudgeVerdict], int]) -> float | None:
        return mean(pick(v) for v in self.verdicts) if self.verdicts else None

    @property
    def trap_falls(self) -> int:
        return sum(1 for v in self.verdicts if v.fell_for_trap.answer == "yes")


def _evidence(v: JudgeVerdict) -> int:
    return v.evidence_support.score


def _separation(v: JudgeVerdict) -> int:
    return v.fact_assumption_separation.score


def _actions(v: JudgeVerdict) -> int:
    return v.recommended_actions.score


def _judge_aggregate_cell(results: Sequence[EvalResult]) -> str:
    stats = _JudgeStats.of(results)
    if not stats.verdicts and not stats.failed:
        return JUDGE_PENDING
    parts = [
        f"ES {_fmt(stats.mean(_evidence), 2)} · FA {_fmt(stats.mean(_separation), 2)} "
        f"· RA {_fmt(stats.mean(_actions), 2)} · trap {stats.trap_falls}/{len(stats.verdicts)}"
    ]
    if stats.failed:
        parts.append(f"{stats.failed} failed")
    if stats.pending:
        parts.append(f"{stats.pending} {JUDGE_PENDING}")
    return " · ".join(parts)


def _judge_rows(results: Sequence[EvalResult], systems: Sequence[str]) -> list[str]:
    rows = [
        "| System | Judged | Evidence support | Fact/assumption separation | Actions | Fell for trap "
        "| Judge failures |",
        "|---|---|---|---|---|---|---|",
    ]
    for system in systems:
        stats = _JudgeStats.of([r for r in results if r.system == system])
        trap = f"{stats.trap_falls}/{len(stats.verdicts)}" if stats.verdicts else "n/a"
        rows.append(
            f"| {system} | {len(stats.verdicts)} of {stats.pairs} | {_fmt(stats.mean(_evidence), 2)} "
            f"| {_fmt(stats.mean(_separation), 2)} | {_fmt(stats.mean(_actions), 2)} | {trap} | {stats.failed} |"
        )
    return rows


def _judge_cost_rows(results: Sequence[EvalResult], systems: Sequence[str]) -> list[str]:
    rows = [
        "| Results judged for | LLM calls | Tokens in | Tokens out | Cache tokens | Latency s |",
        "|---|---|---|---|---|---|",
    ]
    for system in systems:
        judged = [r.judge for r in results if r.system == system and r.judge is not None]
        rows.append(
            f"| {system} | {sum(j.llm_calls for j in judged):,} | {sum(j.usage.tokens_in for j in judged):,} "
            f"| {sum(j.usage.tokens_out for j in judged):,} | {sum(j.usage.cache_tokens for j in judged):,} "
            f"| {_fmt(sum(j.latency_s for j in judged))} |"
        )
    return rows


def _judge_section(results: Sequence[EvalResult], systems: Sequence[str], run: str) -> list[str]:
    pending = sum(1 for r in results if r.judge is None)
    lines = [
        "An LLM judge (the `judge` role in models.toml, its own prompt) read each report with the answer key "
        "and every cited evidence row. It scores evidence support, fact/assumption separation and "
        "recommended actions from 1 to 5 (means over the pairs it judged), and says whether the system fell "
        "for a planted trap (count of yes over judged pairs). **The judge never decides accuracy**: accuracy "
        "above is deterministic, so a judge mistake cannot move it. A judge failure is a pair whose judge "
        "output stayed invalid after one retry; it is counted, not scored.",
        "",
        *_judge_rows(results, systems),
        "",
        "### Judge cost (not charged to either system)",
        "",
        "The judge's own LLM calls, tokens and latency (D6). They are not in any of the system metrics above.",
        "",
        *_judge_cost_rows(results, systems),
        "",
        "- **Bias (R5):** the judge is a Claude model, like the systems it grades, and may favour their "
        "style. Read its scores as a second opinion on reasoning quality, not as ground truth.",
    ]
    if pending:
        lines += [
            f"- {pending} results are not judged yet. Judge them without re-investigating: "
            f"`uv run eval --judge-only --run {run}`.",
        ]
    return lines


def _aggregate_rows(results: Sequence[EvalResult], systems: Sequence[str]) -> list[str]:
    rows = [
        "| System | Pairs | Accuracy | Grounding (mean) | Citations (grounded/total) | Tool calls | LLM calls "
        "| Tokens in | Tokens out | Cache tokens | Latency s | Judge |",
        "|---|---|---|---|---|---|---|---|---|---|---|---|",
    ]
    for system in systems:
        mine = [r for r in results if r.system == system]
        if not mine:
            rows.append(f"| {system} | 0 | n/a | n/a | n/a | n/a | n/a | n/a | n/a | n/a | n/a | {JUDGE_PENDING} |")
            continue
        correct = sum(r.scores.correct for r in mine)
        groundings = [r.scores.grounding for r in mine if r.scores.grounding is not None]
        grounded = sum(r.scores.grounded for r in mine)
        citations = sum(r.scores.citations for r in mine)
        metrics = [r.report.metrics for r in mine]
        judge = _judge_aggregate_cell(mine)
        rows.append(
            f"| {system} | {len(mine)} | {correct}/{len(mine)} ({100 * correct / len(mine):.0f}%) "
            f"| {_fmt(mean(groundings) if groundings else None, 3)} | {grounded}/{citations} "
            f"| {_fmt(mean(m.tool_calls for m in metrics))} | {_fmt(mean(m.llm_calls for m in metrics))} "
            f"| {_fmt(mean(m.tokens_in for m in metrics), 0)} | {_fmt(mean(m.tokens_out for m in metrics), 0)} "
            f"| {_fmt(mean(m.cache_tokens for m in metrics), 0)} | {_fmt(mean(m.latency_s for m in metrics))} "
            f"| {judge} |"
        )
    return rows


def _totals_rows(results: Sequence[EvalResult], systems: Sequence[str]) -> list[str]:
    rows = [
        "| System | Tool calls | LLM calls | Tokens in | Tokens out | Cache tokens | Latency s |",
        "|---|---|---|---|---|---|---|",
    ]
    for system in systems:
        metrics = [r.report.metrics for r in results if r.system == system]
        rows.append(
            f"| {system} | {sum(m.tool_calls for m in metrics):,} | {sum(m.llm_calls for m in metrics):,} "
            f"| {sum(m.tokens_in for m in metrics):,} | {sum(m.tokens_out for m in metrics):,} "
            f"| {sum(m.cache_tokens for m in metrics):,} | {_fmt(sum(m.latency_s for m in metrics))} |"
        )
    return rows


def _per_incident_rows(results: Sequence[EvalResult]) -> list[str]:
    rows = [
        "| Incident | System | Expected | Outcome | Top hypothesis | Correct | Grounding | Tool calls "
        "| LLM calls | Tokens in | Tokens out | Cache tokens | Latency s | Ended by | Judge |",
        "|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|",
    ]
    for r in sorted(results, key=lambda r: (r.incident_id, r.system)):
        s, m = r.scores, r.report.metrics
        expected = s.expected_outcome if s.expected_service is None else f"{s.expected_service} / {s.expected_category}"
        top = r.report.ranked_hypotheses[0] if r.report.ranked_hypotheses else None
        top_text = f"{top.service} / {top.category}" if top is not None else "—"
        correct = "yes" if s.correct else f"no ({s.accuracy_reason})"
        rows.append(
            f"| {r.incident_id} | {r.system} | {expected} | {r.report.outcome} | {top_text} | {correct} "
            f"| {_fmt(s.grounding, 3)} ({s.grounded}/{s.citations}) | {m.tool_calls} | {m.llm_calls} "
            f"| {m.tokens_in:,} | {m.tokens_out:,} | {m.cache_tokens:,} | {_fmt(m.latency_s)} "
            f"| {r.report.terminated_by} | {_judge_cell(r)} |"
        )
    return rows


def render_summary(
    results: Sequence[EvalResult], *, run: str, expected_pairs: Sequence[tuple[str, str]]
) -> str:
    """Markdown comparing the systems. `expected_pairs` are the (incident, system) pairs
    this invocation asked for; the ones without a result are listed as missing."""
    systems = list(dict.fromkeys([s for _, s in expected_pairs] + [r.system for r in results]))
    have = {(r.incident_id, r.system) for r in results}
    missing = [f"{i} {s}" for i, s in expected_pairs if (i, s) not in have]
    backends = sorted({r.llm for r in results}) or ["n/a"]
    done = sum(1 for p in expected_pairs if p in have)
    lines = [
        f"# Evaluation summary: run `{run}`",
        "",
        f"- Generated: {datetime.now(UTC).strftime('%Y-%m-%d %H:%M UTC')}",
        f"- LLM: {', '.join(backends)}",
        f"- Pairs complete: {done} of {len(expected_pairs)}"
        + (f" (missing: {', '.join(missing)})" if missing else ""),
        "",
        "## Aggregate (mean per incident unless noted)",
        "",
        *_aggregate_rows(results, systems),
        "",
        "## Totals",
        "",
        *_totals_rows(results, systems),
        "",
        "## Per incident",
        "",
        *_per_incident_rows(results),
        "",
        "## Judge scores",
        "",
        *_judge_section(results, systems, run),
        "",
        "## How to read this",
        "",
        "- **Each system ran once per incident** (Q11). There are no repeated trials, so a "
        "single-incident difference is one sample, not a trend.",
        "- **Accuracy:** the top-ranked hypothesis matches the answer key's service and category, "
        "with outcome ROOT_CAUSE. Inconclusive-by-design incidents are correct only when the outcome "
        "is INCONCLUSIVE; a root-cause incident answered INCONCLUSIVE is incorrect.",
        "- **Grounding:** the share of cited evidence IDs (Facts and hypothesis supporting/contradicting "
        "IDs, plus the IDs the evidence rules removed from the report) that exist in the scenario. "
        "n/a means the report cited nothing; the mean skips it.",
        "- **Tokens:** input (uncached input + cache writes), output and cache reads are shown separately "
        "(D5). The token budget excludes cache reads.",
        "",
    ]
    return "\n".join(lines)
