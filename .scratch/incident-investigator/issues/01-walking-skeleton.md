# 01: Walking skeleton: one incident, mock LLM, report on disk

**What to build:** A runnable end-to-end path through the whole system with the thinnest possible content. A developer runs the investigate CLI on one small incident with a mock LLM backend. A minimal LangGraph graph calls the logs tool once and writes a schema-valid InvestigationReport. This fixes the project layout, the core Pydantic schemas (Scenario, Evidence, ToolResult, Finding, Hypothesis, InvestigationReport; see the PRD's data model sketch), and the test harness that every later ticket builds on.

**Blocked by:** None (can start immediately)

**Status:** done

Context: `incident-investigator-ai-ready-prd.md` (data model sketch, step 8), `DECISIONS.md` Q1, Q4, Q22.

- [x] Python 3.12 project managed with `uv`. LangGraph, Pydantic v2 and pytest versions pinned in the lockfile. `git init` with a first local commit, and no remote (Q22)
- [x] The core schemas from the PRD's data model sketch exist and validate
- [x] One small incident (inc01) and its answer key are stored separately. The answer key is never loaded by agent code
- [x] The logs tool returns evidence rows with stable IDs in the form `log:inc01:NNNN`
- [x] Running the investigate command on inc01 with the mock LLM exits 0 and writes a schema-valid InvestigationReport JSON
- [x] An unknown incident ID exits non-zero with a clear message and writes no report
- [x] `pytest` is green

## Comments

**2026-11-12, implementation review (fixed in this ticket):** the evidence-ID validator now enforces `<kind>:<incident>[:<series>]:NNNN`, and all scenario/query datetimes must be timezone-aware.

**Deferred to later tickets:**
- 03: `dependencies` rows have no evidence IDs yet, but `Evidence.kind` allows `dependency`. Invalid tool input must return `status=error`, not raise.
- 04: `Blackboard` sits in `graph.py` and holds the whole `Scenario`. Reshape it to the PRD shape (incident, tasks, findings, hypotheses, gaps, failures, counters) so agents learn facts only through tools. Give `Counters` an `add(usage)` method instead of adding fields one by one.
- 08/09: `MockLLM` hard-codes `category="bad_deploy"` and cites the first 3 IDs it sees, including INFO rows. Don't use mock runs for grounding scores. The answer-key isolation test only greps for names, so tighten it when the "no hardcoding" test lands. The outcome is spelled `root_cause` in the answer key but `ROOT_CAUSE` in the report, so scoring must map between them.
