# 03: All five tools with injected failures

**What to build:** Agents can query every evidence source an investigator needs: logs, metrics, deployment history (including config changes), service dependencies and historical incidents. Each tool has structured input and output, returns Evidence with stable IDs, and behaves reproducibly when its scenario's failure schedule says the Nth call times out, returns a partial result or errors.

**Blocked by:** 01 (walking skeleton)

**Status:** done

Context: PRD data model (ToolResult, FailureSpec, EvidenceGap), step 2. `DECISIONS.md` Q5, Q16, Q21.

- [x] The 5 tools exist, each with a typed input schema and a ToolResult output (`ok`, `partial`, `timeout` or `error`)
- [x] Every evidence row has a stable ID that is unique within its scenario
- [x] Past incidents are found by keyword or tag search. There are no embeddings
- [x] A scheduled timeout is retried 2 times with backoff, and then an EvidenceGap is recorded
- [x] A scheduled partial result returns a marked subset of the rows
- [x] Invalid input (an unknown service, or a window whose end is before its start) returns `status=error` and does not raise
- [x] The same scenario and the same call sequence always produce the same failures
- [x] inc01 is extended with data for every tool and at least one scheduled failure

## Comments

**2026-11-12, implementation:**
- Tools: `tools/{logs,metrics,deploys,dependencies,past_incidents}.py`, each `query_x(scenario, XQuery) -> ToolResult`, pure (no failure injection). Unknown service and inverted window return `status=error`. Past incidents: case-insensitive keyword substrings + exact tags, ranked by match count, then ID.
- Dispatcher: `tools.ToolSession(scenario, retries=2, backoff_s=0.5, sleep=time.sleep)`. `.call(name, args_dict)` validates raw args (bad args / unknown tool -> `status=error`, never raises). `.run(name, typed_query)` takes an already-typed query. `TOOL_SPECS` holds name, description and input model (JSON schema for prompts).
- Failure counters live on the `ToolSession`, so create one per investigation run. They count accepted calls per tool. A call rejected as invalid input does not count.
- Timeout: a scheduled timeout persists across the retries of that call (the schedule targets the agent's Nth call). Result: `status=timeout`, `attempts=3`, sleeps 0.5 s then 1.0 s, plus an `EvidenceGap`. Partial: the first floor(n * fraction) rows (default 0.5), `status=partial`, `omitted=<count>`, plus a gap. A scheduled error returns `status=error` plus a gap, with no retry. Gaps come back on `ToolResult.gap`. Ticket 04 copies them onto the blackboard.
- Schema: `Scenario.dependencies` is now `list[DependencyRow{id, service, depends_on}]` with IDs `dependency:<inc>:NNNN` (was a dict). Also added `Scenario.services()`, `ToolResult.omitted` and `ToolResult.gap`.
- For ticket 04: `graph.py` still calls `query_logs` directly, so inc01's schedule (metrics call 2 times out, past_incidents call 1 is partial) does not fire there yet. Route agent calls through one `ToolSession` per run. Parallel agents share the session, so the call order across them must be deterministic, or the "Nth call" can differ between runs.
