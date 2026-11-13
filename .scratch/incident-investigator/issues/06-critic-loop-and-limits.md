# 06: Critic feedback loop and bounded execution

**What to build:** The investigation challenges itself and always finishes. The Critic agent reviews the Hypotheses and can add follow-up tasks (for example, "check metrics for svc-B between 10:00 and 10:15"), which loop back through the Orchestrator. Every limit is enforced, and when one is hit the run still produces a report that says why it stopped.

**Blocked by:** 04 (planner and parallel investigation)

**Status:** done

Context: PRD L26, steps 5–6, non-functional constraints, error states. `DECISIONS.md` Q16.

- [x] Critic follow-up tasks appear on the blackboard with `origin=critic` and their loop number
- [x] With a mocked Critic that always asks for more, exactly 2 extra loops run, then Reporting runs, with `terminated_by=critic_limit`
- [x] Limits are enforced: 6 tool calls per agent task, 30 tool calls per run, 300k tokens per run (input plus output, excluding cache reads), and 10 minutes of wall-clock time
- [x] Each limit ends the run with its own `terminated_by` value, and a report is still written
- [x] An agent that fails twice is recorded as an AgentFailure. The DAG continues without it, and Reporting still runs
- [x] The limit values come from one shared config that the baseline also reads

## Comments

**2026-11-12, implementation:**
- **All boxes ticked.** Tests are in `tests/test_critic_and_limits.py` (15 tests). Each runs through `investigate()` with a scripted `RoleLLM`, a token-heavy LLM or a `FakeClock`. The last box is ticked on the interface: `limits.py` is importable, and the graph reads its limits only from there. Ticket 07 must import the same module.
- **Limits config (for 07):** `incident_investigator.limits` has `RunLimits` and `DEFAULT_LIMITS`. `RunLimits` is a frozen Pydantic model with `max_tool_calls_per_task=6`, `max_tool_calls_per_run=30`, `max_tokens_per_run=300_000`, `wall_clock_s=600`, `max_critic_loops=2` and `max_failed_attempts=2`. Enforcement uses `RunGuard(limits, clock=...)`. Wrap each LLM client in `GuardedLLM(client, guard)`, and send each tool call through `guard.tool_call(lambda: session.call(...))`. After a limit trips, the next LLM or tool call raises `RunStopped(reason)`. `run_agent` and `ask` catch it and return what they already have, with no AgentFailure. Read `guard.stop` for `terminated_by` and `guard.elapsed_s()` for latency. Pass `run_agent(max_tool_calls=limits.max_tool_calls_per_task)` for the per-task cap. The entry point is `investigate(incident_id, llm, *, limits=DEFAULT_LIMITS, clock=utc_now)`.
- **How `terminated_by` is decided:** if a run limit tripped, it is the guard's stop reason (the first limit hit wins):
  - `tool_budget`: the 31st tool call is asked for. It is refused, so exactly 30 calls are made.
  - `token_budget`: input plus output tokens reach 300k. Cache reads are excluded (D5). The call that reaches the budget completes, and no new call starts.
  - `timeout`: the clock passes 600 s at an LLM call, a tool call or a scheduling step.

  If no limit tripped, it is `critic_limit` when the Critic still asked for follow-ups after 2 loops, and `done` otherwise. After a limit trips, `schedule` dispatches nothing more, the Critic is skipped, and Reporting runs on the current blackboard. Reporting's LLM call for recommended actions bypasses the guard, but it is skipped after `token_budget` or `timeout`. A wall-clock trip cannot interrupt an LLM call that is already running. The 120 s per-call timeout bounds that call.
- **Critic:** the code is in `agents/critic.py` (`review` and `follow_up_tasks`), and the prompt is `CRITIC_SYSTEM` in `prompts.py`. The Critic runs after the evidence rules, but only when there are hypotheses and no limit has tripped. Its reply is `{critique, follow_ups: [{agent: observability|dependency, goal, params}]}`, with at most 3 follow-ups. An empty list means it is satisfied. Each loop adds tasks `c<n>.<k>` and a Reasoning task `c<n>.r` that depends on them. All of them have `origin="critic"` and `loop=n`. The second Reasoning pass also sees the current hypotheses and the critique. Reviews are kept on `Blackboard.critic_reviews`. `Blackboard.terminated_by` holds `critic_limit` and then the final value. The Critic is called a 3rd time after loop 2, to tell `critic_limit` apart from `done`. A Critic that fails twice is recorded as an AgentFailure, and the run reports with `done`.
- **Report:** added `InvestigationReport.failures: list[AgentFailure] = []` as the last field, after 05's `rule_adjustments`. `AgentFailure` moved to `schemas.py` and is re-exported from `blackboard` and `agents/actions`. `reporting` sets it with `model_copy` on the result of `build_report`.
- **Mock:** on its first review, the MockLLM Critic asks for one observability follow-up (metrics for the affected service over the incident window). After that it is satisfied. A mock run of inc01 now makes 17 LLM calls and 8 tool calls, and `test_cli` is updated to match. All 10 incidents end with `done` after 1 loop.
- **Other:** a task cut off by a limit is marked `failed`, with no AgentFailure. `run_agent` now counts a tool call only after it is dispatched. LangGraph's `recursion_limit` is raised to 500, because one loop takes about 7 graph steps.
- **Open:** hypotheses merge by id. If a revision pass drops an id, the old hypothesis stays on the board. The prompt asks for the full revised set to reduce this. `TerminatedBy` is defined in both `limits.py` and `evidence_rules.py` (05). Merge the two definitions when convenient.
