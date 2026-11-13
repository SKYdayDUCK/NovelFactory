# Durable admission ablation

Alembic revision `0027_hera_capacity` retains the scanned admission policy by default. Empty tables are added at upgrade, and the new task trigger starts disabled. To switch an isolated database, call `SELECT nf_hera_set_capacity_mode(true)` in a short READ COMMITTED administrative transaction. Commit before admitting work. The switch takes the same exclusive maintenance gate as backups, a SHARE ROW EXCLUSIVE lock on tasks and a policy row lock, installs the task trigger, and rebuilds four exact scopes from authoritative live tasks in that transaction. The rebuild is O(live backlog), stops writers during cutover, and can be retried after rollback. Use a planned cutover for larger production histories. Role-resource remapping rebuilds only when the mapping changes with counters active. Old admission receipts retain their resource class. Without a receipt, a historical task follows the current role map.

The product transaction updates total, work, principal and resource usage. A failed scoped increment aborts the task, command receipt and Outbox event together. The total row orders concurrent writers across processes and remains a contention point. Terminal transitions release units, including direct SQL status updates. Failed transactions leave no durable reservation. Drained per-principal, work and resource rows are pruned. All authoritative claim, acceptance, dependency and Provider operations remain on their existing paths. `SELECT nf_hera_set_capacity_mode(false)` removes counter maintenance, clears the derived counts and restores the scan. This is independent of whether the existing admission policy is enabled. Historical tasks and receipts survive both toggles. Downgrade to 0026 requires application rollback to its corresponding SQL version.

## Committed creations

Two fresh PG17.11 databases per depth, 2 works, 8 grants, 4 threads, pool 4, one monitor connection, independent PostgreSQL process. The app and database were pinned to CPU 5. An arm created 40 (first trial) or 48 (final trial) real kernel tasks, then cancelled the probe tasks to restore the starting depth. The historical backlog used deliberately seeded task rows and admission receipts, with comparable scope cardinalities in both arms. Probe creates and cancellations used real kernel authorization, inputs, fairness, receipts and Outbox. These are component experiments. Request generation and the kernel shared a process. The machine was active, and cold OS page-cache resets were not attempted. The first arm in each database measured cold connection setup. Every depth used scan/exact/exact/scan twice.

| Source and starting depth | Scan creates/s | Exact creates/s | Scan/exact P95 ms | Scan/exact P99 ms |
|---|---:|---:|---:|---:|
| Initial, 0 | 30.2 | 28.4 | 223 / 269 | 299 / 805 |
| Initial, 5,000 | 15.5 | 23.6 | 828 / 355 | 1,607 / 1,299 |
| With scoped row reclamation, 0 | 28.7 | 30.7 | 271 / 229 | 426 / 303 |
| With scoped row reclamation, 5,000 | 27.2 | 38.0 | 272 / 195 | 384 / 301 |

All 1,408 timed creates committed with correct READY receipts across the two trials. Every round restored the initial backlog and verified total usage in exact mode. The deep-backlog advantage was 52% and 40% across trials, with P95 and P99 decreasing in both. The zero-backlog tail changed direction between trials. Keep scan as the default. Operators can trial exact accounting for deep queues with their own resource mix. These measurements do not quantify a full novel-production lifecycle or establish the 30% application-wide throughput target.

Final source `7c5db29513bb99dadb40417f925a5419f240dfb0`. In the 5,000-deep scan arm, a create spent mean 66.22 ms on the serialized policy UPDATE round trip, 15.64 ms on the occupancy aggregate, 3.81 ms on task INSERT, 2.21/1.40 ms on idempotency read/write and 1.80 ms on Outbox INSERT. Exact accounting used a 2.06-ms shared configuration lookup. The task INSERT with guarded quota updates took 30.38 ms, the idempotency read/write took 2.99/2.75 ms, and Outbox INSERT took 3.55 ms. SQL round trips include client scheduling, execution and waits. The 20-ms observer saw 502/205 transaction-ID wait samples and 13/0 tuple wait samples on scan/exact. Samples cannot be converted directly to per-query lock duration. PG user-function timing also registered 5,038 ms across 340 outer quota-trigger calls, including nested work and waits. Function snapshots include cleanup and asynchronously published statistics, so the number of flushed calls differs from the number of timed creates.

[Initial raw SQL, transaction and lock records (60c8ea4)](raw/capacity/initial.json.gz) · [Final raw records](raw/capacity/final.json.gz) · [Driver](../../../benchmarks/hera_capacity.py) · [Summary extraction](../../../benchmarks/hera_capacity_summary.py)

## Fixed stripes

The independent four-client PostgreSQL mechanism test assigned four quota scopes and 128 total units to one or eight physical buckets. Uniform load submitted 160 attempts and used all 128 units for either topology, in two repeats. With all attempts targeting a single bucket, eight stripes accepted only 16 in each repeat while one stripe accepted 128. No run exceeded capacity. Reject this fixed partition for product use because it can strand 112 of 128 units. Transferable or renewable bucket reservations need additional coordination and recovery evidence. This probe changes no product routing.

[Stripe mechanism source](../../../benchmarks/hera_capacity_stripes.py) · [Per-attempt raw rows and final authoritative counts](raw/capacity/stripes.json.gz)

## Correctness and recovery

15 real PostgreSQL tests passed, including two database pools racing starts, canonical idempotency, fail-before-commit backend termination, terminal release, role-based historical backfill and remapping, disabling, retry, revocation and persistent deadlines. The `0026 → 0027 → 0026 → 0027` migration round trip completed. A separate pg_dump/pg_restore of an enabled exact-counter database compared all scoped rows byte-for-byte between the two databases. The restored trigger was active and releasing all four restored READY tasks reduced total use from 4 to 0.

[JUnit](raw/capacity/regression.xml) · [Restore transcript](raw/capacity/restore.log)

To reproduce on disposable PG17 with the repository environment and `NF_TEST_DATABASE_URL`, set `shared_preload_libraries=pg_stat_statements`, `pg_stat_statements.track=all`, `track_functions=pl` and `track_io_timing=on`. Pin the PG process and harness consistently.

```sh
python -m benchmarks.hera_capacity --count 48 --output capacity.json.gz
python -m benchmarks.hera_capacity_summary capacity.json.gz
python -m benchmarks.hera_capacity_stripes --output stripes.json.gz
python -m pytest -q tests/integration/test_hera_durable_admission.py
```

## Matched eight-depth sweep and borrowing probe

At each depth four clients made 24 authorized kernel creates per arm, in scan/exact/exact/scan order twice on PG 17.11. Each create was cancelled and the database count returned to the starting depth. Both the driver and PG used CPU 4, with four product connections and one observation connection. These are medians of the four correct-create rates at each depth. All 1,536 creates and all cleanup checks passed. The initial backlog consists of seeded task rows and receipts, and the measured operations use the real kernel. The source product for this run is identical to the incoming version for these operations.

| Starting depth | Scan creates/s | Exact creates/s |
|---:|---:|---:|
| 0 | 64.56 | 68.31 |
| 50 | 65.90 | 68.69 |
| 200 | 59.72 | 59.82 |
| 500 | 54.26 | 58.26 |
| 1,000 | 59.45 | 68.49 |
| 2,000 | 53.02 | 69.23 |
| 5,000 | 44.03 | 63.91 |
| 7,500 | 21.95 | 53.87 |

Reproduce with `python -m benchmarks.hera_capacity --depth 0 50 200 500 1000 2000 5000 7500 --count 24 --output sweep.json.gz`. The raw JSON was losslessly recompressed with XZ. It includes individual operations, SQL intervals, wait observations, WAL and the per-arm totals. [Complete sweep](raw/final-round/capacity-sweep.json.xz)

A separate four-process quota experiment allowed a request to spill into another physical bucket independently for each of four scopes. A fallback waits for eligible locked rows before rejecting the request. The transaction records the assigned bucket for each scope so a different process can release it after the client exits. Under fully skewed preferences, plain eight-bucket partitions consumed 16/128 units in each arm. Both spill arms consumed 128/128. The 128 admissions required 512 scope allocations, which reconciled and returned to zero. Under skew, the spill rates were 141.59 and 257.06 admissions/s. The single-bucket controls were 243.79 and 351.71/s. The workload measures physical quota acquisition and release on PostgreSQL, with no transport, authorization or business execution. Spilling fixed the stranded-capacity problem here but adds allocation cost and has no product-level throughput qualification. Production keeps its original scan and optional exact-counter modes. Run `python -m benchmarks.hera_capacity_spill --output spill.json.gz`. [Twelve unfiltered runs](raw/final-round/quota-spill.json.gz)
