# PostgreSQL durable admission

Product commit `fd059ce`, PostgreSQL 17.11, local Linux, SQLAlchemy pools up to 8+4 in the admission race, two independent engines, no paid inference. Migration starts at 0022 and adds a singleton policy, sparse task admission records and a deadline index. It does not backfill task history or rewrite existing rows. Tests ran on empty disposable clusters with current migrations.

```sh
python -m pytest -q tests/integration/test_hera_durable_admission.py tests/reliability/test_tasks.py tests/reliability/test_workflow.py tests/reliability/test_workflow_downgrade.py tests/service/test_hera_mixed_ingress.py tests/integration/test_hera_supervisor.py
```

[Raw JUnit](raw/durable-regression.xml) contains 43 passes and no failures. Eight simultaneous distinct submissions from two independent PostgreSQL engines competed for a global capacity of two: exactly two committed, six returned retryable BACKPRESSURE. Eight concurrent replay requests at capacity returned the same durable receipt, and changing the payload returned an idempotency conflict. After cancelling the first two, a previously rejected task successfully committed. The database held three admissions and three unique initial task-change events. A second scenario checked that pre-policy tasks count toward occupancy, work and principal ceilings operate independently, a caller cannot change its resource class by naming the task kind, and grant revocation blocks a replay. The recovery exercise enforced initial expiry for unenrolled READY and BLOCKED tasks after service restart and policy disable; repeated reconciliation recorded one expiration per task and zero execution attempts. A real PostgreSQL backend was terminated after the new event was inserted but before COMMIT. A new connection observed zero task, admission, command-receipt and outbox rows for the interrupted operation. The same authorized key then committed and replayed exactly once.

Six real database cases reject invalid resource-map values, including nested arrays, nonstrings and unknown classes.

The enabled singleton serializes short admission transactions. The initial implementation counts nonterminal tasks against the configured policy on each new command, adding work proportional to the rows inspected. It does not hold a database connection during model work. This correctness gate does not establish a throughput improvement. See [the design and rollback](../../../docs/design/HERA_DURABLE_ADMISSION.md) and [the mixed-traffic measurements](MIXED_INGRESS.md) for the performance workload and its separate terminal-work criterion.

At 5,000 live tasks the separate [storage and depth experiment](ADMISSION_DEPTH.md) measured per-relation byte growth, backend RSS and additional policy scan cost against a disabled baseline.
