# Multi-Agent Production Incident Investigator

Sources: `orange_invest_assignment.md` (the brief) and `DECISIONS.md` (Q1–Q25, the decision record).
This PRD adds the interview of 2026-11-12 (L24–L26) and recommendations still awaiting sign-off (D#).

> **Change, 2026-11-13 (DECISIONS.md Q25, ADR 0002):** Claude is called only through the Anthropic API with `ANTHROPIC_API_KEY`. The subscription route (`claude -p`, the `claude_cli` backend) was removed. Items that applied only to that route (Q1, Q3, D4, R1, R2) are marked superseded below; the rest of the PRD is updated to match.

## TL;DR

A Python CLI and API that investigates simulated production incidents with six cooperating agents. The agents share a typed blackboard and a task DAG, and a Critic can send the investigation back for more evidence. The system returns ranked, evidence-cited hypotheses, or an honest `INCONCLUSIVE`.

The thesis is to reuse LangGraph for the control flow and the Anthropic API for the LLM, and to build only the investigation logic, the simulated data, and a fair evaluation against a single-agent baseline.

The audience is the reviewer of the AI Engineer assignment. The budget is one overnight build (Q3).

## Locked assumptions

- **Language/runtime:** Python 3.12, managed with `uv` (Q1).
- **Framework:** LangGraph (Q14). FastAPI for the API (Q19). Pydantic v2 for all schemas. *Exact package versions are pinned in `uv.lock` at scaffold time (D9).*
- **LLM access:** Claude through the Anthropic Messages API, paid by `ANTHROPIC_API_KEY` (Q25, which replaces Q13 and the subscription parts of Q15).
- **Models:** Opus 5.5 for plan creation, Reasoning, Critic, the baseline and the judge. Haiku 5.5 for Observability, Dependency and Reporting (Q8).
- **Deployment:** local only, on the developer's macOS machine. No hosting, no database, no remote git (Q19, Q22).
- **Data:** 10 hand-written synthetic incidents. No real production data anywhere (Q5, Q18).

## Goals

- Investigate an incident with ≥ 5 distinct, genuinely collaborating agents. The design uses 6.
- Use tools with structured I/O, simulated failures, bounded retries and recovery.
- Produce a report with ranked hypotheses, supporting and contradicting evidence, confidence, actions, a fact/assumption split, and an inconclusive outcome when evidence is too weak.
- Compare the multi-agent system with a **fair** single-agent baseline on accuracy, grounding, tool calls, latency and tokens.
- Ship all 5 brief deliverables, including the coding-assistant transcript.

## Non-goals (v1), with reasons

*Reasons come from DECISIONS.md where it states them. Otherwise they are the recommended reason (D10). Please confirm.*

- **No frontend UI.** The brief says no polished frontend is required, and the budget is one overnight build (Q3).
- **No vector database or embeddings for past incidents.** Keyword or tag search is enough for 10 incidents and avoids extra infrastructure (Q3).
- **No database.** Results are kept in memory plus JSON files, which is enough for a single-user local tool (Q19).
- **No LLM-generated incident data.** It would make the answer keys unreliable and the results impossible to reproduce (Q5).
- **No remote git push or hosting.** How to submit is decided later by the user (Q22).
- **No auto-remediation.** Actions are recommendations only. The system never changes any real system, because the data is simulated and the brief asks only for recommendations.
- **No LLM judge on root-cause accuracy.** Accuracy stays deterministic, so a judge mistake can't move the main metric (Q17).
- **No multiple runs per incident or statistical significance.** Each system runs once per incident because of the time budget (Q11). This is a documented limitation.
- **No published local-model evaluation.** The `openai_compat` backend is built, but the published numbers use Claude only (Q20).

## Capability map (build-source tagged)

*Tags are recommendations drafted from DECISIONS.md (D1). Please confirm.*

| Capability | Tag | Note |
|---|---|---|
| Graph runtime, typed state, parallel fan-out (`Send`), conditional edges, `recursion_limit` | `[core]` | LangGraph as-is |
| LLM access via API key | `[core]` | Official `anthropic` Python SDK (version pinned in `uv.lock`) |
| API server + Swagger at `/docs` | `[core]` | FastAPI as-is |
| Schema validation | `[core]` | Pydantic v2 |
| `LLMClient` interface + backends (`anthropic_api` default, `openai_compat`, offline `mock`) | `[build]` | New. Per-agent choice in `models.toml` (Q20, Q25). At most 3 concurrent LLM calls. |
| Synthetic incident dataset (10) + answer keys | `[build]` | New. JSON scenarios with 2–3 traps each (Q18). Keys in a separate folder that agents never read. |
| 5 tools: logs, metrics, deploys, dependencies, past incidents | `[build]` | New. Structured in/out, stable evidence IDs, failures injected on a fixed per-scenario schedule (Q5, Q21) |
| Blackboard state + task DAG | `[build]` | New. Built on LangGraph state (Q4) |
| Orchestrator (1 Opus planning call + Python DAG runner) | `[build]` | New (Q9) |
| Observability, Dependency, Reasoning, Critic, Reporting agents | `[build]` | New. JSON actions (`call_tool` / `finish`) validated by Pydantic (Q15) |
| Confidence capping + fact validation | `[build]` | New. Deterministic code, not the LLM (Q21) |
| Single-agent baseline | `[build]` | New. Same tools, limits and schema as multi-agent (Q6) |
| Evaluation runner with checkpoint/resume | `[build]` | New (L25) |
| Deterministic scorer + Opus judge | `[build]` | New (Q12, Q17) |
| CLI + API, both calling one `investigate()` | `[build]` | New (Q19) |
| Transcript export after every turn | `[build]` → done | Already exists: `.claude/hooks/export_transcript.py` + `.claude/settings.json` (Q23) |
| README, Mermaid diagram, technical note (≤ 500 words) | `[build]` | New docs |

## Data model sketch

*Draft for confirmation (D2). Field names are recommendations.*

- **Scenario** `data/incidents/incNN.json`: `{id, report: {title, symptoms, affected_service, window_start, window_end, severity}, logs[], metrics[], deploys[], config_changes[], dependencies: {service: [downstream]}, past_incidents[], failure_schedule[]}`
- **FailureSpec**: `{tool, call_index, mode: timeout|partial|error, partial_fraction?}`. It is deterministic: the Nth call to a tool fails in the same way on every run.
- **AnswerKey** `data/answer_keys/incNN.json`: `{incident_id, expected_outcome: root_cause|inconclusive, service, category: bad_deploy|config_change|dependency_failure|capacity|cert_dns|db_migration|retry_storm|feature_flag|third_party_limit, key_evidence_ids[], traps: [{description, misleading_evidence_ids[]}]}`
- **Evidence** (a tool result row): `{id: "<kind>:<incident>:<row>", kind, timestamp, payload}`
- **ToolResult**: `{status: ok|partial|timeout|error, evidence[], error?, attempts}`
- **Task**: `{id, agent, goal, params, depends_on[], status: pending|running|done|failed, origin: plan|critic, loop}`
- **Finding**: `{task_id, agent, facts: [{statement, evidence_ids[]}], assumptions: [{statement, basis, how_to_verify}]}`
- **Hypothesis**: `{id, service, category, summary, supporting_ids[], contradicting_ids[], proposed_confidence, final_confidence}`
- **EvidenceGap**: `{tool, reason, affects_hypotheses[]}`. **AgentFailure**: `{agent, task_id, reason, attempts}`
- **Blackboard**: `{incident, tasks[], findings[], hypotheses[], gaps[], failures[], critic_loops, counters: {tool_calls, tokens_in, tokens_out, cache_tokens, llm_calls}, started_at}`
- **InvestigationReport**: `{incident_id, outcome: ROOT_CAUSE|INCONCLUSIVE, ranked_hypotheses[], facts[], assumptions[], recommended_actions: [{action, rationale, hypothesis_id}], gaps[], metrics: {tool_calls, llm_calls, tokens_in, tokens_out, cache_tokens, latency_s}, terminated_by: done|tool_budget|token_budget|timeout|critic_limit}`
- **EvalResult** `eval/results/<run>/<incident>__<system>.json`: the report plus deterministic scores plus judge scores. One file per (incident, system) pair, which is also the resume checkpoint (L25).

## User journey: per-step success table

*Done signals are drafts (D3). Each is something an agent can check.*

| Step | Action | Done signal (agent-verifiable) |
|---|---|---|
| 1 | Load the dataset | `pytest tests/test_dataset.py` passes. All 10 scenarios and keys validate against the Pydantic schemas. Every `key_evidence_id` exists in its scenario. No agent prompt or code path under `agents/` reads `data/answer_keys/` (a test greps for it). |
| 2 | Call a tool | `pytest tests/test_tools.py` passes. Valid input returns `status=ok` with evidence IDs. **Invalid input** (unknown service, inverted time window) returns `status=error` and does not raise. A scheduled timeout returns `timeout` after 2 retries with backoff and records an `EvidenceGap`. A scheduled partial returns a subset marked `partial`. |
| 3 | `LLMClient` call | `pytest tests/test_llm_anthropic_api.py tests/test_agent_loop.py` (fake SDK client) passes. Valid JSON is parsed into an action. **Invalid JSON twice** produces an `AgentFailure`, and the run continues. The request sends the agent's system prompt as `system` with SDK retries off. Token fields are read from the API's `usage`. |
| 4 | Plan + run the DAG | With a mocked LLM, Observability and Dependency tasks with no dependency between them start concurrently (overlapping timestamps on the trace). A task with `depends_on` never starts before its dependency is done. |
| 5 | Critic loop | With a mocked Critic that always asks for more, the graph runs exactly 2 extra loops, then Reporting runs and the report shows `terminated_by=critic_limit`. A Critic follow-up task appears on the blackboard with `origin=critic`. |
| 6 | Limits | With a mocked agent that always calls tools, the run stops at 30 tool calls (`tool_budget`). A per-task cap of 6 holds. The 300k token budget and the 10-minute wall clock each end the run with the matching `terminated_by`, and a report is still produced. |
| 7 | Evidence rules | A Fact citing a non-existent ID is rejected (moved to assumptions or dropped, and logged). A hypothesis with no supporting Fact is capped at 0.5. Each open gap affecting it lowers it by 0.1. No hypothesis ≥ 0.6 gives `outcome=INCONCLUSIVE`. |
| 8 | Live investigation (CLI) | `uv run investigate inc01 --mode multi` and `--mode single` each write a schema-valid `InvestigationReport` JSON and exit 0. |
| 9 | API | `POST /investigations` returns `{id}`. Polling `GET /investigations/{id}` reaches `done` with the same report shape. **An unknown `incident_id` returns 404 and no run starts.** **An invalid body returns 422.** `GET /incidents` lists 10. `/docs` renders. |
| 10 | Evaluation | `uv run eval --all` writes 20 result files plus `eval/results/<run>/summary.md` comparing both systems on accuracy, grounding, tool calls, LLM calls, tokens (in/out/cache shown separately), latency and judge scores. **Killing it midway and re-running skips the files that already exist** (L25). |
| 11 | No hardcoding | A test greps `src/` for incident IDs and answer-key values and finds none outside the data loader. |
| 12 | Docs | The README has setup steps, a Mermaid architecture diagram and a coordination explanation. `docs/technical-note.md` is ≤ 500 words (`wc -w`). `docs/transcript/` holds the `.jsonl` and `.md` of this build session. |

## Non-functional constraints

- **Per investigation, for both systems (L26):** a 300,000-token budget (input plus output, excluding cache reads), a 10-minute wall clock, and 30 tool calls.
- **Per LLM call:** a 120 s timeout. A timeout counts as one failed attempt toward the agent-failure limit of 2.
- **Per agent task:** 6 tool calls. **Critic loops:** 2 (Q16).
- **Tool retries:** 2, with backoff. **LLM concurrency:** at most 3 calls in flight (Q20).
- **Token measurement (L24):** report input, output and cache tokens separately, from the API's `usage`.

## Error states and negative paths

- **Tool timeout or partial result:** 2 retries, then an `EvidenceGap`, which lowers confidence (Q16, Q21).
- **Agent returns invalid JSON or crashes twice:** an `AgentFailure`. The DAG continues without that agent's result. Reporting still runs.
- **Budget, time or loop limit hit:** stop, run Reporting with the current blackboard, and set `terminated_by`. *(Updated 2026-11-13, ticket 17: the tool budget stops only tool calls. An agent whose call is refused gets one finish-only turn, unstarted investigator tasks are skipped, and pending Reasoning still runs. The token budget and wall clock remain hard stops.)*
- **`ANTHROPIC_API_KEY` not set:** fail fast before the run with a clear message (exit 3). No partial report.
- **API rate limit, HTTP 429 (L25):** the eval runner stops cleanly after saving the completed pairs and prints the resume command. A pair cut off midway is discarded, not saved as a failure. A 529 overload is an ordinary failed attempt and is retried.
- **API:** unknown incident → 404. Invalid body → 422. Unknown investigation ID → 404. A run that crashes ends with `status=failed` and an error message, never a hang.

## Locked decisions

- **L1–L23:** Q1–Q23 in `DECISIONS.md`, as summarised in its "Summary of settled design" table. These are not repeated here.
- **L24:** Token metric. Input, output and cache tokens are reported separately, from the API's `usage`. *(Updated 2026-11-13: the CLI-overhead part no longer applies.)*
- **L25:** Rate limits. The eval saves each (incident, system) result to disk as it finishes. When a rate limit is hit, it stops cleanly, and re-running skips the pairs already done.
- **L26:** Run limits for both systems. 300k tokens and 10 minutes per investigation, with a 120 s timeout per LLM call.

## Decisions awaiting sign-off (D#)

| # | Recommendation | Rests on |
|---|---|---|
| D1 | The build-source tags in the capability map | DECISIONS.md summary |
| D2 | The data model sketch above | Glossary + Q5, Q12, Q16, Q21 |
| D3 | The per-step done signals above | The brief's requirements A–D + Q16 |
| D4 | *Superseded 2026-11-13 (ADR 0002): the CLI route was removed.* Run `claude -p` with `--setting-sources` limited to none of user/project/local (or equivalent), `--strict-mcp-config` with no servers, and `--no-session-persistence`, so that hooks, MCP servers and CLAUDE.md don't load per call | R2. Which flag combination works with subscription auth must be checked in Phase 2 (see Q1) |
| D5 | Define the token budget as input plus output, excluding cache reads | Cache reads are cheap repeats of an earlier prompt and would use up the budget unevenly |
| D6 | The judge's tokens are reported separately and not counted against either system | Q17: the judge measures, it doesn't investigate |
| D7 | Category list as in AnswerKey (9 categories mapped to the Q18 incidents) | Q12 + Q18 |
| D8 | Default `models.toml` maps agents to models per Q8, all on `anthropic_api` | Q8, Q25 |
| D9 | Pin LangGraph, FastAPI and Pydantic versions at scaffold time in `uv.lock`, and record them in the README | No versions given in the source |
| D10 | Non-goal reasons not stated in DECISIONS.md, as written above | Inferred from Q3, Q11 and the brief |

## Open questions (each tagged with the phase it blocks)

- **Q1 (resolved by ADR 0001, then made moot by ADR 0002):** Can `claude -p` run with subscription (OAuth) auth **and** without project/user settings, hooks and MCP? The help text says `--bare` accepts only `ANTHROPIC_API_KEY` auth, so `--bare` is ruled out. The D4 combination is unverified. *(blocks Phase 2, the LLM backend. A one-call probe resolves it.)*
- **Q2:** How do you want to submit: GitHub repo or zip? *(blocks only the final handoff. Owner: you, Q22.)*
- **Q3:** Do the API key's rate limits allow a full evaluation in one go? This is unknown until the first run. L25 makes it non-blocking: the runner stops cleanly and resumes. *(affects the Phase 6 timeline only)*

## Risks and mitigations

- **R1: Multi-agent makes more LLM calls, and each resends its system prompt.** *Mitigation:* report `llm_calls` next to tokens so a reviewer can see the per-call cost. Discuss it in the technical note. *(The per-call CLI overhead part was removed with the CLI, 2026-11-13.)*
- **R2 (closed 2026-11-13: the system's LLM calls no longer start Claude Code, ADR 0002): Every `claude -p` call from this project folder fires `.claude/settings.json`'s Stop hook (`export_transcript.py`), and also loads user MCP servers and CLAUDE.md.** That pollutes or overwrites `docs/transcript/` and inflates tokens. *Mitigation:* D4 isolation flags, plus running the subprocess with its working directory outside the project. Verify in Phase 2 that `docs/transcript/` is unchanged after a test call.
- **R3: Scope versus an overnight budget.** The plan has 6 agents, 2 live backends plus a mock, CLI plus API, 10 complex incidents and a judge. *Mitigation:* `openai_compat` is built but optional to evaluate (Q20). The brief's must-haves come first in the phasing.
- **R4: LLM nondeterminism with one run per incident.** *Mitigation:* fixed failure schedules. Note the single-run limitation. Keep the raw traces for inspection.
- **R5: The judge shares a model family with the system it grades.** *Mitigation:* the judge never decides accuracy (Q17). Note the risk of the judge favouring its own model family.
- **R6: Answer-key leakage into prompts.** *Mitigation:* keys live in a separate folder. A test asserts that no agent code path reads it (step 1, step 11).

## Responsibility split

| Product / AI builder provides | User (you) performs |
|---|---|
| Code, tests, dataset, answer keys, eval results | Reviews the answer keys and traps for realism |
| Draft technical note (≤ 500 words) | Edits and signs off the technical note |
| Transcript export via the hook | Checks the transcript for anything private before submitting |
| Local git commits | Chooses how to submit and does it (Q2) |
| The `anthropic_api` backend | Provides `ANTHROPIC_API_KEY` and pays for its usage |
| README setup steps | Runs the full evaluation (it is billed to your API key) |

## Phasing sketch

1. **Scaffold + schemas.** `uv` project, `git init`, pinned deps, Pydantic models (D2). `pytest` runs green on an empty suite.
2. **`LLMClient` backends.** `anthropic_api` (the default) and `openai_compat`, with tests against fake clients. *(Originally the `claude_cli` subscription backend, gated on Q1; replaced 2026-11-13, ADR 0002.)*
3. **Dataset + tools.** 10 scenarios, answer keys, 5 tools, failure injection (steps 1–2).
4. **Vertical slice.** One incident through plan → Observability → Reasoning → Reporting with a real LLM, producing a valid report (a thin step 8).
5. **Full graph.** Dependency and Critic agents, parallel fan-out, the Critic loop, limits, evidence rules (steps 4–7). Then the baseline.
6. **Evaluation.** Runner with checkpoint/resume, deterministic scorer, judge, `summary.md` (step 10). *It may stop at a rate limit and resume (Q3).*
7. **Interfaces + docs.** CLI polish, FastAPI (step 9), README, Mermaid diagram, technical note, transcript check (step 12). *Your sign-off: technical note and submission (Q2).*
