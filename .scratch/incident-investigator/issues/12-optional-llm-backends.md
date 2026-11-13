# 12: Optional LLM backends (API key and local model)

> **Updated 2026-11-13 by ticket 14:** `anthropic_api` is no longer optional. It is the default for every role and the backend the published evaluation uses; `claude_cli` was removed. The text below is the original record.

**What to build:** The system can run without the subscription. Two more `LLMClient` backends are added: `anthropic_api` (reads `ANTHROPIC_API_KEY`) and `openai_compat` (Ollama, LM Studio or vLLM through an OpenAI-compatible endpoint). Each is selectable per agent in the models config. The published evaluation still uses Claude through the CLI. This is the first ticket to cut if time runs short.

**Blocked by:** 02 (Claude CLI backend)

**Status:** done

Context: PRD capability map, non-goals. `DECISIONS.md` Q15, Q20.

- [x] Both backends implement the same interface and return the same JSON action and usage fields as `claude_cli`
- [x] Choosing a backend per agent in the models config works. An unknown backend name fails at startup with a clear message
- [x] A missing API key or an unreachable local endpoint fails fast with a clear message
- [x] Both are covered by mocked tests. A live local-model run is optional and documented if done

## Comments

**2026-11-12, implementation:**
- **Backends:** `llm/anthropic_api.py` (`AnthropicAPI`, official `anthropic` SDK 2.8.0) and `llm/openai_compat.py` (`OpenAICompat`, plain `httpx` 0.28.1, `POST {base}/chat/completions`, no openai SDK). Both implement `preflight()` and `complete(*, system, prompt, model) -> LLMResponse`. The model's reply text goes back unchanged, so the JSON action protocol is the same as for `claude_cli`. Errors: a timeout raises `LLMTimeoutError`, an API, HTTP or connection error or a malformed reply raises `LLMError`.
- **Usage mapping (D5):** `anthropic_api` maps usage exactly like `claude_cli`: `tokens_in` = `input_tokens` + `cache_creation_input_tokens`, `cache_tokens` = `cache_read_input_tokens`. `openai_compat` maps `tokens_out` = `completion_tokens`. When the server reports `usage.prompt_tokens_details.cached_tokens` (Ollama and vLLM do), those tokens go to `cache_tokens` and `tokens_in` = `prompt_tokens` − cached. So cache reads stay out of the budget for every backend.
- **Limits:** the 120 s timeout and the semaphore of 3 moved from `claude_cli.py` to `llm/base.py` (`CALL_TIMEOUT_S`, `MAX_IN_FLIGHT`, `IN_FLIGHT`). `claude_cli` re-exports the names. The cap of 3 now covers all backends together, process-wide (Q20). `anthropic_api` sets the SDK's `max_retries=0`, so the agent loop owns retries, as with `claude_cli`.
- **Fail fast:** `anthropic_api.preflight()` checks that `ANTHROPIC_API_KEY` is set. It makes no network call, so a wrong key fails on the first call as an `LLMError`. `openai_compat.preflight()` sends `GET {base}/models` with a 5 s timeout. If the server is unreachable or does not answer 200, it raises `LLMUnavailableError`. The message names the URL and how to fix it (`ollama serve`, `OPENAI_COMPAT_BASE_URL`, models.toml). The CLI prints `error: ...` and exits 3 with no report.
- **Base URL precedence:** `$OPENAI_COMPAT_BASE_URL`, then `[backends.openai_compat] base_url` in models.toml, then `http://localhost:11434/v1`. `$OPENAI_COMPAT_API_KEY` is sent as a bearer token when it is set (for vLLM `--api-key`).
- **Models config:** each role in `models.toml` is now `{ backend = "...", model = "..." }`. A plain string still loads as `claude_cli`, so the loader is backward-compatible. The defaults are unchanged: every role uses `claude_cli` with the same models. The known backends are `claude_cli`, `anthropic_api`, `openai_compat` and `mock`. An unknown name fails at load with `roles.<role>.backend: unknown backend '<name>'; choose one of: ...`, and the CLI exits 3. `ModelsConfig` gained `backend_for(role)`, `choice_for(role)`, `role_names()` and `backends.openai_compat.base_url`.
- **Routing:** `llm/router.py` has `make_client(backend, models)` and `RoleRouter(models)`. `RoleRouter` is an `LLMClient`. `client_for(role)` returns the role's backend client. `preflight()` checks each backend in use once. `complete(model=...)` dispatches by model name, because the graph names the role only through `model=models.model_for(role)`. With this routing, one model string on two backends is a startup error (`ModelsConfigError`). Pass `route_by_model=False` to allow that, and route only with `client_for`.
- **CLI:** with no `--llm`, the CLI uses `RoleRouter(load_models())`, which with the default config behaves exactly as before. `--llm claude_cli|anthropic_api|openai_compat|mock` overrides the backend for every role.
- **Integration step for the graph (ticket 04 or later):** nothing is required. `investigate(id, RoleRouter(models))` works today, because routing by model name is transparent. For cleaner routing, `build_graph` should take the router and call `router.client_for(role).complete(...)` for each node. It should also pass the same `ModelsConfig` that it uses for `model_for(role)`, and build the router with `route_by_model=False`. Then one model string can run on two backends (for example `claude-opus-5-5` through both the CLI and the API). Today `build_graph` and the CLI each call `load_models()` on the default path, so both read the same file.
- **Live run (optional, done):** Ollama was already running on localhost:11434. With `observability`, `dependency` and `reporting` set to `{ backend = "openai_compat", model = "qwen2.5:7b" }`, `investigate inc01` (default routing, through the router) gave ROOT_CAUSE, `bad_deploy` on checkout-svc, in about 7 s with 2 LLM calls and 1 tool call. `tokens_in` was 746, `tokens_out` 346 and `cache_tokens` 426 (Ollama reports cached prompt tokens). For this probe only, the claude_cli preflight was stubbed, because the other roles were not called. A direct `OpenAICompat` call returned `{"ok": true}` (24 in, 6 out, about 2 s). `anthropic_api` was not run live, because no API key was used.
- **Tests:** `tests/test_llm_openai_compat.py` (httpx `MockTransport`), `tests/test_llm_anthropic_api.py` (a fake client injected through the constructor, real `anthropic` exception types), `tests/test_llm_router.py`, and new cases in `tests/test_models_config.py` and `tests/test_cli.py`. One CLI test connects to `127.0.0.1:9` and expects the connection to be refused. No live calls.

**2026-11-13, API key only (ticket 14):**
- `anthropic_api` is the default backend for every role in `models.toml`, and a plain model string now means `anthropic_api` (was `claude_cli`). The known backends are `anthropic_api`, `openai_compat` and `mock`.
- `anthropic_api` now raises `LLMRateLimitError` on HTTP 429, so the eval runner stops cleanly and resumes. A 529 overload stays an ordinary failed attempt and is retried.
- The preflight message no longer suggests `--llm claude_cli`.
- A `tests/conftest.py` fixture unsets `ANTHROPIC_API_KEY` for every test, so no test can make a billed call.
