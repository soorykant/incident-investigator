# 11: HTTP API with Swagger

**What to build:** Investigations can be started and inspected over HTTP. FastAPI exposes the same `investigate()` entry point the CLI uses, running investigations as background tasks. Results are kept in memory plus JSON files, with no database.

**Blocked by:** 07 (single-agent baseline)

**Status:** done

Context: PRD step 9, error states. `DECISIONS.md` Q19.

- [x] `POST /investigations` with an incident ID (or an inline incident report) and `mode: multi|single` returns an investigation ID right away
- [x] `GET /investigations/{id}` returns the status, the blackboard trace and, when done, the InvestigationReport
- [x] `GET /incidents` lists the 10 incidents. Swagger renders at `/docs`
- [x] An unknown incident ID returns 404 and starts no run. An invalid body returns 422. An unknown investigation ID returns 404
- [x] A crashed run ends with `status=failed` and an error message. It never hangs
- [x] The CLI and the API produce identical report shapes (tested with the mock LLM)

## Comments

**2026-11-12, implementation:**
- **Start it:** `uv run api` serves on 127.0.0.1:8000 (`--host`, `--port`, `--results-dir`, default `runs/api`). Swagger is at `/docs`. The LLM comes from `models.toml` per role (`RoleRouter`, as the CLI without `--llm`), so the default is `claude_cli`. `create_app(llm_factory=..., results_dir=..., tool_backoff_s=..., max_parallel_runs=2)` in `api.py` makes all of this injectable. The tests use `MockLLM` and `tool_backoff_s=0`.
- **Endpoints:**
  - `POST /investigations` takes `{"incident_id": "inc01", "mode": "multi"}`, or `"incident_report": {...}` in place of `incident_id`. Exactly one of the two is allowed, and extra fields are rejected. It returns `202 {"id": "<hex>"}`.
  - `GET /investigations/{id}` returns `{id, status: pending|running|done|failed, mode, incident_id, created_at, finished_at, report, blackboard, error}`. `report` and `blackboard` (the full trace) are set when the status is `done`. `error` (`<ExceptionType>: <message>`) is set when it is `failed`.
  - `GET /incidents` returns `[{id, title, affected_service, severity}]`, read from the incident reports only.
- **Runs:** a `ThreadPoolExecutor` (2 runs at once) calls the same `investigate()` the CLI uses. The LLM client is built and preflighted inside the run, so a broken backend ends as `failed`, not as a 500. Any exception ends as `failed`. State lives in memory and in `<results_dir>/<id>.json`, written atomically at each status change. On startup the app reads that folder back. It marks a run that a restart left `pending` or `running` as `failed`.
- **Inline incident reports:** `investigate_report(report, llm, ...)` in `investigate.py` uses the same run code as `investigate()` (both call `_run`). No telemetry stands behind an inline report, so the tools query an empty scenario. Every tool returns no evidence, and the run ends `INCONCLUSIVE` under the incident ID `inline`. With the mock LLM, the Reasoning agent (or the baseline) also records an `AgentFailure`, because it has no evidence to cite. This is honest, but it does not help real triage. Support for uploaded telemetry would need a new ticket.
- **Errors:** an unknown `incident_id` returns 404 before anything is stored or submitted. An invalid body returns 422 (FastAPI/Pydantic). An unknown investigation ID returns 404.
- **Same report shape:** `tests/test_api.py` runs inc01 through the CLI and through the API with the mock. It asserts that the two reports are equal except for `metrics.latency_s`.
- **Isolation test:** `api.py` joined the `EXAMPLES` allowlist for the `e.g. inc01` field description that Swagger shows.
- **Tests:** `tests/test_api.py` (17 tests). 287 tests pass, and pyright is clean. A manual smoke run of `uv run api` served `/docs` (200), `/incidents` and the 404 path. It did not start a real-LLM run.
- **Open:** there is no cancel endpoint, and a running thread cannot be stopped. The run limits (600 s wall clock) still bound it. The blackboard is available only when the run ends, because `investigate()` returns it at the end. There is no live trace while the status is `running`.

**2026-11-13, API key only (ticket 14):**
- The API's default backend is now `anthropic_api` for every role, so `uv run api` needs `ANTHROPIC_API_KEY` in its environment, and every investigation it starts is billed to that key.
