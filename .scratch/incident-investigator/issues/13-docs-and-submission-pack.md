# 13: Documentation and submission pack

**What to build:** Everything a reviewer needs to run, understand and judge the work: setup instructions, an architecture diagram with an explanation of how the agents coordinate, the evaluation results, a technical note, and the coding-assistant transcript. Submitting is left to the user.

**Blocked by:** 09 (evaluation runner), 10 (LLM judge), 11 (HTTP API)

**Status:** ready-for-human

Context: brief §4 deliverables. PRD responsibility split, R1–R6, Q2. `DECISIONS.md` Q22, Q23.

- [x] The README covers setup (uv, CLI login), the CLI and API usage, and how to run tests and the evaluation. Following it on a clean checkout works
- [x] A Mermaid architecture diagram shows the agents, the blackboard, the task DAG, the Critic loop and the tools. It comes with a short explanation of how the agents coordinate
- [ ] The evaluation results from ticket 09 and ticket 10 are included
- [x] The technical note is 500 words or fewer (checked by word count) and covers key decisions, trade-offs, limitations (CLI overhead, a single run per incident, the judge favouring its own model family) and possible improvements
- [x] Unfinished features and external dependencies are listed
- [x] The transcript folder holds the session log and its readable version. The user is flagged to review it for private content before submitting
- [x] All work is committed locally. Nothing is pushed

## Comments

**2026-11-12, implementation (docs and submission pack):**
- **Files:** `README.md` (replaces the stub), `docs/technical-note.md` (new), `tests/test_docs.py` (new: keeps the note at 500 words or fewer, counting words as `wc -w` does after removing HTML comments), `.gitignore` (adds `eval/results/*` and `!eval/results/live/`). No source code changed.
- **Technical note:** 475 words without the draft comment, 493 by plain `wc -w` with it. A one-line HTML comment at the top marks it as a draft for the user to edit and sign off.
- **Mermaid diagram:** checked with `mermaid.parse` (mermaid 11.4.1 under jsdom). It parses as `flowchart-v2`. A broken control diagram fails, so the check is real.
- **Commands verified on this checkout** (all offline, nothing ran against the real Claude CLI): `uv sync`; `uv run investigate inc01 --mode multi --llm mock` and `--mode single --llm mock` (exit 0, report and trace written); `uv run investigate inc99 --llm mock` (exit 2); `uv run eval --all --llm mock --results-dir <tmp>` (20 pairs plus 20 judge verdicts, `summary.md`); the same command again (20 skipped, so resume works); `--incidents inc01,inc09 --systems single --no-judge --run nojudge`, then `--judge-only --run nojudge --systems single` (2 judged); `uv run python -m incident_investigator.evaluation.runner --help`; `uv run api --port 8765` (`/docs` 200, `/incidents` lists 10, unknown incident 404, extra body field 422, unknown investigation 404; no POST that would start a real run); the POST then GET flow in-process with `create_app(llm_factory=MockLLM)` (202, then `done` / ROOT_CAUSE); `uv run pytest` (359 passed); `uv run pyright` (0 errors).
- **Box 1 ("works on a clean checkout"):** ticked on the evidence above. The live paths (`claude_cli` runs, `uv run api` POSTs, `uv run eval --all` without `--llm`) were not run here, because they use the subscription.
- **Box 3 (evaluation results) is NOT ticked.** The live evaluation has not been run. The README's "Evaluation results" section explains every metric, gives the command, names `eval/results/live/summary.md`, and has a marked placeholder for the live tables. The only numbers in it are an offline mock smoke test, labelled as meaningless for accuracy. No live number was invented.
- **`eval/results/`:** the live run is a deliverable, so `.gitignore` keeps `eval/results/live/` and ignores every other run folder (mock, scratch). `runs/` (CLI and API output) stays ignored. If the live run uses another `--run` name, add a `!eval/results/<name>/` line or rename the folder to `live`.
- **Mock run note:** today's mock run gives multi 2/10 and single 1/10, with 8/5 tool calls and 17/7 LLM calls per incident, the same as ticket 09's comment.
- **Transcript:** `docs/transcript/` holds three sessions (`5ef28d2e…`, `941b5c47…`, `c7efce86…`), each as `.jsonl` (raw session log) and `.md` (readable: messages, tool calls, tool results cut at 3,000 characters, no thinking). The Stop hook `.claude/hooks/export_transcript.py` rewrites them after every turn in the main checkout. The copies in git are snapshots from an earlier commit, so commit the final versions from the main checkout after the last session. No transcript file was edited here.

**Privacy checks for the user before submitting the transcript** (found by a quick grep; not exhaustive):
- Your account email address appears in the `.jsonl` files, in the session context that Claude Code records.
- Your home folder path, which includes your macOS user name, appears hundreds of times in the `.jsonl` files and dozens of times in the `.md` files.
- The `.jsonl` files list the names of your connected integrations and MCP tools (for example your company's platform tools, Slack, Gmail, Linear), and the company name appears in them.
- The `.jsonl` files keep the assistant's thinking blocks; the `.md` files leave them out.
- Check any pasted text, file contents or command output you would not want shared, including anything from other projects. Decide whether to submit only the `.md` files, or to redact the `.jsonl` files.

**User's remaining steps:**
1. Export `ANTHROPIC_API_KEY`, check one run with `uv run investigate inc01`, then run the live evaluation: `uv run eval --all` (billed to your API key; if it stops at a rate limit, re-run the same command to resume).
2. Paste the aggregate, totals and judge tables from `eval/results/live/summary.md` into the README placeholder (or link the file), then commit `eval/results/live/`. Then tick box 3.
3. Edit and sign off `docs/technical-note.md`: update the "live evaluation pending" line and add one or two findings from the real results. Remove the draft comment. `uv run pytest tests/test_docs.py` keeps it at 500 words or fewer.
4. Review `docs/transcript/` for private content (list above) and commit its final state.
5. Review the answer keys and traps for realism (inc07 and inc10 especially).
6. Choose how to submit (GitHub repo or zip, Q2) and do it. Nothing has been pushed.

**2026-11-13, API key only (ticket 14):**
- README, technical note and diagram now describe API-key-only access (ADR 0002). The user's step 1 above was updated: the evaluation is billed to the API key, not the subscription.
- The live paths noted above (`uv run api` POSTs, `uv run eval --all` without `--llm`) now need `ANTHROPIC_API_KEY`. They still have not been run against a real key.

**2026-11-13, live evaluation:**
- All 20 investigations ran on `anthropic_api` (code `cd5e31c`), sent one at a time through the running HTTP API, which held the key, and saved in the eval runner's result format in `eval/results/live/` with traces and `summary.md`.
- Accuracy: multi 8/10, single 9/10. Grounding: multi 0.997 (812/814), single 1.000. All 20 top hypotheses named the right service and category. Misses: multi inc07 and inc10 (right answer, confidence 0.40 and 0.50, under 0.6); single inc06 (final answer rejected twice for an extra JSON field in an assumption).
- The README results section now has the aggregate table and the misses. Box 3 stays open until the judge runs: `uv run eval --judge-only --run live` needs `ANTHROPIC_API_KEY` in the shell.
