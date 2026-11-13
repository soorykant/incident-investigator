# 10: LLM judge for evidence quality

**What to build:** A second, qualitative layer of evaluation. An Opus judge with its own prompt receives the answer key, the report and the cited evidence. It scores evidence support (1–5), fact/assumption separation (1–5) and recommended-action quality (1–5), and says whether the system fell for the planted trap (yes/no). It never decides root-cause accuracy.

**Blocked by:** 09 (evaluation runner)

**Status:** done

Context: PRD D6, R5. `DECISIONS.md` Q17.

- [x] The judge output is schema-validated. Invalid output is retried, then recorded as a judge failure without breaking the run
- [x] Judge scores appear in the comparison summary next to the deterministic scores, per system
- [x] Judge tokens are reported separately and are not charged to either system
- [x] Judge results use the same save-as-you-go and resume behaviour as the evaluation runner
- [x] A test confirms that changing a judge score does not change the accuracy metric

## Comments

**2026-11-12, implementation:**
- **All boxes ticked.** The code is in `src/incident_investigator/evaluation/judge.py` (new), `runner.py` and `summary.py`. `llm/mock.py` now plays the `judge` role too. Tests: `tests/test_eval_judge.py` (20 tests: the judge function with scripted fake LLMs, and the runner and summary with the mock LLM and a tmp dir). One assertion in `tests/test_eval_runner.py` changed, because the judge now runs by default. 357 tests pass, and pyright is clean. The live judge was **not** run here; only the mock and scripted fakes were.
- **Commands:**
  - `uv run eval --all` runs both systems, then the judge, then writes `summary.md`. The judge is part of the default flow.
  - `uv run eval --all --no-judge` gives deterministic scores only.
  - `uv run eval --judge-only --run <name>` judges the results that run already has. It does not investigate anything. Add `--all` / `--incidents` / `--systems` to narrow it; without them, every incident and both systems. When the run has no result to judge, it exits 2.
  - Offline: `uv run eval --all --llm mock`. With `--llm <backend>`, the judge uses that backend too. Without `--llm`, the judge uses the `judge` role in `models.toml` (Opus 5.5 on `claude_cli`).
  - Resume: run the same command again. On a rate limit or usage cap, during investigation or during judging, the runner prints the exact command (it keeps `--judge-only` / `--no-judge`) and exits 4.
- **Judge prompt:** `JUDGE_SYSTEM` in `evaluation/judge.py` (it starts with `Role: judge`). `build_prompt` sends four parts: the incident report, the answer key (including the trap descriptions and the misleading IDs), the report under review (without `metrics`), and every cited ID resolved to its scenario row (loaded with `load_scenario`). The cited IDs are the same set as the grounding citations, including the IDs the evidence rules removed. An ID that is not in the scenario shows `"NOT IN SCENARIO"`.
- **Output schema:** `JudgeVerdict` (`extra=forbid`): `evidence_support`, `fact_assumption_separation` and `recommended_actions`, each `{score: 1-5, rationale}`, and `fell_for_trap: {answer: "yes"|"no", rationale}`. Fenced JSON is accepted.
- **Retry policy:** `MAX_JUDGE_ATTEMPTS = 2`, so one retry. The retry prompt repeats the input and names the validation error. An invalid reply and an ordinary `LLMError` (for example a timeout) are both failed attempts. After 2 failed attempts the pair gets `judge = {status: "failed", verdict: null, error, ...}` and the run continues. `LLMRateLimitError` is not retried. It propagates and stops the run cleanly.
- **Storage:** the verdict goes in the pair's own result file, in `EvalResult.judge` (the `JudgeScores` model; the field name is the same as in 09). `JudgeScores` = `status`, `verdict`, `error`, `model`, `attempts`, `llm_calls`, `usage` (tokens in/out/cache), `latency_s`, `judged_at`. The judge pass runs after all investigations. For each pair with a result file and `judge is None`, it rewrites the file atomically (`write_atomically`). A re-run skips pairs that are already judged. A judge call cut off midway leaves the file unchanged (`judge` stays null). **A recorded judge failure counts as judged**, so a re-run does not retry it. To retry it, set `judge` back to `null` in the file, then run `--judge-only`.
- **Cost separation (D6):** the judge's tokens live only in `judge.usage`. `report.metrics` and the summary's system cost columns do not change (a test compares a judged run with a `--no-judge` run). The summary has a separate "Judge cost (not charged to either system)" table.
- **Summary:** the aggregate Judge column shows `ES · FA · RA` means, `trap yes/judged`, and the failure and pending counts. The per-incident Judge column shows each verdict or `judge failed`. The "Judge scores" section has a table per system (judged of pairs, three means, fell-for-trap count, judge failures), the judge cost table and the R5 note.
- **Accuracy is safe:** the judge never writes `scores`, and nothing reads the judge back into `scores`. A test runs the same incidents with all-5 and all-1 judges and checks that `scores` and the Accuracy column are the same.
- **Mock judge:** it is deterministic and does not depend on the incident. Evidence support = 4 minus the number of `NOT IN SCENARIO` IDs (minimum 1). Separation is always 3. Actions = 3 when there are actions, else 1. `fell_for_trap` = yes when the top hypothesis's `supporting_ids` include a misleading ID listed in the prompt. Mock run (20 pairs): ES 4.00, FA 3.00, RA 3.00 and trap 8/10 for both systems. Judge cost was 10 calls per system.
- **Known bias (R5):** the judge is a Claude model, like the systems it grades, and may favour their style. This is why it never decides accuracy. The summary says so too. The judge also sees the answer key, so its scores can lean toward reports that happen to match the key.
- **Open:** with `--judge-only` and no selection, "Pairs complete" in the summary counts against all incidents × the selected systems, so a partial run shows `x of 20`. The judge runs one pair at a time, without parallelism.

**2026-11-13, API key only (ticket 14):**
- Without `--llm`, the judge now uses the `judge` role in `models.toml`: Opus 5.5 on `anthropic_api`, billed to `ANTHROPIC_API_KEY`. The run stops and prints the resume command on an API rate limit (429).
