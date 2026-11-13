# 08: The full 10-incident synthetic dataset

**What to build:** The evaluation set. The remaining 9 hand-written incidents are added, each with 2–3 traps (misleading evidence, conflicting observations, tool failures), a separate answer key and a deterministic failure schedule. One incident is inconclusive by design. Tests guard against answer leakage and hardcoding.

**Blocked by:** 03 (all tools)

**Status:** done

Context: PRD data model (AnswerKey, D7), steps 1 and 11, R6. `DECISIONS.md` Q5, Q18.

- [x] There are 10 scenarios covering the 10 cases listed in `DECISIONS.md` Q18, including inc09 (inconclusive by design) and inc10 (deploy red herring with a third-party rate limit)
- [x] Every answer key validates. Every key and misleading evidence ID exists in its scenario
- [x] Each answer key names its traps and the evidence IDs that make up each trap
- [x] Across the set, every tool fails at least once, and every failure mode (timeout, partial, error) appears
- [x] A test proves that no agent or baseline code path reads the answer keys
- [x] A test greps the source for incident IDs and answer-key values and finds none outside the data loader

## Comments

**2026-11-12, implementation:**
- Data: `data/incidents/inc02..inc10.json` and `data/answer_keys/inc02..inc10.json`, hand-written in inc01's style. inc01 keeps all its IDs; it gains `log:inc01:0008`, `log:inc01:0009`, `metric:inc01:pay:0001` and a second trap. No schema change: `AnswerKey.traps` already fits. The 9 solvable incidents use all 9 categories once.
- Failure schedules cover the full tool x mode matrix (a test enforces this). Each failure hits one call only, so re-querying recovers the evidence. inc04 times out the first two logs calls in a row; the memory metrics carry the case without logs.
- Tests: `tests/test_dataset.py` validates all 10 keys and scenarios, evidence IDs, 2-3 traps, coverage, inc09/inc10, and a giveaway-word leak check on each report. `tests/test_answer_key_isolation.py` adds: no `DATA_DIR` or `"data" /` path building outside `dataset.py`; an AST check that other modules import only `load_scenario`, `list_incident_ids` and `UnknownIncidentError` from the dataset module; a runtime check that runs `investigate()` on all 10 incidents with `MockLLM` while tripwires watch `open`, `Path.open`, `Path.read_text` and `load_answer_key`; and the step-11 grep for literal IDs `inc01`..`inc10` and answer-key root-cause service names.
- The step-11 grep has one reviewed exception: the `cli.py` help text `e.g. inc01` (cli.py is out of scope here). It matches literal IDs only, so `llm/mock.py`'s `inc\d{2}` regex passes. I changed one comment in `schemas.py` from `inc01` to `incNN`.
- For 09: inc10's answer service is `parcelhub-api`, the third party that sets the limit, not the caller `shipping-svc`. inc07's is `cart-svc`, the zero-backoff retrier, not the alerted `storefront` or the overloaded `pricing-svc`. Scoring may want to give partial credit for the caller.

**Incidents for realism review** (root cause; traps):
- inc01: checkout-svc bad_deploy (v2.3.1 NPE in discount code). Traps: noisy-neighbour CPU spike plus a CPU past incident; payments-svc TLS bump plus a slow handshake log.
- inc02: orders-api config_change (`db.pool.max_connections` 50 to 5). Traps: deploy v4.12.0 with a heavy export job plus a release past incident; orders-db autovacuum plus vacuum tuning plus a past incident (DB CPU 18%).
- inc03: avatar-store dependency_failure (read-only volume, shard down). Traps: profile-svc v1.8.0 plus a renderer panic plus a past incident; profile-svc pod restarts; avatar-store green shallow health check.
- inc04: image-resizer capacity (buffer pool grows for 117 h, then OOMKills). Logs time out twice. Traps: thumbnail-cache deploy and cold start; oversized-upload rejections plus a bad-input past incident.
- inc05: ledger-api cert_dns (TLS cert expired at midnight, renewer lock stuck). Traps: mobile-bff HTTP-client upgrade plus handshake retries plus a past incident; `tls_min_version` 1.2 to 1.3 change 30 min before.
- inc06: inventory-db db_migration (0142 dropped an index before 0143 ran, so seq scans). The first metrics call is partial and shows only healthy samples. Traps: flash sale, autoscaling and a traffic past incident; inventory-svc v2.9.0 cold cache; partial healthy metrics.
- inc07: cart-svc retry_storm (5 retries, 0 ms backoff, about 17x amplification onto pricing-svc after an 18 s promo-db failover). Alert on storefront. Traps: pricing-svc CPU 99% and GC plus a past incident; storefront v12.3.0; the promo-db failover.
- inc08: recs-svc feature_flag (ranker_v2 enabled only in eu-west-1, whose feature-store lacks a feature). Traps: eu-west-1 packet loss plus a past partition incident; v6.0.0 ranker deploy; cache size change in all regions.
- inc09: inconclusive. Notification lag while the log shipper dropped 44 min of logs; recovery by consumer rebalance. Traps: v3.3.0 deploy, broker `max_inflight` change, one push-provider 429.
- inc10: parcelhub-api third_party_limit (600/min plan cap hit after a 1.2M-recipient campaign). Traps: shipping-svc v5.2.0 deploy plus a release past incident (the partial deploys call hides the rollback); the error dip after the rollback; the provider status page says operational.
