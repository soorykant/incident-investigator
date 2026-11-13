# 09: Evaluation runner with deterministic scoring

**What to build:** One command runs both systems on all 10 incidents and produces a comparison a reviewer can trust. Scoring is deterministic. Each (incident, system) result is saved as soon as it finishes, so a run stopped by the subscription's usage cap resumes where it left off.

**Blocked by:** 06 (Critic loop and limits), 07 (single-agent baseline), 08 (full dataset)

**Status:** done

Context: PRD L24, L25, step 10, Q3. `DECISIONS.md` Q12.

- [x] Accuracy: the top Hypothesis matches both the answer key's service and its category. Inconclusive-by-design incidents score correct only when the outcome is INCONCLUSIVE
- [x] Grounding: the share of cited evidence IDs that exist in the scenario
- [x] Cost: tool calls, LLM calls, input/output/cache tokens (shown separately) and latency for each pair
- [x] Each pair's result is written to disk when it completes. Re-running skips existing results. A pair cut off midway is discarded, not saved
- [x] On a rate-limit or usage-cap error, the runner stops cleanly and prints the resume command
- [x] A summary table compares both systems per incident and in aggregate. It notes that each system ran once per incident and that some CLI overhead remains
- [x] Scoring logic is unit-tested with fixed reports (no LLM)

## Comments

**2026-11-12, implementation:**
- **All boxes ticked.** The code is in `src/incident_investigator/evaluation/`: `scoring.py` (pure functions), `runner.py` (`run_eval`, `EvalResult`, the `eval` CLI) and `summary.py` (renders `summary.md`). Tests: `tests/test_eval_scoring.py` (17 tests on hand-built reports and keys), `tests/test_eval_runner.py` (9 tests with the mock LLM and a tmp dir), plus rate-limit tests in `tests/test_llm.py` and `tests/test_investigation.py`. 337 tests pass, and pyright is clean. The live evaluation was not run here.
- **Commands:**
  - Live run with the per-role models from `models.toml`: `uv run eval --all`. The run name defaults to `live`, so results go to `eval/results/live/`.
  - Resume after a usage cap or a kill: run the **same** command again (`uv run eval --all`, plus `--run <name>` if you named the run). On a rate limit, the runner prints the exact command, for example `uv run eval --all --run live`, and exits 4.
  - Offline: `uv run eval --all --llm mock` (run name `mock`). Subsets: `--incidents inc01,inc02` and `--systems single`. Other flags: `--results-dir` (default `eval/results`) and `--run`.
  - Exit codes: 0 means done. 2 means an unknown incident. 3 means the preflight failed (CLI missing or not logged in). 4 means a rate limit or usage cap stopped the run. Any other crash propagates after the completed pairs are saved.
- **Result file** `eval/results/<run>/<incident>__<system>.json` (`EvalResult`, `extra=forbid`): `{schema_version: 1, run, incident_id, system: multi|single, llm: "models.toml"|<backend>, finished_at, report: InvestigationReport, scores: {expected_outcome, expected_service, expected_category, correct, accuracy_reason, citations, grounded, grounding, unknown_ids}, judge: null}`. Cost is in `report.metrics` (tool_calls, llm_calls, tokens_in, tokens_out, cache_tokens, latency_s). Next to it, `<incident>__<system>.trace.json` holds the full blackboard, including every evidence row the tools returned. Both files are written atomically (temp file, then `os.replace`). The trace is written first and the result last, so the result file is the checkpoint. A pair cut off midway leaves no result file and no temp file.
- **For ticket 10 (judge):** replace `EvalResult.judge: dict[str, Any] | None` with a `JudgeScores` model, and keep the field name. Run the judge as its own pass, not inside the runner: for each result file with `judge is None`, read the report, the cited evidence from the trace's `evidence`, and `load_answer_key` (allowed inside `evaluation/`). Call the `judge` role, rewrite the file with `write_atomically`, and re-render the summary with `render_summary(load_results(run_dir), ...)`. Judging then resumes the same way. `summary.py` already has a Judge column (aggregate and per incident) and a "Judge scores" section that says "pending (ticket 10)". `_judge_cell` prints the judge dict today. The judge must never change `scores.correct`.
- **Scoring definitions:**
  - Accuracy: `expected_outcome` maps case-insensitively to `ROOT_CAUSE` or `INCONCLUSIVE`. An inconclusive-by-design incident is correct only when the outcome is INCONCLUSIVE. A root-cause incident is correct only when the outcome is ROOT_CAUSE and the top-ranked hypothesis matches the service and the category (case and surrounding spaces ignored). `accuracy_reason` is `match`, `outcome_mismatch`, `no_hypothesis`, `wrong_service`, `wrong_category` or `wrong_service_and_category`. There is no partial credit for the caller service (inc07, inc10).
  - Grounding: the citations are the Facts' `evidence_ids` and the ranked hypotheses' supporting and contradicting IDs, counted once per place they appear, **plus the IDs the evidence rules removed** (`fact_demoted` and `hypothesis_ids_dropped` adjustments). Without these, every report would be 100% grounded, because the rules strip unknown IDs before the report is built. grounding = grounded / citations, rounded to 3 decimals. **Zero citations gives `grounding = null`** (shown as "n/a"), and the mean skips it.
- **Rate limit (L25):** `LLMRateLimitError(LLMError)` is in `llm/base.py`. `claude_cli.py` raises it for a failed call (an error result, a non-zero exit or no JSON) when the result text or stderr matches, case-insensitively: `rate limit` / `rate_limit` / `rate-limit`, `usage limit`, `hit your limit`, `<n>-hour limit`, `session limit`, `weekly limit`, `overloaded`, or a standalone `429` or `529`. A successful reply that mentions 429 is not an error. `agents/loop.py`, `agents/ask.py` and the crash guard of `run_task` in `graph.py` re-raise it, so it leaves `investigate()` from every role in both modes. Other `LLMError`s are still ordinary failed attempts.
- **Isolation test:** `ALLOWED` now holds `dataset.py` and `evaluation/*.py`, for the answer-key mention and import checks only. The data-path check and both step-11 checks (incident IDs and answer-key services) still scan `evaluation/`. A new check fails if any module outside `evaluation/` imports it.
- **Mock run** (`--llm mock`, 20 of 20 pairs, about 20 s; the mock's fixed categories make accuracy meaningless): multi 2/10 correct, grounding 1.000, and per incident 8 tool calls, 17 LLM calls, 16,138 tokens in, 1,211 out, 0 cache. Single 1/10, grounding 1.000, 5 tool calls, 7 LLM calls, 9,171 in, 586 out, 0 cache. A second run skipped all 20 pairs.
- **Merged main** (ticket 11) into this branch with no conflicts. `[project.scripts]` keeps `api` and adds `eval`.
- **Open:** `anthropic_api` and `openai_compat` do not raise `LLMRateLimitError` yet; only `claude_cli`, the published backend, does. `overloaded` / 529 stops the run instead of retrying. The CLI's wording for the cap can change; all the markers are in one regex in `claude_cli.py`.

**2026-11-13, API key only (ticket 14):**
- The open item above is partly resolved: `anthropic_api`, now the published backend, raises `LLMRateLimitError` on a 429, and a 529 overload is retried instead of stopping the run. `openai_compat` still treats a 429 as an ordinary failure.
- Exit code 3 now means the preflight failed because `ANTHROPIC_API_KEY` is not set (or a local server is unreachable). Exit code 4 means an API rate limit; there are no subscription usage caps any more.
- The summary's "CLI overhead" note was removed.
