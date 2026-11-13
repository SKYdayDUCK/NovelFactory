# Fair claims, renewal contention and compute scaling

Four claim workers and one renewal worker used five real PostgreSQL connections against a new migrated database in each arm. Two books each had 48 authorized READY tasks with the same kind, all of the first book enqueued before the second. An additional live RUNNING task was renewed 48 times during the drain. The comparison changed only the SQL sort order via a benchmark-only engine listener. The ordinary row locks, eligibility predicates, task generation, fairness ledger, execution, input and Outbox writes all ran unchanged. A trusted internal scheduler was used to see both books; HTTP claims remain grant-bound. The first claim-queue EXPLAIN ANALYZE used restored READY states within a rolled-back savepoint.

| Order | Actual claims/s | Claim P95 / P99 ms | Renewal P95 / P99 ms | First claim position by work A / B | Max absolute difference in number served |
|---|---:|---:|---:|---:|---:|
| Fair | 55.23 | 131.80 / 158.68 | 102.90 / 148.25 | 2 / 1 | 1 |
| FIFO | 47.30 | 136.96 / 206.97 | 105.13 / 168.21 | 1 / 49 | 48 |
| FIFO | 45.53 | 196.56 / 263.72 | 122.40 / 269.90 | 1 / 49 | 48 |
| Fair | 51.98 | 148.91 / 200.20 | 116.44 / 170.72 | 2 / 1 | 1 |

All 384 queue tasks reached generation 1 RUNNING exactly once and 192 renewals succeeded. Existing fair scheduling alternated books and outperformed FIFO in this workload. Fair full-backlog select plans reported 1.10 and 1.99 ms with 1,005 shared buffer hits each; FIFO reported 3.09 and 1.46 ms with 999/997 shared hits. With 96 eligible tasks the planner scanned the READY set and sorted eligible candidates before locking one. End-to-end claim latencies include shared/exclusive advisory-lock competition, remaining reads/writes and commit. FIFO neither justified removing fairness nor provided a priority guarantee for renewals. The observed renewal tails cover 300-second leases and cannot justify shorter leases or arbitrary renewal priority. [Driver](../../../benchmarks/hera_claim_scheduler.py) · [Per-claim, per-renewal and EXPLAIN records](raw/index-topology/claim.json.gz)

## Adaptive index execution

The one-CPU replay processed 24 immutable world-index jobs per arm with two warmed worker processes, three parent pool connections, one connection per child and two global slots. All 96 index receipts and branch watermarks matched. Fixed, adaptive, adaptive, fixed rates were 14.01, 15.63, 11.96 and 7.46 jobs/s. The controller correctly held the admission target at one, consistent with its effective CPU cap; no resize occurred in either adaptive arm. Variance between fixed arms precludes a general throughput decision from this block. [Single-CPU measurements](raw/index-topology/scaling.json)

To replay use `python -m benchmarks.hera_claim_scheduler --per-work 48 --output claim.json.gz`. On a separate freshly migrated database run `python -m benchmarks.p55_capacity --jobs 24 --workers 2 --measure-resize --output scaling.json`. Both tools use NF_TEST_DATABASE_URL.
