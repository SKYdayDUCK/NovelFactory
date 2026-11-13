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

A second run pinned every process to CPUs 0 and 8. Fixed, adaptive, adaptive, fixed rates were 20.01, 13.05, 10.65 and 23.94 jobs/s, with P99 completion times of 1.20, 1.84, 2.25 and 1.00 s. All 96 index jobs verified; neither adaptive arm grew above one and no scale decision was persisted. Physical child processes were warmed ahead of timing in every arm. Consequently these results provide throughput and effective admission target samples, with neither an actual scale-out latency nor an observed two-to-one transition. Initial two-CPU testing uncovered a race in the original warm-up driver; one already-started process could take every short warm-up job before its sibling was ready. A process-initializer barrier now proves each child has opened its PG connection before timing. [Two-CPU run](raw/index-topology/scaling-2cpu.json) · [Preserved preflight failure](raw/index-topology/warmup-failure.txt)

## Live downscale and reacquisition

Three fresh PG17 fixtures on CPUs 0 and 8 each ran eight genuine index jobs. Two independent index workers owned valid leases before the database's CPU_COMPUTE limit changed from two to one. Barrier controls kept the existing leases alive until their admission checks were observed, then released each worker to materialize its actual immutable index receipt. A third claim was rejected at depth two and again at depth one. After depth reached zero the authoritative query granted exactly one new permit. Raising the cap back to two immediately admitted an additional real job. These times include the diagnostic queries and controlled ordering; they measure the live quota's nonpreemptive behavior rather than OS process creation or destruction.

| Repetition | Lower-limit commit ms | Request → active depth 1 ms | Request → old leases drained ms | Request → next valid permit ms | Raise request → second valid permit ms | Task renewals / slowest ms |
|---|---:|---:|---:|---:|---:|---:|
| 1 | 16.36 | 112.66 | 195.25 | 212.97 | 19.69 | 16 / 32.21 |
| 2 | 14.81 | 101.23 | 176.32 | 196.70 | 19.13 | 17 / 17.51 |
| 3 | 24.28 | 139.50 | 236.49 | 248.49 | 13.99 | 19 / 32.68 |

All 52 concurrent 10-second task-lease renewals succeeded, all 24 index events were consumed once, the final watermarks matched, and zero active index leases remained. This finite test establishes observed progress and enforcement at each capacity boundary; it provides no unbounded starvation guarantee. A preflight assertion used the outer dispatcher result shape for a direct index receipt and was corrected to verify its actual persisted consumption flag and event ID. [Per-lease, permit, watermark and renewal timings](raw/index-topology/resize.json.gz) · [Driver](../../../benchmarks/hera_index_resize.py) · [Preflight diagnostic](raw/index-topology/resize-fixture-failure.txt)

Replay with `python -m benchmarks.hera_index_resize --output resize.json.gz` in a disposable PG17 database.

## Scheduler wake traffic from an unrelated work

A separate real compute supervisor with two worker slots listened for PostgreSQL commit notifications while another work created 64 authorized tasks. Before measurement, one transfer started the worker pool. After the unrelated traffic, four accepted transfers produced their own world events and durable compute hints. Each arm verified every task, all five distinct index receipts, and the graph/timeline watermarks at the correct final branch revision. The existing fixed scheduler and its one-second reconciliation interval stayed active. The candidate only coalesced hints during idle waits, up to the smaller of 25 ms and time remaining until the next reconciliation. The maximum in-flight worker count stayed fixed.

| Window in mirrored order | Unrelated task creates/s | Durable compute hint reads | Total supervisor SQL during unrelated commits | Four new index receipts after hint commit, ms |
|---|---:|---:|---:|---|
| 0 ms | 21.41 | 63 | 271 | 93, 79, 81, 98 |
| 25 ms | 48.75 | 31 | 130 | 166, 111, 107, 153 |
| 25 ms | 35.57 | 33 | 144 | 140, 249, 128, 114 |
| 0 ms | 39.15 | 63 | 265 | 257, 80, 87, 73 |

The bounded waits reduced redundant probes, but median latency to an index receipt rose in both pairs. Unrelated creation throughput improved in only one pair. This experiment retains the original immediate wake behavior. All 13 focused PostgreSQL scheduler and supervisor regression tests passed. Apply the archived patch to `eb1a75e`, set `NF_TEST_DATABASE_URL` to PostgreSQL 17, pin PostgreSQL and the driver to the same CPU, and run `python -m benchmarks.hera_idle_wakes --output wakes.json.gz`. The harness dispatches committed source envelopes through the real compute subscriber in-process and verifies the separate supervisor's PostgreSQL effects. It does not time Redis relay delivery. [Full SQL records, delays, rejected patch and tests](raw/index-worker-qualification/wake-coalescing/)

### Preserve immediate wake and join the two scheduler checks

After locking and authorizing the live administrator and work, a single SQL query reads the enrollment and the current processor hint. An absent enrollment returns idle even for reconciliation. A configured branch with a null hint performs the same periodic scan as before. The durable hint occurrence and compare-before-clear, authoritative reservation, lease expiry and scope checks stay in place. This has no new schema or required configuration. Reverting the query restores the two-read version without moving data.

| Arm | Other-work creates/s | Actual compute hint probes | Supervisor SQL in the measured traffic window | Median commit-to-index receipt ms (four post-window transfers) |
|---|---:|---:|---:|---:|
| Original | 42.14 | 63 | 264 | 80.4 |
| Joined | 42.31 | 63 | 202 | 81.7 |
| Joined | 42.06 | 63 | 202 | 76.3 |
| Original | 40.92 | 63 | 264 | 87.5 |

The adjacent controls each needed 264 SQL statements and both treatments needed 202 (−23.5%). All 256 unrelated tasks and 20 separate branch index receipts finished the fixture with the correct watermarks. The small changes in external task throughput are insufficient to establish a workload-wide acceleration. Eight PostgreSQL scheduler and processor tests passed, including denial after administrator revocation even when the transport is disabled. The query avoids an extra database round trip during immediate wake and introduces no intentional waiting. Execute the same `benchmarks/hera_idle_wakes.py` driver by absolute path with each measured checkout as the working directory and `PYTHONPATH` set to that checkout, using `--output OUT.json.gz`; pin all processes including PostgreSQL to one CPU. [Four original traces and focused regressions](raw/index-worker-qualification/scheduler-hints/)
