# 16: Salvage agent work, and a budget-aware Critic

**What to build:** Three fixes found by reading the live inc04 run (`runs/api/6efefa7fd85f4f96ab9abf034f964eff.json`). It ranked the right cause first (image-resizer `capacity`) but ended INCONCLUSIVE at 0.45 with `terminated_by: tool_budget`. 12 of its 30 tool calls produced nothing, so the Critic's second loop, which asked for the decisive 5-day memory baseline (`metric:inc04:mem:0001`), got no tool calls.

- t2 reached its 6-call limit, kept calling tools and failed: its findings were lost.
- c1.1 made 6 calls, then finished with 3 facts that cited no evidence IDs. The whole finish was rejected twice and its work was lost.
- The Critic did not know the budget, and asked for 3 follow-ups when none could run.

**Blocked by:** none

**Status:** done

- [x] After each tool result the agent sees "Tool calls left in this task: N of 6"; at 0 it is told to finish
- [x] The first call over the per-task limit is a warning that gives one finish-only turn, not a failed attempt. Further calls over the limit are failed attempts, as before
- [x] A finish whose fact cites no evidence IDs is kept: that fact becomes an Assumption whose basis says so. The other facts stay facts
- [x] The Critic's prompt shows "Tool calls left in this run: N of 30" and the per-task cap, and asks for only the follow-ups the budget can pay for
- [x] When no tool calls are left after the evidence rules, the Critic is not asked; Reporting runs with `terminated_by: tool_budget`

## Comments

**2026-11-13, implementation:**
- Tests first: `run_agent` and `parse_action` (tests/test_agent_loop.py), and `investigate()` with the scripted RoleLLM (tests/test_critic_and_limits.py). One existing test changed: an agent that always calls tools now fails after 9 replies (6 calls, the warning, 2 failed attempts) instead of 8.
- Uncited facts are demoted inside `FinishAction` parsing, so they never reach the evidence rules as Facts. They are not logged as a `RuleAdjustment`; the Assumption's basis records the reason.
- `RunGuard.tools_spent()` trips `tool_budget` only from the Critic routing. A pending Reasoning task needs no tools, so the scheduler does not check it.
- The baseline is unchanged: it has no per-task cap, and its prompt already states the run budget.
- Not re-run live. A fresh inc04 run is the check; the effect on accuracy depends on how the model uses the extra calls.
