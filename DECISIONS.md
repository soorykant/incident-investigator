# Design Decisions — Multi-Agent Incident Investigation System

Source brief: `orange_invest_assignment.md`

How to use this file: each question has a recommended answer. Write your answer on the `Answer:` line.
"yes" accepts the recommendation. Settled answers stay here as the decision record and later feed the 500-word technical note.

---

## Round 1

### Q1 — Language
Should the system be written in Python?
The alternatives are TypeScript or another language. Python has the most mature agent libraries and makes evaluation and plotting easy. A Python 3.12 venv already exists here.

**Recommended:** Yes, Python 3.12, managed with `uv`.

Answer: **Yes.** Python 3.12 with `uv`.

---

### Q2 — LLM provider
Should we use Anthropic Claude?
One option is a strong model for the Reasoning and Critic agents and a cheaper one for Observability, Dependency and Reporting. That split also gives the token and latency comparison something interesting to show.
The alternatives are OpenAI, Gemini, or a local model through Ollama, which is free but weaker and slower.

**Recommended:** Yes, Claude: Sonnet 5.5 for Reasoning and Critic, Haiku 5.5 for the other agents. The baseline uses Sonnet.

Answer: **Yes, Claude.** Use Opus 5.5 where it is needed. Which agents get which model is decided in round 2 (Q8).

---

### Q3 — Time budget and depth
Is this a 3–5 day submission where depth beats breadth?
That means all 6 agents, 10–12 incidents and a solid evaluation, with only a CLI and no extras like a UI or a vector database for past incidents.
Or is there a hard deadline or a smaller budget that should shrink the scope?

**Recommended:** Yes, about 3–5 days with a CLI only. Past incidents are found by keyword or tag search, not embeddings.

Answer: **No, the budget is smaller.** Finish today, overnight at most. v1 is a CLI only, with no UI. A FastAPI layer with Swagger may come later if needed.

---

### Q4 — How agents collaborate
Should they use a shared "blackboard"?
Every agent reads from and writes typed findings to one shared investigation state. The Orchestrator runs a list of tasks with dependencies between them.
The Critic can add follow-up tasks, such as "check metrics for svc-B between 10:00 and 10:15", which sends the investigation back through the loop.
The alternative is free-form messages between agents, which is harder to bound and test.
The brief says simply chaining calls isn't enough, and this model directly shows shared state, task dependencies and feedback loops.

**Recommended:** Yes, a blackboard with typed Pydantic findings, a task DAG, and a Critic that can add tasks.

Answer: **Yes.** Blackboard, task DAG, and a Critic that can add tasks.

---

### Q5 — Simulated data
Should each test incident be a hand-written JSON scenario?
Each scenario contains the logs, metrics, deploys, dependency graph and past incidents. The answer key sits in a separate file the agents never see.
Each scenario also sets which tools fail, how (a timeout or a partial result) and when, so the failures can be reproduced.
The alternative is having an LLM generate the data, which is less controllable and makes the answer key unreliable.

**Recommended:** Yes, hand-written scenarios with a separate answer key and failures injected on a fixed schedule.

Answer: **Yes.** Hand-written JSON scenarios, a separate answer key, and failures injected on a fixed schedule.

---

### Q6 — Fair baseline
Should the single-agent baseline get the same tools, the same model and the same tool-call and token limits?
It is one agent working in a loop that must produce the same report schema. If its limits are set differently, the comparison is easy to attack.

**Recommended:** Yes: same tools, same limits, same output format, and Sonnet for the single agent.

Answer: **Undecided.** Asked again in round 2.

---

### Q7 — Recording decisions
Should settled decisions be recorded in this file (`DECISIONS.md`)?
A short glossary section would be added at the end for terms like finding, hypothesis, evidence and fact vs assumption.

**Recommended:** Yes, this single file is the decision record.

Answer: **Yes.** This file, with a glossary at the end.

---

## Round 2

### Q6 (asked again) — Why the baseline matters
The brief says to compare the multi-agent system with a single-agent baseline that uses the same tools. A reviewer will look for a fair comparison.
- **Fair:** one agent in a loop, with the same tools, the same model as the strongest agent, the same tool-call and token limits, and the same report format. Any difference in results then comes from the architecture, not the setup.
- **Unfair:** the baseline gets a weaker model or fewer tool calls. The multi-agent system wins, but a reviewer will call it rigged.

**Recommended:** Fair. The baseline is one Opus 5.5 agent with the same tools, the same 30-tool-call limit and token limit, and the same `InvestigationReport` schema.

Answer: **Yes.** A fair baseline: Opus 5.5, the same tools, limits and schema.

### Q8 — Which model each agent uses
**Recommended:** Opus 5.5 for Reasoning and Critic, where the hard thinking happens. Haiku 5.5 for Observability, Dependency and Reporting, which mostly call tools and summarise. The Orchestrator does not use an LLM (see Q9).

Answer: **Yes.** Opus for Reasoning and Critic, Haiku for Observability, Dependency and Reporting.

### Q9 — Orchestrator: code or LLM
**Recommended:** A hybrid. One Opus call turns the incident into a typed task plan. Plain Python then runs the task DAG: it runs independent tasks in parallel, retries, and enforces limits. The Orchestrator re-plans only when the Critic adds tasks. This is cheaper and predictable, and it still counts as planning.

Answer: **Yes.** Hybrid: one LLM call plans, plain Python runs the DAG.

### Q10 — Framework
Options: the plain Anthropic SDK with `asyncio` and Pydantic; LangGraph; the Claude Agent SDK.

**Recommended:** The plain Anthropic SDK with `asyncio` and Pydantic. You get full control over the blackboard, parallel runs and token counting, with no framework to learn overnight.

Answer: **LangGraph or CrewAI.** The choice between them is in round 3 (Q14).

### Q11 — Scope for an overnight build
**Recommended:**
- Exactly 10 incidents, each with a small amount of data.
- Each system runs once per incident.
- `pytest` covers the tools, failure injection, the DAG and scoring. It mocks the LLM.
- The architecture diagram is a Mermaid diagram in the README.
- The FastAPI layer is cut from v1.

Answer: **Keep it minimal, but include both the CLI and an API in v1.** Incidents should be fairly complex, as the brief describes (misleading evidence, conflicts, tool failures).

### Q12 — How results are scored
**Recommended:** The answer key holds the root-cause service, the category (for example `bad_deploy`, `config_change`, `dependency_failure` or `capacity`) and the IDs of the important evidence.
- **Accuracy:** the top-ranked hypothesis matches both the service and the category. Scenarios that should end inconclusive count as correct when the system returns inconclusive.
- **Grounding:** the share of cited evidence IDs that exist in the scenario data. A citation that points to nothing counts against it.
- **Cost:** tool-call count, latency and tokens are measured directly.
- No LLM judge is used.

Answer: **Structured scoring plus an LLM judge.** The judge's details are in round 3 (Q17).

### Q13 — Run cost and API key
A full evaluation run is 10 incidents × 2 systems, mostly on Opus. A rough estimate is a few dollars per full run.
Do you have an Anthropic API key, and is a budget of about $10–20 for development and evaluation runs OK?

**Recommended:** Yes. The key is read from the `ANTHROPIC_API_KEY` environment variable, and a `--max-cost` guard stops a run that goes over budget.

Answer: **No API key for now.** Use the Claude subscription, and add an API key only if needed. Don't worry about cost.

---

## Round 3

### Q14 — LangGraph or CrewAI
**Recommended: LangGraph.**
- **Shared state:** a LangGraph graph's typed state is the blackboard.
- **Parallel work:** `Send` fans out Observability and Dependency at the same time.
- **Critic loop:** a conditional edge from the Critic goes back to the Orchestrator.
- **Limits:** `recursion_limit` and step counters bound the run.

CrewAI is built around roles and tasks and hides more of the control flow. That makes it harder to show task dependencies, bounded retries and termination conditions, which the brief grades.

Answer: **Yes.** LangGraph.

### Q15 — Using the Claude subscription from code
LangGraph's normal Claude integration (`langchain-anthropic`) needs an API key and cannot use a subscription. To use the subscription, LLM calls go through the Claude Code CLI (`claude -p --output-format json`), which is logged in with your subscription and reports token usage.
That route does not support native tool calling. So each agent returns a structured JSON "action" (`call_tool` with arguments, or `finish` with findings), and the LangGraph node runs the tool in Python and loops. The baseline works the same way.

**Recommended:** Yes:
- A small `LLMClient` interface with two backends: `claude_cli`, the default, which uses the subscription, and `anthropic_api`, an optional switch to an API key.
- Every agent uses JSON actions validated by Pydantic.
- Note: a call through the CLI has a few seconds of extra latency. This is the same for both systems, so the comparison stays fair, and it is documented as a limitation.

Answer: **Yes**, and also allow a locally hosted model. Details in round 4 (Q20).

### Q16 — Investigation limits and termination
**Recommended:**
- **Agents:** at most 6 tool calls per agent task.
- **Critic loop:** at most 2 loops. After that the Reporting agent must report with what it has.
- **Whole run:** a limit of 30 tool calls and a wall-clock timeout.
- **Tool failures:** retry 2 times with backoff, then record an `EvidenceGap` on the blackboard, which lowers confidence.
- **Agent failures:** if an agent crashes or returns invalid JSON 2 times, record an `AgentFailure` and continue without it.
- **Inconclusive:** when no hypothesis reaches 0.6 confidence, the outcome is `INCONCLUSIVE`.

Answer: **Yes.**

### Q17 — LLM judge
**Recommended:** Two layers.
- **Deterministic:** root-cause match (service and category), whether cited evidence IDs exist, tool calls, latency and tokens.
- **LLM judge** (Opus, a separate prompt, given the answer key and the cited evidence):
  - Does the evidence actually support each claim? (1–5)
  - Are facts kept separate from assumptions? (1–5)
  - Are the recommended actions sensible? (1–5)
  - Did the system fall for the planted misleading evidence? (yes/no)

The judge never decides root-cause accuracy. That stays deterministic, so a judge mistake can't change the main metric.

Answer: **Yes.**

### Q18 — Incident set (10, fairly complex)
**Recommended:** Each incident combines 2–3 traps.
1. A bad deploy, plus a coincidental noisy-neighbour CPU spike (misleading).
2. A config change (connection pool) that a recent deploy seems to explain (misleading).
3. A downstream dependency outage that looks like the service failing (conflicting).
4. Memory leak or capacity exhaustion, with the logs tool timing out (tool failure).
5. A certificate or DNS expiry, where the past incident points to the wrong cause.
6. A database slow query after a schema migration, with metrics only partly returned.
7. A retry storm that cascades across 3 services, where the alert fires on the wrong service.
8. A feature-flag rollout that hurts only one region (conflicting observations).
9. **Inconclusive by design:** key logs are missing and the evidence is ambiguous.
10. **Red herring:** a deploy happened, but the real cause is a third-party rate limit.

Answer: **Yes.**

### Q19 — API shape
**Recommended:** FastAPI with Swagger at `/docs`.
- `POST /investigations` with `{incident_id | incident_report, mode: multi|single}` returns `{id}`. The investigation runs as a background task.
- `GET /investigations/{id}` returns its status, the blackboard trace and the final report.
- `GET /incidents` lists the incidents in the dataset.
- Results are stored in memory plus a JSON file. There is no database.

The CLI and the API call the same `investigate()` function.

Answer: **Yes.**

---

## Round 4

### Q20 — Local model backend
**Recommended:**
- Add a third `LLMClient` backend, `openai_compat`. It works with Ollama, LM Studio or vLLM through their OpenAI-compatible endpoint, and is chosen per agent in a config file such as `models.yaml`.
- The published evaluation uses Claude, through the subscription CLI. A local model is optional, and is run for the cheap agents only if time allows.
- Calls to the LLM run at most 3 at a time, so the subscription doesn't hit rate limits.

Answer: **Yes.**

### Q21 — Facts, assumptions and confidence
**Recommended:**
- Every tool result row gets a stable evidence ID, for example `log:inc04:0042` or `metric:inc04:p99:1012`.
- A **Fact** must cite at least one evidence ID that exists. The code checks this.
- An **Assumption** is an inference. It names what it rests on and how it could be checked.
- Each hypothesis lists supporting and contradicting evidence IDs.
- The Reasoning agent proposes a confidence. The code then caps it at 0.5 if the hypothesis has no supporting Facts, and lowers it by 0.1 for each open `EvidenceGap` that affects it.

Answer: **Yes.**

### Q22 — Repo and submission
**Recommended:**
- `git init` in this folder, with small commits during the build.
- Submit as a GitHub repo, or a zip of it.
- Layout: `src/incident_investigator/` (agents, graph, tools, llm, api, cli), `data/incidents/`, `data/answer_keys/`, `eval/`, `tests/`, `docs/`, `README.md`.

Answer: **Yes, but local only.** `git init` and commit locally. Don't push or create a remote; how to submit is decided at the end.

### Q23 — Coding-assistant transcript
**Recommended:** At the end, copy this Claude Code session log (`~/.claude/projects/…/*.jsonl`) into `docs/transcript/`, along with a Markdown version that is easy to read.

Answer: **Yes, and export after every turn**, so the transcript is never lost to context compaction. This needs a Claude Code hook.

### Q24 — Build order for an overnight run
**Recommended:**
1. Schemas, incident data and tools, including failure injection.
2. The `LLMClient` backends.
3. The LangGraph multi-agent graph.
4. The single-agent baseline.
5. Evaluation (deterministic scoring and the judge) and the results report.
6. The CLI and the FastAPI layer.
7. README, the Mermaid diagram, the technical note and the transcript.

`pytest` tests are written alongside each step.
If time runs short, the first thing to cut is the local-model evaluation, then the judge's "recommended actions" score. The CLI, API, evaluation and docs are never cut.

Answer: **Not decided here.** The user's own skill turns these decisions into a PRD and then into issues and tasks, which set the build order.

---

## Round 5

### Q25 — Drop the subscription, use only the API key (2026-11-13)
Calling Claude through the subscription CLI (`claude -p`, Q15) caused problems in use. Should the system keep it?

Answer: **No.** Remove the subscription CLI backend. The system depends only on the API key (`ANTHROPIC_API_KEY`), which is the right solution for this kind of application. This replaces the Q13 answer ("no API key for now") and the subscription parts of Q15 and Q20. JSON actions stay, so every backend speaks the same protocol. See ADR 0002.

---

## Summary of settled design

| Area | Decision |
|---|---|
| Language | Python 3.12, `uv` |
| Framework | LangGraph |
| LLMs | Claude through the Anthropic API (`ANTHROPIC_API_KEY`) by default; a local model (OpenAI-compatible) as an optional backend, chosen per agent in `models.toml` (Q25; the subscription CLI was removed) |
| Models | Opus 5.5 for Reasoning, Critic, plan creation, the baseline and the judge; Haiku 5.5 for Observability, Dependency and Reporting |
| Agent I/O | JSON actions (`call_tool` / `finish`) validated by Pydantic; tools run in graph nodes |
| Coordination | Typed blackboard (graph state), task DAG, parallel fan-out with `Send`, Critic adds tasks and loops back |
| Orchestrator | Hybrid: one LLM call plans, Python runs the DAG |
| Limits | 6 tool calls per agent task, 2 Critic loops, 30 tool calls per run, a wall-clock timeout, 2 retries with backoff → `EvidenceGap`, 2 agent failures → `AgentFailure`, confidence < 0.6 → `INCONCLUSIVE` |
| Evidence | Stable evidence IDs; Facts must cite real IDs; Assumptions name their basis and how to check them; confidence capped and lowered by code |
| Data | 10 hand-written JSON incidents with 2–3 traps each; separate answer keys; failures injected on a fixed schedule |
| Baseline | One Opus agent with the same tools, limits and report schema |
| Evaluation | Deterministic: accuracy, grounding, tool calls, latency, tokens. Opus judge: support, fact/assumption separation, action quality, "fell for the trap" |
| Interfaces | CLI and FastAPI (Swagger at `/docs`), sharing one `investigate()` |
| Repo | Local git only; how to submit is decided later |
| Transcript | Exported to `docs/transcript/` after every turn by a hook |
| Next step | The user's PRD skill, then issues and tasks |

## Glossary

- **Incident:** the input report, with symptoms, the affected service and a time window.
- **Evidence:** one tool result row with a stable ID, such as `log:inc04:0042`.
- **Finding:** an agent's typed output on the blackboard, made of Facts and Assumptions.
- **Fact:** a statement that cites at least one existing evidence ID.
- **Assumption:** an inference that is not directly backed by evidence. It states its basis and how it could be checked.
- **Hypothesis:** a candidate root cause (a service and a category) with evidence for and against it and a confidence.
- **EvidenceGap:** data the investigation needed but could not get, because of a tool failure or missing data.
- **Blackboard:** the shared investigation state that every agent reads and writes.
- **Inconclusive:** the outcome when no hypothesis reaches 0.6 confidence.
