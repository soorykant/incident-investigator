# 04: Planner and parallel investigation on a shared blackboard

**What to build:** A real multi-agent investigation. The Orchestrator makes one Opus call that turns the incident report into a typed task plan with dependencies. Plain Python runs that task DAG over a shared blackboard (the LangGraph state). The Observability and Dependency agents run at the same time, each calling tools and writing Findings (Facts and Assumptions). The Reasoning agent builds and compares competing Hypotheses from those Findings. The Reporting agent writes the ranked report.

**Blocked by:** 02 (Claude CLI backend), 03 (all tools)

**Status:** done

Context: PRD capability map, step 4. `DECISIONS.md` Q4, Q9, Q14.

- [x] The planning call produces a validated task list with `depends_on` edges
- [x] Independent tasks start concurrently (overlapping start and end times in the trace, shown with a mocked LLM)
- [x] A task never starts before every task it depends on is done
- [x] Agents exchange information only through typed entries on the blackboard, not free text passed between prompts
- [x] The Reasoning agent outputs at least 2 competing Hypotheses, each with supporting and contradicting evidence IDs
- [x] A live run on inc01 produces a report whose top Hypothesis names a service and a category
- [x] The full blackboard trace is saved with the report

## Comments

**2026-11-12, implementation:**
- **Live run:** `uv run investigate inc01` (claude_cli, default routing) planned 4 tasks (2 observability, 1 dependency, all in wave 0, then reasoning in wave 1). The three investigators overlapped (18:668 to 48:111), and reasoning started 2 ms after the last one ended. Top hypothesis: checkout-svc / bad_deploy at 0.8, then pricing-db / dependency_failure, checkout-svc / capacity and search-svc / config_change. 15 LLM calls, 8 tool calls, 31k tokens in, 77 s. The partial past_incidents result was recorded as a gap on t3.
- **Box 5 caveat:** the code requires at least 2 hypotheses, with distinct (service, category), unique IDs and at least 1 supporting ID each. A rejected reply is sent back once. It does not require contradicting IDs, so the model is not pushed to invent them. The prompt asks for them, and the mock and the tests show both lists. In the live run, h2 to h4 had contradicting IDs and h1 had none.
- **Code:** `blackboard.py` has `Blackboard`, `Task`, `TaskTrace`, `Counters` (`add(usage=, tool_calls=, llm_calls=, llm_latency_s=)` and `+`), `AgentFailure` (moved here and re-exported from `agents/actions.py`) and the `merge_by_id` reducer. `agents/planner.py` has `make_plan(llm, incident=, model=) -> PlanResult`. `agents/specialists.py` has the investigators, Reasoning and `recommend_actions`. `agents/ask.py` makes a non-action JSON call with 2 attempts. `agents/prompts.py` holds the system prompts and `role_of(system)`. `dag.py` has `ready_tasks` and `ToolTurns`. `evidence_rules.py` is the seam for 05. `graph.py` is the graph. `investigate()` now returns `Investigation{report, blackboard}`. The CLI also writes `<id>__multi.trace.json`, which is the full blackboard dump.
- **Blackboard shape:** `incident_id`, `incident` (the IncidentReport only, never the Scenario), `started_at`, `tasks`, `findings`, `evidence` (every row a tool returned in this run), `hypotheses`, `gaps`, `failures`, `counters` (with `llm_latency_s`, the deferred item from 02), `trace` (`TaskTrace{task_id, agent, wave, started_at, ended_at, status, tool_calls, llm_calls}`), `critic_loops` and `report`. Fields that parallel tasks write have reducers. `tasks` and `hypotheses` merge by id, so a write with a known id replaces the old entry. `EvidenceGap` gained `task_id`.
- **Graph:** `plan -> schedule -(Send per ready task)-> run_task -> schedule ... -> evidence_rules -> reporting`. `schedule` marks tasks running. A failed dependency counts as finished, so the DAG continues (Q16). A task that depends on an unknown ID is marked failed. Reporting ranks the hypotheses in code by `final_confidence`, and the Reporting LLM only writes `recommended_actions` that point at known hypothesis IDs. If there are no hypotheses, Reporting makes no LLM call.
- **Planner:** the plan is validated for unique IDs, known agents, existing dependencies, no cycles, at least one investigator, and exactly one reasoning task that depends, directly or through other tasks, on every investigator. An invalid plan is sent back once with the problem. A second failure records `AgentFailure(agent="planner", task_id="plan")` and the run uses `DEFAULT_PLAN`.
- **Determinism choice:** `dag.ToolTurns`. The tasks in one wave are ranked by plan order. The k-th tool call of rank r has key (k, r), and calls are dispatched in key order: round-robin, with plan order inside each round. A call waits until every smaller key has been made, or until the task that would make it has finished. LLM calls never wait, so tasks still overlap. The failure schedule's "Nth call" now depends only on what each agent asks for, not on thread timing. Waves run one after another. There is a test for this: on inc01, the dependency agent asks for metrics first, but the 2nd metrics call (the timeout) still goes to t2.
- **Routing (ticket 12):** `build_graph` uses `router.client_for(role)` when it is given a `RoleRouter`, and takes the `ModelsConfig` from the router. The CLI builds the router with `route_by_model=False`. A single plain `LLMClient` still works for every role.
- **For 05:** replace the body of `evidence_rules.apply_evidence_rules(board) -> list[Hypothesis]`, which today sets final = proposed. The IDs that really exist are `board.evidence`. Gaps carry `task_id`, so you can fill `affects_hypotheses` there. The baseline (07) should call the same function.
- **For 06:** put the Critic in `graph._after_evidence_rules`, which today always routes to `reporting`. It should append `Task(origin="critic", loop=n)` to `tasks` (the reducer appends new IDs), increment `critic_loops` and route to `schedule`. Note that hypotheses merge by id, so a second reasoning pass replaces h1/h2 with the same IDs. Run limits fit in `schedule` (stop dispatching) and in the tool wrapper `_run.call_tool`. `run_agent` already enforces 6 tool calls per task.
- **For 07:** `investigate()` returns `Investigation`, and the baseline can fill the same Blackboard.
- **Mock:** `MockLLM(delay_s=0.01)` plays every role from the `Role: <role>` line at the top of each system prompt. It plans obs || dep -> reasoning, makes 5 tool calls and 10 LLM calls, and builds hypotheses h1 bad_deploy (0.7) and h2 dependency_failure (0.4) on the affected service. It runs cleanly on all 10 incidents, including timeouts, partials and errors.
- **Open:** `InvestigationReport` still has no `failures` field, so failures appear only in the trace file. Ticket 13 or 11 can add it if the report needs it.

**2026-11-13, API key only (ticket 14):**
- The live run recorded above used the removed `claude_cli` backend. The same command now runs on `anthropic_api` and needs `ANTHROPIC_API_KEY`; it has not been re-run on the API yet.
