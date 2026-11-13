# 15: Close recovered evidence gaps, and stop on a rejected API key

**What to build:** Two fixes found by reading the first real API run of inc01 (`runs/api/36b674195ff14358979daf664e85893c.json`).

1. **Close recovered gaps.** That run ranked the right root cause first (checkout-svc `bad_deploy`, all 4 key evidence IDs cited, both traps avoided), but ended INCONCLUSIVE. A pricing-db metrics query in t3 timed out, the Critic's follow-up c1.2 re-ran it successfully, and the gap stayed open. Its 0.1 penalty took h1 from 0.62 to 0.52, under the 0.6 threshold.
2. **Stop on a rejected key.** Three earlier runs used an invalid key. Every LLM call failed, and each run still saved a "done" INCONCLUSIVE report built from 8 failed calls. In an evaluation, a bad key would fill every result file with fake results.

**Blocked by:** none

**Status:** done

- [x] Each gap gets an ID (`gap:<n>`) and the service its query was about. A later `status=ok` call to the same tool for the same service closes it (`ToolResult.closes_gaps`, `Blackboard.closed_gaps`)
- [x] A partial result closes nothing; another service or another tool closes nothing; a gap closes once
- [x] A closed gap lowers no confidence, is logged as a `gap_closed` RuleAdjustment and is left out of the report's gaps. Agents' prompts show open gaps only
- [x] Both modes (graph and baseline) record closures the same way
- [x] A 401 or 403 from the API raises `LLMAuthError`. Like `LLMRateLimitError`, it is an `LLMFatalError` that stops the run instead of counting as a failed attempt
- [x] CLI: exit 3, no report. API: the run ends `failed` with the error. Eval: stops, saves nothing for that pair, prints the resume command and exits 3

## Comments

**2026-11-13, implementation:**
- Tests first at the existing seams: `ToolSession` (tests/test_tools.py), the evidence rules (tests/test_evidence_rules.py), `investigate()` with the mock and a scenario whose first metrics call times out (tests/test_investigation.py), the API backend, the CLI, the eval runner and the HTTP API.
- The gap key is (tool, service), not the exact query. c1.2 in the real run used narrower metric names than t3, and an exact-query key would not have closed the gap. The cost is coarseness, listed in the README's unfinished features.
- The prompts now say "Open evidence gaps". The mock's Reporting role split its prompt on the old heading, so it was updated too.
- Not re-run live: whether c1.2 in the real run queried the same service as t3 is not recorded on the blackboard (tool args are not stored). The Critic's goal says it re-ran "the timed-out pricing-db metrics", so it most likely matches. A fresh `uv run investigate inc01` is the check.
