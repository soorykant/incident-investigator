# 07: Single-agent baseline

**What to build:** A fair comparison point. The investigate command gains a single-agent mode: one Opus 5.5 agent working in a loop with the same tools, limits, failure schedules, JSON action protocol, evidence rules and InvestigationReport schema as the multi-agent system. Any difference in results then comes from the architecture alone.

**Blocked by:** 05 (evidence rules)

**Status:** done

Context: PRD L26, goals. `DECISIONS.md` Q6.

- [x] Single-agent mode on inc01 writes a schema-valid InvestigationReport
- [x] It uses the same tool implementations, the same limit config, and the same evidence-rule step as multi-agent mode
- [x] Hitting a limit stops it with the matching `terminated_by`, just like multi-agent mode
- [x] The token, tool-call, LLM-call and latency counters are recorded in the same fields as multi-agent mode
- [x] A test asserts that both modes read identical limit values

## Comments

**2026-11-12, implementation:**
- **Entry point (for 09 and 11):** `investigate(incident_id, llm, *, mode="multi", limits=DEFAULT_LIMITS, clock=utc_now, tool_backoff_s=DEFAULT_BACKOFF_S) -> Investigation{report, blackboard}` in `investigate.py`. `mode` is `Mode = Literal["multi", "single"]`, and `MODES` lists both. An unknown mode raises `ValueError`. An unknown incident raises `UnknownIncidentError`. The CLI takes `--mode multi|single` and writes `<id>__<mode>.json` and `<id>__<mode>.trace.json` in both modes. `tool_backoff_s` is the base retry backoff of a timed-out tool call (default 0.5 s, then 1.0 s). Tests pass 0.
- **Code:** `baseline.py` has `run_baseline(llm, tools, start, *, guard, models=None) -> Blackboard`. The prompt is `baseline_system(max_tool_calls)` in `prompts.py`: role `baseline`, the tool catalog and the shared `PROTOCOL` from `actions.py`. `investigate()` builds the ToolSession, the RunGuard and the start Blackboard once, for both modes.
- **Fairness choices:**
  - **Tools and failures:** the same `ToolSession` over the same scenario, so the same tools and the same failure schedule. Every call goes through `guard.tool_call`.
  - **Tool budget:** the baseline is one task, so the per-task cap of 6 does not apply. The run's 30-call budget is the binding limit, as it is for the multi-agent run as a whole. `run_agent` now accepts `max_tool_calls=None` (no per-task cap), and the baseline passes it. The guard refuses the 31st call and trips `tool_budget`, exactly as in multi mode. A cap of 6 would starve the baseline. A cap of 30 gives it the same total the multi-agent system can spend.
  - **Tokens, wall clock, failed attempts:** the same `RunLimits` object: 300k tokens without cache reads, 600 s, and 2 failed attempts, then an `AgentFailure` with agent and task id `baseline`. There is no Critic, so the baseline never ends with `critic_limit`.
  - **Model:** the `baseline` role in `models.toml` (Opus 5.5, Q6). With a `RoleRouter`, the baseline uses `client_for("baseline")`.
  - **Finish check:** the same as the Reasoning agent (`check_hypotheses`, now public in `specialists.py`): at least 2 competing hypotheses with unique ids, each citing a supporting ID.
  - **Report step:** the same `apply_evidence_rules` and `build_report`, and the same `recommend_actions` call for the recommended actions. That call uses the baseline's own client and model, not the Haiku reporting role. As in multi mode, it runs outside the guard and is skipped after `token_budget` or `timeout`. It counts in the metrics.
  - **Counters:** the same `ReportMetrics` fields, and `blackboard.counters` matches them. The blackboard has one `Finding` (task id `baseline`), the evidence, the hypotheses, the gaps (task id `baseline`) and one `TaskTrace` entry. It has no tasks and no Critic reviews.
- **Mock:** `MockLLM` plays `baseline` without knowing the incident. It calls logs, metrics, deploys, dependencies and past_incidents. Then it finishes once with the investigators' facts and assumptions and the mock Reasoning hypotheses. inc01 in single mode: 5 tool calls, 7 LLM calls, ROOT_CAUSE, `done`. All 10 incidents end with `done` and no failures.
- **Shared-limits test:** `tests/test_baseline.py::test_both_modes_read_the_same_limit_config_object` spies on `RunGuard` and asserts that both modes get the same object (`is DEFAULT_LIMITS`). A parametrized test also checks that a custom tool budget binds both modes the same way.
- **Test speed:** the slow mock tests pass `tool_backoff_s=0`. `test_cli.py` has an autouse fixture that does the same for in-process CLI runs. The answer-key runtime test now runs both modes on all 10 incidents. The suite went from about 45 s to about 11 s. No assertion was weakened.
- **Tests:** `tests/test_baseline.py` (15 tests). 270 tests pass, and pyright is clean.
- **Open:** the baseline re-sends its whole transcript at each step (Q15, no native tool calling), so its token use grows roughly with the square of its tool calls. That cost is part of what the comparison measures. `TerminatedBy` is still defined in both `limits.py` and `evidence_rules.py`.
