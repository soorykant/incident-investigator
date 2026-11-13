# ADR 0002: Call Claude only through the API key, and remove the subscription CLI backend

**Status:** accepted, 2026-11-13. Supersedes ADR 0001. Changes DECISIONS.md Q13, Q15 and Q20.

## Context

The first build called Claude through the user's subscription, by running `claude -p` once per LLM call (`claude_cli` backend, ADR 0001). In use it proved fragile for an application:

- Each call starts a Claude Code process, and some of the CLI's own overhead stays in every call even with the isolation flags.
- Usage caps and rate limits had to be read from the CLI's wording with a regex, and that wording can change.
- The isolation depends on CLI flags and environment variables that can change between Claude Code versions.
- A subscription is meant for interactive use by one person, not for a service that runs investigations unattended.

The project owner decided that the API key is the only right way for this kind of application.

## Decision

- Delete the `claude_cli` backend, its tests and its CLI choice. The backends are now `anthropic_api`, `openai_compat` and `mock`.
- `anthropic_api` is the default for every role in `models.toml`, and a plain model string in that file means `anthropic_api`. A config that still names `claude_cli` fails at startup with `unknown backend 'claude_cli'`.
- `anthropic_api` raises `LLMRateLimitError` on HTTP 429, so the evaluation runner stops cleanly (exit 4) and can resume. Any other API error, including a 529 overload, is an ordinary failed attempt that the agent loop retries.
- A test fixture unsets `ANTHROPIC_API_KEY` for every test, so no test can make a billed call by accident.
- The JSON action protocol stays. Every backend, including local models, speaks it, so no agent code changes.

## Consequences

- Live runs, the HTTP API and the live evaluation need `ANTHROPIC_API_KEY`, and their calls are billed to it. Without the key a run fails fast with exit code 3.
- The per-call CLI overhead is gone, and the tokens reported are what the API reports.
- `anthropic_api` is tested with a fake SDK client only. It has not yet made a live call.
- ADR 0001 is kept as history. The Stop hook in `.claude/settings.json` can no longer fire from the system's own LLM calls, because those calls never start Claude Code.
