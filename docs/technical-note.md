# Technical note

## Key decisions

- **LangGraph state as a blackboard.** Agents never message each other; they write typed entries that reducers merge.
- **Hybrid Orchestrator.** One Opus call plans a task DAG; Python runs it in parallel waves, keeping scheduling and limits deterministic.
- **Claude through the API key only.** A subscription-CLI backend was removed as too fragile for an unattended service (ADR 0002). Agents reply with validated JSON actions.
- **Code, not the LLM, owns confidence.** Facts must cite evidence IDs a tool returned. Unsupported hypotheses cap at 0.5, each open evidence gap subtracts 0.1, and below 0.6 the outcome is `INCONCLUSIVE`.
- **Reproducible failures.** Scenarios inject timeouts, partial results and errors on a fixed tool call.
- **A fair baseline.** One Opus agent with the same tools, failures, limits, evidence rules and report schema.
- **Deterministic accuracy.** Accuracy and grounding come from answer keys. An Opus judge scores reasoning quality, never accuracy.

## Results (10 incidents, one run each)

| | Accuracy | Grounding | LLM calls | Tokens | Judge: evidence / separation / actions |
|---|---|---|---|---|---|
| Multi-agent | 8/10 | 0.997 | 35 | 138k | 3.9 / 3.1 / 4.6 |
| Single-agent | 9/10 | 1.000 | 10 | 31k | 4.1 / 4.3 / 4.2 |

All 20 reports ranked the right service and category first, and both systems answered the inconclusive incident correctly. Multi-agent missed inc07 and inc10 with the right answer under 0.6: the Critic kept challenging what drove the load and Reasoning lowered its confidence. Single-agent missed inc06 because its final answer had an extra JSON field and was rejected. On these incidents, multi-agent cost about 4.5 times the tokens without better accuracy. Its reports keep stale assumptions from earlier rounds.

Reading live runs drove fixes: closing recovered gaps, stopping on a rejected key, salvaging an agent's work at its tool limit, and letting the tool budget stop only tool calls. The last let an inc04 run overturn the deploy trap.

## Trade-offs

- Multi-agent makes more calls, each resending its system prompt; the Critic buys scrutiny at the price of caution.
- Hand-written incidents give reliable answer keys, but only 10, written alongside the system they test.

## Limitations

- **One run per incident,** without significance tests.
- **The judge is a Claude model,** like the systems it grades, and sees the answer key.
- Unknown JSON fields reject a whole answer; stale assumptions are never pruned.
- A gap closes when the same tool later succeeds for the same service, even over another window.
- inc07 and inc10 have contestable answer keys, with no partial credit.
- No prompt caching; the API cannot cancel a run or stream its trace.

## Possible improvements

- Repeat each incident and report variance.
- Drop unknown fields and prune answered assumptions.
- Add a judge from another model family, calibrated against human ratings.
- Add incidents written by someone else.
