# HERA optimization decisions and combination retest

All performance numbers below count correct responses or committed operations. Each row identifies its denominator. The original reports and failed candidates remain in the branch history and raw directories.

| Mechanism | Replicated evidence | Decision |
|---|---|---|
| Exact admission counters | At depth 5,000, final scan/exact throughput 27.15 / 38.00 authorized creates/s. P95 272 / 195 ms and P99 384 / 301 ms. Initial +52.3% and final +40.0% under deep backlog; shallow results differed. | Opt-in for deep queues. Keep scan default. [Counter and recovery evidence](CAPACITY_COUNTERS.md) |
| Eight fixed quota stripes | Balanced 128/128; skewed only 16/128 despite unused global headroom. | Reject fixed stripes without borrowing. [Raw stripes](raw/capacity/stripes.json.gz) |
| Single-task MCP status | 16→11 app SQL and 5→4 transactions per real SDK call. Both warm comparisons increased throughput and reduced P95. The discovery transaction is avoidable only for an installed ordinary binding and compatible method. | Opt-in. Keep audited start/end, current READ and owner/binding checks. [Stage evidence](MCP_STATUS_AUTH.md) |
| Compact create (K) | Copy/validate TaskSpec once in the Kernel path, reuse the locked policy and skip a known empty artifact query. Per 64 warm lifecycles: 4,160→3,904 SQL. K measured 10.20 / 11.35 cycles/s versus 9.59 / 10.03 control arms. K P99 401 / 393 ms versus 613 / 615. | Opt-in. These arms have no model calls and 16 lanes. The combination test below determines the release gate. |
| Reuse compiled scalar lookup shapes (L) | Unchanged live queries and locks. Warm throughput 6.96 / 12.51 cycles/s, P99 1,252 / 474 ms. K+L 9.94 / 9.30 cycles/s, P99 761 / 691 ms. | Reverted. Both source and reversion are retained. [Eight arms, order Z/L/K/D/D/K/L/Z](raw/causal-20261113/app/) |
| Empty progress maintenance | Skips actual trigger execution only after excluding all installed counters. Synchronous same-branch writes contend on one row. Sixteen shards and an Outbox-based, recoverable projection were prototyped on measured transitions. | Keep existing synchronous default; offer fenced suspension only when the projection table is empty. No stale read in claiming/acceptance/recovery. [Trigger timings and WAL](PROJECTION_CONTENTION.md) |
| Fixed-cut cache, batch and notification | High-load same-scope batches 140 wire calls per 1,000 rows. 96 fixed-cut reads verified; 32 listener-triggered authorized reads versus 123 polling reads for two matched 16-event arms. | Keep existing explicit batches, optional dedup and existing fixed-cut cache. Bound LISTEN sessions and re-read current state after every wake. [Full low/medium/high results](READS_AND_DELIVERY_RECHECK.md) |
| Index worker topology | Shared API/index comparison lost 38.4% and 47.8% fast-read throughput under backlog at the fixed one-core budget. Physical separation did not establish the ≤20% target. | No new default. Compare both indexing and read goodput under a deployment's own workload. [Pool/process and OS observations](INDEX_TOPOLOGY.md) |
| Scheduling and elasticity | 384/384 actual claims, 192/192 renewals. FIFO starved the second preloaded queue for 48 consecutive claims. On two CPUs the fixed/adaptive/adaptive/fixed runs measured 20.01/13.05/10.65/23.94 jobs/s. | Keep fair scheduling and static worker defaults. [Queue order, delays and elastic observations](SCHEDULER_RECHECK.md) |
| One/two API replicas | In the full repetition 2,048/2,048 correct reads at four total database connections. RR was 55.55 / 62.13 reads/s vs 48.79 / 45.50 on neighboring one-process controls. Least outstanding 53.86 / 39.05 vs 48.31 / 39.68. P2C 59.57 / 51.25 vs 42.68 / 30.45. | Multiple API processes can improve a saturated mixed-read load. With two replicas least-outstanding and sampling both replicas cannot be ranked as distinct selection rules. One earlier repetition lost a child process; its completed arms and exception were retained. [Complete and interrupted runs](raw/routing-recheck/) |

## Frozen 12-arm combination

Four repetitions each on main (A), compact creation + empty projection suspension + scan (S), and the same with exact quotas (C). All shared the candidate migration schema, no configured progress branches, four application connections, two auth slots, two business slots, 16 separate grants and two works. Migration 0027 installed its exact-count trigger disabled on A and S. Main used its original task implementation without a quota. These are system-level comparisons; the application of durable admission itself adds a real obligation on HERA. PostgreSQL used CPU 5, the API CPU 6 and client CPU 7. Tests elsewhere on the host shared its storage and memory. Each arm included 16 first-use and 64 warm cycles. All 960 cycles, 6,720 responses, 2,880 task-state Outbox effects and 1,920 command receipts reconciled, with no Provider attempts. [Complete traces](raw/causal-20261113/combination/)

| Warm lifecycle/s (chronological within arm) | Median | Warm request P95 range | Warm request P99 range |
|---|---:|---:|---:|
| Main 10.60, 9.13, 7.53, 9.92 | 9.53 | 355–551 ms | 512–622 ms |
| Scan 4.59, 9.71, 11.54, 7.15 | 8.43 | 389–714 ms | 507–6,412 ms |
| Exact 7.61, 8.01, 10.07, 7.35 | 7.81 | 399–705 ms | 525–1,358 ms |

A single scan arm had a 6.4-second P99. It remains in the sample. Neither variant passed the +30% full-task target. The ratios of medians against main were -11.5% and -18.0% for this small-backlog workload. These descriptive comparisons do not establish equivalent regressions in deep admission or model execution. The final code also repairs capacity errors during conflict repair and recovery, and rejects stale administrator snapshots on projection rebuild.

## Final source confirmation after the recovery fixes

The run below used the repaired production `src` tree `c8d890caab742e568a9809f3b8bba4c77e2e2035`, product checkout `e3cd05a`, and the identical 0027 schema for main and HERA. It repeated the CPU 5/6/7 topology with eight lifecycles per warm lane instead of four, giving 128 warm and 16 first-use cycles per arm. Two separate A/B/B/A blocks isolated the two production choices. All 1,152 lifecycles and 8,064 HTTP responses matched the wire and database oracles. [Eight complete arms with SQL, queue, WAL, trigger, resource samples and commits](raw/causal-20261113/repair-confirmation/)

| Order | Warm correct cycles/s | Warm request P95 ms | Warm request P99 ms | Application SQL / 128 cycles |
|---|---:|---:|---:|---:|
| Main before exact | 11.640 | 398.7 | 636.9 | 8,576 |
| Exact count | 9.993 | 378.9 | 533.4 | 7,424 |
| Exact count | 11.855 | 312.9 | 419.2 | 7,424 |
| Main after exact | 10.243 | 397.0 | 534.3 | 8,576 |
| Main before scan | 11.377 | 346.9 | 460.6 | 8,576 |
| Scan | 13.733 | 261.7 | 320.9 | 7,680 |
| Scan | 13.728 | 257.8 | 330.1 | 7,680 |
| Main after scan | 12.818 | 277.1 | 374.2 | 8,576 |

Scan gained **20.7% / 7.1%** over its adjacent main controls, with lower request P95 and P99 in both comparisons. Exact counting changed throughput by **−14.1% / +15.7%** on this shallow workload. Each arm contained all seven HTTP calls per cycle. The quota was active on both HERA variants; both used compact creation and suspended empty progress maintenance. These results supersede any claim about the current source based on the earlier 12-arm series alone. They still do not show the targeted 30% increase or a consistently faster exact-count lifecycle. The earlier weaker measurements remain part of the decision.

## Current iteration

[Audited role delivery](ROLE_DELIVERY.md) reduced SQL and transactions per independent full read. [Static schema traversal](AT30_CONTRACTS.md) improved valid first responses in both 50-agent overload comparisons. [Quota spill](CAPACITY_COUNTERS.md) fixed stranded physical quota on skew but did not justify deployment. [Index materialization and process priority](RESOURCE_ISOLATION.md) retained the full workload and measured background throughput; neither qualified for use. The benchmark listener change in [the calibration](LISTENER_CALIBRATION.md) applies to both products; its transport gains are excluded from product gains.
