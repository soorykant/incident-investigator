# 02: Real Claude through the subscription CLI

> **Superseded 2026-11-13 by ticket 14** (DECISIONS.md Q25, ADR 0002). The `claude_cli` backend this ticket built was removed; Claude is now called only through `ANTHROPIC_API_KEY`. The text below is the original record.

**What to build:** The skeleton investigation runs on real Claude models through the user's subscription. An `LLMClient` interface gets its default `claude_cli` backend, which shells out to `claude -p --output-format json`. Agents return JSON actions (`call_tool` or `finish`) that are validated by Pydantic. Per-agent model choice comes from a models config (Opus 5.5 for plan creation, Reasoning, Critic, baseline and judge; Haiku 5.5 for Observability, Dependency and Reporting). This ticket also resolves PRD open question Q1 and risk R2.

**Blocked by:** 01 (walking skeleton)

**Status:** done

Context: PRD L24, D4, D5, R1, R2, Q1. `DECISIONS.md` Q8, Q15, Q20.

- [x] A one-call probe confirms which flag combination works with subscription (OAuth) auth while skipping user/project settings, hooks, MCP servers and CLAUDE.md. Record the finding. `--bare` is ruled out because it needs an API key
- [x] Every CLI call uses a replacement system prompt, has all built-in tools disabled (`--tools ""`), does not persist the session, and runs with its working directory outside the project
- [x] After a real call, the transcript export folder is unchanged. The project's Stop hook did not fire
- [ ] Input, output and cache tokens, latency and the LLM call count are read from the CLI's JSON output and recorded separately on the blackboard counters
- [x] Each call has a 120 s timeout. At most 3 LLM calls are in flight at once
- [x] Invalid JSON from the model counts as a failed attempt. Two failed attempts produce an AgentFailure instead of a crash (unit-tested with a mocked subprocess)
- [x] If the CLI is missing or not logged in, the run fails fast before it starts, with a clear message
- [x] The investigate command on inc01 with the real backend writes a schema-valid report

## Comments

**2026-11-12, implementation:**
- **Probe (Q1 resolved, R2 checked):** subscription OAuth works with `claude -p --output-format json --model <m> --system-prompt <s> --tools "" --setting-sources "" --strict-mcp-config --mcp-config '{"mcpServers":{}}' --no-session-persistence --disable-slash-commands`. The prompt goes on stdin, and the cwd is `$TMPDIR/incident-investigator-claude-cwd`. The parent-session environment variables are removed. One Haiku call used 676 input tokens in total, and the debug log shows 0 hooks and no MCP servers. `docs/transcript/` (mtimes and hashes) was unchanged after the probe and after a full real run. No `~/.claude/projects/` session folder was written. Details: `docs/adr/0001-claude-cli-isolation-flags.md`.
- **Real run:** `uv run investigate inc01 --llm claude_cli` wrote a schema-valid report: ROOT_CAUSE, `bad_deploy` on checkout-svc, 3 LLM calls, 2 tool calls, about 17 s.
- **Token mapping (D5):** `tokens_in` = `input_tokens` + `cache_creation_input_tokens`. The CLI writes almost the whole prompt to the cache, so `input_tokens` alone is about 2 per call. `cache_tokens` = `cache_read_input_tokens` only, and the budget excludes it.
- **Unticked box (latency):** input, output and cache tokens and the LLM call count are on the counters. Per-call latency is read from `duration_ms` into `LLMResponse.latency_s`, but it is not summed on the counters. `ReportMetrics` (built from `Counters`, `extra=forbid`) has no field for it, and only the run's wall-clock `latency_s` is reported. Add an `llm_latency_s` counter when ticket 04 reshapes the Blackboard and Counters (`Counters.add(usage)`).
- **Code:** `llm/claude_cli.py` (backend, 120 s timeout, a process-wide semaphore of 3, `preflight()`), `llm/base.py` (`preflight` added to the `LLMClient` protocol, plus `LLMError`, `LLMTimeoutError` and `LLMUnavailableError`), `agents/actions.py` (`CallToolAction | FinishAction`, `parse_action`, `AgentFailure`, `PROTOCOL` prompt text), `agents/loop.py` (`run_agent`: 6 tool calls per task. Two failed attempts make an `AgentFailure`. A failed attempt is an invalid reply, an `LLMError` or timeout, or a tool call over the limit), `models_config.py` with `models.toml`, and the CLI option `--llm claude_cli`. Exit code 3 means the preflight failed and no report was written.
- **Deviations:** the models config is TOML (`src/incident_investigator/models.toml`, read with stdlib `tomllib`), not YAML. That avoids a new dependency and a lock-file conflict with ticket 03. `AgentFailure` is in `agents/actions.py`, not `schemas.py`, to avoid a merge conflict with ticket 03. `--llm` now defaults to `claude_cli` (Q15). The mock now speaks the protocol (call_tool logs, then finish), so a mock run makes 2 LLM calls and `test_cli` expects `llm_calls == 2`.
- **For ticket 04:** the observability node dispatches only `logs`. Other tool names come back to the model as `status=error` "not available" results. Plug in the registry by passing `tools={name: callable(args) -> ToolResult}` to `run_agent`. `Blackboard.failures` exists, but `InvestigationReport` does not report failures yet. Move `AgentFailure` next to the reshaped Blackboard schema.

**2026-11-13, API key only (ticket 14):**
- Removed: `llm/claude_cli.py`, `tests/test_llm.py` and the `--llm claude_cli` choice. A models config that names `claude_cli` now fails at startup.
- Kept from this ticket: the `LLMClient` protocol with `preflight()`, the JSON action protocol, the agent loop, the 120 s timeout, the cap of 3 calls in flight and the TOML models config. The agent-loop tests now drive `AnthropicAPI` with a fake SDK client.
- ADR 0001 is marked superseded by ADR 0002. Risk R2 is closed: the system's LLM calls no longer start Claude Code, so they cannot fire the transcript hook.
