# HERA independent component review

Scope: current shared branch through 443ea20, reviewed against bcb0363. The reviewer authored the generic REST/stdio/MCP adapters and in-flight status candidate. The durable admission, projection, lease isolation, installation delegation adapter and Workshop wake implementations had separate authors. This review signs off only on the source paths and local tests named below. Production deployment, native Windows execution, multi-host authentication rates and mixed capacity targets require their own evidence.

| Finding | Evidence and disposition |
|---|---|
| Completion-scoped project recovery had different REST and MCP semantics | REST initially discarded its JSON body before calling generic project recovery. The MCP path verified the exact receipt and generation. Fixed in fc38dc3. The real SDK/REST regression sends a syntactically valid nonexistent completed receipt and verifies both adapters return VIEW_DENIED. Both can still invoke ordinary recovery with the original empty command. |
| Durable resource policy admitted nonstring mapping values | A direct PostgreSQL expression check on 0023 accepted numbers, objects and arrays in the role mapping. Such a policy could raise a database error on the next otherwise valid submission. The admission author and reviewer independently verified this. 1dc8c86 checks JSON type and name; the reviewer reran ten PG tests at the corrected revision. |
| Standalone legacy index worker uses another acquisition path | Before 443ea20 scripts/run_derived_worker.py called DerivedWorldWorker.run_once, taking an OutboxService lease outside project dispatch's CPU_COMPUTE and global slot accounting. Using the legacy and project workers side by side could exceed the stated resource bounds, despite inbox deduplication of formal effects. The resource author fixed this in 443ea20. With resource caps enabled, standalone and scoped workers now acquire the same admission gate and durable index lease. Independent PG regression verified a full compute pool blocks the standalone caller, reopening the pool permits processing, and replay preserves a single business effect. |
| Ingress and SQL budgets have different enforcement scopes | Main task/idempotency and admission policy are PostgreSQL-backed. HTTP authentication buckets, credential slot queues and rate limits are per process. Fleet connection declarations must include each API pool, supervisor pool, dedicated listener and each compute subprocess's one-connection pool. Multi-replica read trials establish fixed-budget behavior for the measured topology only. |

## Authority and state

* Both task create entry points reach TaskService.create_in_transaction. A matching original receipt is checked after current grant authentication and before capacity counting. The enabled singleton serializes concurrent occupancy decisions. The input, task, outbox and receipt share one commit. Old tasks count against enabled limits. Forward and reverse transport retries must retain the original spec and idempotency key.
* The new project progress path derives all counts from nf_tasks transitions in the same transaction. Its explicit backfill takes a lock that excludes task writes. The read applies quota retry time in its database snapshot and falls back to existing authoritative counts if no branch projection is installed. The watermark counts mutations of the aggregate. It does not identify a reconstructible global outbox position.
* Model sends use the existing attempt ledger. Lease recovery treats a SENT attempt with an unknown outcome as UNKNOWN and a held reservation. A repeated command with an existing receipt returns its original result. Actor-specific grants, dynamic role ceilings and knowledge cuts continue to govern the existing bound role APIs. Generic task metadata cannot return a knowledge bundle.
* An ordinary TaskSpec is available for an external worker to claim. Automatically dispatched model work uses the trusted delegation slot. Delegations._bind creates the task, binds its Supervisor config and adds its receipt in one transaction. The same installation verifies exact operation and receipt at Workshop settlement. PostgreSQL stores the durable wake intent. Restart and replay can deliver duplicate hints; inbox and generation guards constrain their business effect. No real provider charges were used in these tests.
* Both the publisher and consumer grouped Workshop paths perform network operations after PostgreSQL commit boundaries. The publisher can replay a lost Redis acknowledgement, and the consumer acknowledges its stream entries after committing the matching inbox transaction. The diagnostic stream and authoritative PostgreSQL transaction remain separate.

## Independent Linux verification

Real PostgreSQL 17 and Redis were launched on private random ports with the locked Python 3.12 environment. The test commands below are executable with the repository's test DSNs/fixtures configured. The Workshop relay test also requires AW_DATABASE_URL and AW_REDIS_TEST_URL. The first combined run omitted AW_DATABASE_URL, so the dual SDK tests passed and the relay fixture failed before the relay ran. Its corrected rerun passed. Check the [raw JUnit files](raw/).

| Pytest selections | Result |
|---|---|
| tests/integration/test_hera_durable_admission.py, test_hera_progress.py, test_hera_supervisor.py | 7 passed |
| tests/integration/test_hera_supervisor.py after standalone index worker fix | 2 passed |
| tests/integration/test_hera_durable_admission.py after resource schema fix | 10 passed |
| tests/service/test_hera_mixed_ingress.py after transport scope correction | 1 passed |
| workshop/integration/test_dual_stdio.py | 2 passed |
| workshop/tests/test_hera_relay.py with AW database configured | 1 passed |
| tests/reliability/test_tasks.py and tests/integration/test_workshop_delegation.py, selected by `-k 'unknown or cross or revoke or lost or repeated'` | 7 passed, 29 deselected |

Reviewed by the HERA transport implementation agent, 2026-11-13. Review disposition: candidate; retain the listed deployment scope and distinguish this Linux review from the full acceptance gates. No approval for merge or performance claims beyond recorded comparisons.
