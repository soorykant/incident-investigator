# 17: Accept JSON with text around it, and let the tool budget stop only tool calls

**What to build:** Two fixes found by reading the second live inc04 run (`runs/api/cfa12a2e1dae4c2992bd65e8f6e83562.json`). It ranked the right cause first (image-resizer `capacity`, 0.55, 5 of 6 key evidence IDs) but stayed INCONCLUSIVE. The decisive check, memory climbing for days before the deploy, was lost twice:

1. Task c1.1 made 5 tool calls, then both of its finish replies failed with `Invalid JSON: expected value at line 1 column 1`: the reply did not start with `{`.
2. Task c2.1 retrieved `metric:inc04:mem:0003` (3420 MiB on Nov 12, before the deploy), then the run hit its 30-call budget. That stopped all LLM work, so c2.1 never wrote a finding and the final Reasoning task c2.r never ran.

**Blocked by:** none

**Status:** done

- [x] Agent actions, structured answers (planner, Critic, Reporting) and the judge use the first JSON object in a reply; prose and code fences around it are ignored (`json_text.extract_json`)
- [x] A reply that still fails is quoted (first 200 characters) in the failure reason
- [x] The tool budget stops only tool calls: a refused call gives the agent one finish-only turn; calling a tool again after that is a failed attempt
- [x] When no tool call is left, investigator tasks that have not started are skipped and pending Reasoning tasks still run; the Critic is skipped; `terminated_by` is `tool_budget`
- [x] The token budget and the wall clock stay hard stops
- [x] The baseline follows the same rule, and its prompt says so

## Comments

**2026-11-13, implementation:**
- Tests first: `parse_action` and `run_agent` (tests/test_agent_loop.py), the graph through `investigate()` (tests/test_critic_and_limits.py), and the baseline (tests/test_baseline.py).
- Three existing tests changed meaning. An agent that always calls tools now fails after the finish-only turn instead of being "stopped": in multi mode each such investigator records an AgentFailure, and the baseline makes 33 LLM calls instead of 31. In the six-investigator test, Reasoning is now dispatched after the budget is spent.
- `RunGuard.stop` now reports `tool_budget` without blocking LLM calls; `RunGuard.poll()`/`check()` report only hard stops. The scheduler skips investigators when `tool_calls_left() == 0`, then re-checks so Reasoning tasks that depended on them become ready.
- The same JSON-extraction code replaced three copies of the code-fence regex (agent actions, `ask`, judge).
- Not re-run live. A third inc04 run is the check.
