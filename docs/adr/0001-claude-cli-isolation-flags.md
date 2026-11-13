# ADR 0001: Isolation flags for `claude -p` with subscription auth

**Status:** superseded by ADR 0002 on 2026-11-13: the `claude_cli` backend was removed, and Claude is called only through `ANTHROPIC_API_KEY`. Originally accepted 2026-11-12. Resolved PRD Q1 and checked risk R2 (decision D4).

## Context

The `claude_cli` backend calls Claude through the user's subscription (OAuth), not an API key (Q15). Each call must skip user and project settings, hooks, MCP servers and CLAUDE.md. Otherwise the project's Stop hook rewrites `docs/transcript/` and the overhead inflates the token counts (L24, R2). `--bare` would do this, but it accepts only `ANTHROPIC_API_KEY` auth, so it is ruled out.

## Decision

Every call runs this command, with the prompt on stdin. The working directory is `$TMPDIR/incident-investigator-claude-cwd`, which is outside any project:

```
claude -p --output-format json --model <model> \
  --system-prompt "<agent system prompt>" \
  --tools "" \
  --setting-sources "" \
  --strict-mcp-config --mcp-config '{"mcpServers":{}}' \
  --no-session-persistence \
  --disable-slash-commands
```

| Flag | Purpose |
|---|---|
| `--system-prompt` | Replaces Claude Code's default system prompt (L24) |
| `--tools ""` | Turns off all built-in tools |
| `--setting-sources ""` | Loads no user, project or local settings, so no settings hooks (the Stop hook) and no `enabledPlugins` |
| `--strict-mcp-config` with an empty `--mcp-config` | Loads no MCP servers. The debug log shows "Artifacts MCP server not added: strict_mcp_config" |
| `--no-session-persistence` | Saves no session to `~/.claude/projects/` |
| `--disable-slash-commands` | Loads no skills or commands |
| cwd outside the project | No project `.claude/` and no CLAUDE.md lookup |

The backend also removes the variables that link a child `claude` to a parent Claude Code session (`CLAUDECODE`, `CLAUDE_CODE_SESSION_ID`, `CLAUDE_CODE_MESSAGING_*` and others) from the child's environment. It keeps auth variables such as `CLAUDE_CODE_OAUTH_TOKEN`.

## Evidence (Claude Code 2.1.322, `claude.ai` team login)

- **Probe:** 1 call, `claude-haiku-5-5`, run from a fresh `mktemp -d` directory with the flags above. Exit 0, `"is_error": false`, about 3 s. The OAuth subscription auth works with these flags.
- **Overhead:** the whole call used `input_tokens` 2, `cache_creation_input_tokens` 674 and `output_tokens` 9. So no tool schemas, MCP schemas or CLAUDE.md were sent.
- **Debug log (`--debug-file`):** "Registered 0 hooks from 5 plugins", "project memory is off", and "Artifacts MCP server not added: managed_or_strict_mcp_config". The settings lookups resolve to the temp directory, not the project.
- **`docs/transcript/`:** the file list, mtimes, sizes and SHA-256 hashes of the main checkout were snapshotted before and after the probe and after a full real `investigate inc01 --llm claude_cli` run (3 calls). They were unchanged both times, so the Stop hook did not fire. No new `~/.claude/projects/` folder appeared either.
- **Prompt on stdin:** the full real run passed every prompt on stdin, and that works.

## JSON output fields the backend reads

`result` (the model's text), `is_error`, `subtype`, `duration_ms`, and `usage.input_tokens`, `usage.output_tokens`, `usage.cache_creation_input_tokens` and `usage.cache_read_input_tokens`. Other fields are present but unused: `total_cost_usd`, `modelUsage`, `num_turns`, `session_id` and `ttft_ms`.

Token mapping: `tokens_in = input_tokens + cache_creation_input_tokens`, `tokens_out = output_tokens`, `cache_tokens = cache_read_input_tokens`. The CLI writes almost the whole prompt to the prompt cache, so `input_tokens` alone is near zero (6 tokens for a 3-call run). Cache writes are new input and count toward the budget. Cache reads do not count (D5).

## Consequences and remaining overhead

- The binary's own native plugin modules still load (security defaults, telemetry, the AGENTS.md loader). They register 0 hooks and add no prompt tokens here, but the flags cannot remove them. Document this as a limitation (L24).
- Each call costs about 1 s of process start-up. With the 3-call concurrency limit this adds to latency, and R1 already covers it.
- A newer CLI version could change these flags. If `claude --version` changes, re-run the probe.
