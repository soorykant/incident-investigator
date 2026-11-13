# 14: Use only the API key, remove the subscription CLI backend

**What to build:** Claude is called only through `ANTHROPIC_API_KEY`. The `claude_cli` backend (`claude -p` on the subscription) is deleted, and `anthropic_api` becomes the default for every role. The owner reported problems with the subscription route and decided an API key is the right solution for this kind of application (DECISIONS.md Q25, ADR 0002).

**Blocked by:** none

**Status:** done

- [x] `llm/claude_cli.py` and its tests are deleted; `claude_cli` is no longer a backend or a `--llm` choice
- [x] `models.toml` uses `anthropic_api` for every role; a plain model string means `anthropic_api`; a config naming `claude_cli` fails at startup
- [x] With no `ANTHROPIC_API_KEY`, a live run fails fast with exit 3 and writes nothing
- [x] A 429 from the API raises `LLMRateLimitError`, so the eval runner stops cleanly and can resume; a 529 overload is retried as an ordinary failed attempt
- [x] No test can make a billed call: a conftest fixture unsets `ANTHROPIC_API_KEY`
- [x] README, technical note, DECISIONS.md and ADRs updated

## Comments

**2026-11-13, implementation:**
- Tests were changed first (router, models config, CLI, agent loop, API backend), then the code. The agent-loop and "garbage reply" CLI tests now drive the agent through `AnthropicAPI` with a fake SDK client instead of a mocked subprocess.
- While the tests were red, one test run started with the old default (`claude_cli`). The CLI was logged in, so it may have made a few real `claude -p` calls on the subscription before it was stopped. That is why the conftest guard now unsets the API key for every test.
- The evaluation summary no longer has the "CLI overhead" note.
- Open: `anthropic_api` has not made a live call yet. The first `uv run investigate inc01` with a real key is the check.
