# Recovery and evidence

The existing NF outbox, durable tasks, call ledger and workflow wait/inbox guard are authoritative. Rolled-back speculative work is never signaled as accepted. Redis can lose a hint or an entire stream; the PostgreSQL source and inbox remain available to replay. In Workshop an explicitly subscribed completion releases a PENDING manager request when the installation-scoped delegation is SETTLED, then the same branch's project resume API validates current grants, generation and the exact originating receipt. This includes CANCELLED with terminal accounting, allowing a manager to inspect the final outcome. An ineligible or expired unfinished dependency receives WAITING_OPERATOR instead.

| Failure path | Execution result |
|---|---|
| Cold and reused SDK sessions lose the reply after NF created a delegation | 2 independent databases, actual SDK stdio peers. The original task and hold are found after reconnect. Redis is rebuilt from both outboxes. An authorized manager's completion subscription becomes eligible once after a terminal cancellation. Wrong branch and receipt both fail. The manager then recovers once through the exact NF binding; the next worker is IDLE. |
| Worker/pool died or database connection reopened, provider outcome UNKNOWN | Native supervisor, lease, routing and workflow tests cover these boundaries. The 2,000-workflow fixture retains every one of 20 injected UNKNOWN sends without another provider call and recovers all expected dependencies. |
| Grouped Workshop publisher lost connection after sending a 32-hint chunk, before updating PostgreSQL | Restart publishes all 40 sources. Redis holds 72 hints; exactly 40 authorized SQL inbox receipts, zero Redis pending. A subsequent full SQL replay adds zero new receipts. |
| Full ingress operation slot, then two transports retry the same installation command | Live REST returns 429, Retry-After 1. Live remote tool returns BACKPRESSURE with retry_same_operation=true and retry_after_ms=1000. No delegation receipt exists after the rejected pair. The replay yields one canonical task and one provider effect. |
| All local model workers busy during a queue deadline | The optional supervisor control path retires the overdue second job while the first remains RUNNING with a single provider send. No extra model is dispatched. |

Workshop delivery timings on e62db03/65e5afe use PostgreSQL 17.11, Redis 8.0.2, Python 3.12.14, one publisher and one consumer, fresh authorization/store per arm, 1-hour message TTL and a 32-entry maximum immediate batch. Values are single-invocation publication plus validated SQL receipt times, in ms, in A/B/B/A order.

| Offered messages | Legacy A1 | Grouped B1 | Grouped B2 | Legacy A2 |
|---:|---:|---:|---:|---:|
| 1 | 57.13 | 23.47 | 29.00 | 32.66 |
| 32 | 637.24 | 89.28 | 63.16 | 569.58 |
| 128 | 2297.83 | 351.85 | 394.95 | 2332.67 |

All 644 receipts match their real source event IDs and Redis has zero pending after each run. For 32 queued messages, paired elapsed-time ratios are 7.14× and 9.02×, and for 128 messages 6.53× and 5.91×. Two observations per arm cannot establish deployment tail percentiles. Grouping remains optional. Replay `python benchmarks/hera_workshop_relay.py --output out.jsonl` using a disposable Workshop PostgreSQL database in `AW_DATABASE_URL` and Redis in `AW_REDIS_TEST_URL`. For the dual-service tests configure `AW_DUAL_TEST_ADMIN_URL`, `AW_NF_SOURCE`, and `AW_DATABASE_URL` as in the Workshop CI runner. [Raw relay per-event comparison](raw/workshop_relay.jsonl) · [Workshop integration results](raw/workshop-wake.xml) · [Resource isolation](RESOURCE_ISOLATION.md) · [Preregistration](RESOURCE_EXPERIMENT_PLAN.md)

The final combined Linux supervisor/derived-worker regression ran 37 checks successfully. One check requires native Windows resource counters and was skipped on Linux. It covers both standalone and scoped compute dispatch, including holding one class lease in one pathway while the other returns idle. [Raw pytest XML](raw/supervisor-regression.xml)

The branch progress checker compares live task counts and the aggregate in one database snapshot, detects both an invalid representation and a plausible positive count changed out of band, and supports an explicit rebuild. The reader falls back on malformed data. A trigger-local data error while applying a damaged delta evicts the affected derived row and the authoritative task cancellation commits. Repeated live authorization still denies a revoked reader. The real PG test covers backfill, rolled-back update, corruption, rebuild, disabled state and grant expiry/revocation boundaries applicable to this reader (explicit revocation exercised). [Projection integrity check](raw/projection-integrity.xml)

## Cross-transport Workshop completion and physical Redis loss

Each ingress direction ran as a separate operation. Workshop's `aw_delegate` issued the operation and budget reservation for a chief editor's assignment to an evidence maintainer. The manager subscribed over a separate stdio session. The reconciler submitted the exact Workshop payload with the installation credential over loopback TCP. The test discarded the successful submit reply before the Workshop transaction could record it. The opposite transport read the committed receipt, rebound the operation and replayed the same submit. NF kept one receipt and one task.

| Test arm | Direct observations |
|---|---|
| Remote MCP submit → REST query and reconciliation | One simulated provider call, 18 tokens, one accepted summary. Workshop settled the original hold. The enrolled manager woke the exact blocked successor to READY. |
| REST submit → remote MCP query and reconciliation | The same assertions passed with a distinct installation, operation, task, receipt, manager request and successor. |
| Cold and reused stdio | Both existing lost-reply, handoff, cancellation and zero-charge settlement cases passed with a real Redis rebuild from both outboxes. |
| Physical Redis process loss | A pre-crash published hint vanished. PostgreSQL accepted new mail and its acknowledgement while redis-server was dead. A replacement process started with zero keys. Fresh publisher and consumer processes rebuilt both notifications from PostgreSQL. Both event IDs had exactly one SQL inbox receipt after a deliberate duplicate, one mail acknowledgement persisted, and Redis pending was zero. |

The mixed arms used a deterministic provider with a zero-dollar receipt, actual SQL task and accounting transitions, and real MCP SDK sessions. NF rejected a mismatched receipt before the bound project grant released the waiting successor. The next manager poll returned IDLE. The test invokes Supervisor's candidate-acceptance stage separately so the manager owns the next recovery pass. The physical outage used the native Redis 8.0.2 binary with persistence disabled. The Docker fixture path was not exercised locally.

The run used two separate PostgreSQL 17.11 clusters. The integration fixture checked distinct system identifiers and rejection of both cross-service SQL logins. For reproduction, set `AW_DUAL_TEST_ADMIN_URL` and `AW_DUAL_TEST_NF_ADMIN_URL` to administrator URLs on distinct disposable clusters, `AW_NF_SOURCE` to the checkout, `AW_DATABASE_URL` to a disposable Workshop test database, and `AW_REDIS_TEST_URL` to a disposable Streams endpoint. With `redis-server` on PATH and `AW_REDIS_TEST_CONTAINER` unset, run from the repository root.

```sh
python -m agent_workshop.migrate upgrade
python -m pytest -q -o junit_family=xunit1 --tb=short \
  workshop/integration/test_dual_stdio.py \
  workshop/integration/test_hera_delegation_wake.py \
  workshop/tests/test_notifications.py \
  --junitxml=reports/performance/hera/raw/workshop-full-recovery.xml
```

The command returned `6 passed in 101.37s (0:01:41)`, with zero failures, errors or skips. The native Redis child changed from PID 577 to PID 591. The JUnit properties contain both Redis run IDs and event IDs, and each delegation's installation, operation, task, result, receipt, budget reservation, manager request and awakened successor. [Raw pytest results and per-operation identifiers](raw/workshop-full-recovery.xml)
