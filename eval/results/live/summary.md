# Evaluation summary: run `live`

- Generated: 2026-11-13 07:30 UTC
- LLM: models.toml
- Pairs complete: 20 of 20

## Aggregate (mean per incident unless noted)

| System | Pairs | Accuracy | Grounding (mean) | Citations (grounded/total) | Tool calls | LLM calls | Tokens in | Tokens out | Cache tokens | Latency s | Judge |
|---|---|---|---|---|---|---|---|---|---|---|---|
| multi | 10 | 8/10 (80%) | 0.997 | 812/814 | 23.2 | 35.2 | 105,068 | 33,017 | 0 | 185.0 | ES 3.90 · FA 3.10 · RA 4.60 · trap 1/10 |
| single | 10 | 9/10 (90%) | 1.000 | 307/307 | 8.0 | 10.0 | 26,371 | 4,478 | 0 | 53.2 | ES 4.10 · FA 4.30 · RA 4.20 · trap 1/10 |

## Totals

| System | Tool calls | LLM calls | Tokens in | Tokens out | Cache tokens | Latency s |
|---|---|---|---|---|---|---|
| multi | 232 | 352 | 1,050,678 | 330,173 | 0 | 1,850.2 |
| single | 80 | 100 | 263,713 | 44,783 | 0 | 532.0 |

## Per incident

| Incident | System | Expected | Outcome | Top hypothesis | Correct | Grounding | Tool calls | LLM calls | Tokens in | Tokens out | Cache tokens | Latency s | Ended by | Judge |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| inc01 | multi | checkout-svc / bad_deploy | ROOT_CAUSE | checkout-svc / bad_deploy | yes | 1.000 (61/61) | 22 | 33 | 86,403 | 26,550 | 0 | 151.0 | done | ES 4 · FA 3 · RA 5 · trap no |
| inc01 | single | checkout-svc / bad_deploy | ROOT_CAUSE | checkout-svc / bad_deploy | yes | 1.000 (28/28) | 6 | 8 | 17,397 | 3,151 | 0 | 38.8 | done | ES 4 · FA 5 · RA 5 · trap no |
| inc02 | multi | orders-api / config_change | ROOT_CAUSE | orders-api / config_change | yes | 1.000 (76/76) | 15 | 23 | 56,682 | 20,281 | 0 | 84.3 | done | ES 4 · FA 4 · RA 5 · trap no |
| inc02 | single | orders-api / config_change | ROOT_CAUSE | orders-api / config_change | yes | 1.000 (39/39) | 6 | 8 | 17,532 | 3,511 | 0 | 39.2 | done | ES 5 · FA 5 · RA 5 · trap no |
| inc03 | multi | avatar-store / dependency_failure | ROOT_CAUSE | avatar-store / dependency_failure | yes | 1.000 (81/81) | 27 | 39 | 108,728 | 32,466 | 0 | 150.7 | done | ES 4 · FA 3 · RA 4 · trap no |
| inc03 | single | avatar-store / dependency_failure | ROOT_CAUSE | avatar-store / dependency_failure | yes | 1.000 (31/31) | 6 | 8 | 17,735 | 4,572 | 0 | 48.6 | done | ES 5 · FA 5 · RA 4 · trap no |
| inc04 | multi | image-resizer / capacity | ROOT_CAUSE | image-resizer / capacity | yes | 0.972 (70/72) | 30 | 46 | 142,110 | 42,783 | 0 | 241.9 | tool_budget | ES 4 · FA 3 · RA 5 · trap no |
| inc04 | single | image-resizer / capacity | ROOT_CAUSE | image-resizer / capacity | yes | 1.000 (36/36) | 14 | 16 | 46,002 | 5,707 | 0 | 75.2 | done | ES 4 · FA 4 · RA 5 · trap no |
| inc05 | multi | ledger-api / cert_dns | ROOT_CAUSE | ledger-api / cert_dns | yes | 1.000 (44/44) | 15 | 22 | 49,511 | 15,392 | 0 | 78.2 | done | ES 4 · FA 3 · RA 4 · trap no |
| inc05 | single | ledger-api / cert_dns | ROOT_CAUSE | ledger-api / cert_dns | yes | 1.000 (35/35) | 9 | 11 | 26,639 | 4,383 | 0 | 57.9 | done | ES 4 · FA 5 · RA 5 · trap no |
| inc06 | multi | inventory-db / db_migration | ROOT_CAUSE | inventory-db / db_migration | yes | 1.000 (112/112) | 23 | 34 | 105,500 | 30,254 | 0 | 158.2 | done | ES 4 · FA 3 · RA 5 · trap no |
| inc06 | single | inventory-db / db_migration | INCONCLUSIVE | — | no (outcome_mismatch) | n/a (0/0) | 5 | 7 | 20,053 | 4,581 | 0 | 43.8 | done | ES 1 · FA 1 · RA 1 · trap no |
| inc07 | multi | cart-svc / retry_storm | INCONCLUSIVE | cart-svc / retry_storm | no (outcome_mismatch) | 1.000 (91/91) | 30 | 47 | 151,751 | 50,895 | 0 | 295.5 | tool_budget | ES 4 · FA 3 · RA 4 · trap no |
| inc07 | single | cart-svc / retry_storm | ROOT_CAUSE | cart-svc / retry_storm | yes | 1.000 (41/41) | 8 | 10 | 24,532 | 4,403 | 0 | 57.9 | done | ES 4 · FA 5 · RA 4 · trap no |
| inc08 | multi | recs-svc / feature_flag | ROOT_CAUSE | recs-svc / feature_flag | yes | 1.000 (65/65) | 11 | 18 | 43,282 | 15,111 | 0 | 84.9 | done | ES 4 · FA 4 · RA 5 · trap no |
| inc08 | single | recs-svc / feature_flag | ROOT_CAUSE | recs-svc / feature_flag | yes | 1.000 (20/20) | 6 | 8 | 17,837 | 3,530 | 0 | 39.7 | done | ES 5 · FA 5 · RA 5 · trap no |
| inc09 | multi | INCONCLUSIVE | INCONCLUSIVE | notification-svc / bad_deploy | yes | 1.000 (83/83) | 30 | 46 | 132,588 | 46,335 | 0 | 290.1 | tool_budget | ES 3 · FA 2 · RA 4 · trap yes |
| inc09 | single | INCONCLUSIVE | INCONCLUSIVE | notification-svc / bad_deploy | yes | 1.000 (23/23) | 9 | 11 | 26,299 | 4,432 | 0 | 49.3 | done | ES 4 · FA 4 · RA 3 · trap yes |
| inc10 | multi | parcelhub-api / third_party_limit | INCONCLUSIVE | parcelhub-api / third_party_limit | no (outcome_mismatch) | 1.000 (129/129) | 29 | 44 | 174,123 | 50,106 | 0 | 315.4 | done | ES 4 · FA 3 · RA 5 · trap no |
| inc10 | single | parcelhub-api / third_party_limit | ROOT_CAUSE | parcelhub-api / third_party_limit | yes | 1.000 (54/54) | 11 | 13 | 49,687 | 6,513 | 0 | 81.8 | done | ES 5 · FA 4 · RA 5 · trap no |

## Judge scores

An LLM judge (the `judge` role in models.toml, its own prompt) read each report with the answer key and every cited evidence row. It scores evidence support, fact/assumption separation and recommended actions from 1 to 5 (means over the pairs it judged), and says whether the system fell for a planted trap (count of yes over judged pairs). **The judge never decides accuracy**: accuracy above is deterministic, so a judge mistake cannot move it. A judge failure is a pair whose judge output stayed invalid after one retry; it is counted, not scored.

| System | Judged | Evidence support | Fact/assumption separation | Actions | Fell for trap | Judge failures |
|---|---|---|---|---|---|---|
| multi | 10 of 10 | 3.90 | 3.10 | 4.60 | 1/10 | 0 |
| single | 10 of 10 | 4.10 | 4.30 | 4.20 | 1/10 | 0 |

### Judge cost (not charged to either system)

The judge's own LLM calls, tokens and latency (D6). They are not in any of the system metrics above.

| Results judged for | LLM calls | Tokens in | Tokens out | Cache tokens | Latency s |
|---|---|---|---|---|---|
| multi | 10 | 153,823 | 12,364 | 0 | 125.8 |
| single | 10 | 54,360 | 9,931 | 0 | 105.9 |

- **Bias (R5):** the judge is a Claude model, like the systems it grades, and may favour their style. Read its scores as a second opinion on reasoning quality, not as ground truth.

## How to read this

- **Each system ran once per incident** (Q11). There are no repeated trials, so a single-incident difference is one sample, not a trend.
- **Accuracy:** the top-ranked hypothesis matches the answer key's service and category, with outcome ROOT_CAUSE. Inconclusive-by-design incidents are correct only when the outcome is INCONCLUSIVE; a root-cause incident answered INCONCLUSIVE is incorrect.
- **Grounding:** the share of cited evidence IDs (Facts and hypothesis supporting/contradicting IDs, plus the IDs the evidence rules removed from the report) that exist in the scenario. n/a means the report cited nothing; the mean skips it.
- **Tokens:** input (uncached input + cache writes), output and cache reads are shown separately (D5). The token budget excludes cache reads.
