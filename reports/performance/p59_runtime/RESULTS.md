# P5.9 runtime extension measurements

## Durable workflow qualification

Each participant had a distinct persisted grant, principal and task. Start-of-drain populations are READY / WAITING / already CANCELLED. Every confirmed result belonged to the current generation and context. The two prerequisite summaries per run reached ACCEPTED. All other successful results were stored for subsequent use. The two books' world heads stayed unchanged. The 1% of participants cancelled before execution and the 1% with an intentionally lost provider reply were excluded from successful throughput. Every injected outcome matched the ledger and adapter. There were zero unexpected outcomes and zero dispatch slots left at the end of all three runs.

| Participating agents | Initial ready / waiting / cancelled | Successful results | UNKNOWN / cancelled | Confirmed wakes | Simulated sends | Drain / total s | Successful results/s, drain / total |
| ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| 500 | 396 / 99 / 5 | 490 | 5 / 5 | 99 | 495 | 168.17 / 200.56 | 2.91 / 2.44 |
| 1,000 | 791 / 199 / 10 | 980 | 10 / 10 | 199 | 990 | 314.07 / 376.54 | 3.12 / 2.60 |
| 2,000 | 1,581 / 399 / 20 | 1,960 | 20 / 20 | 399 | 1,980 | 500.64 / 624.63 | 3.92 / 3.14 |

| Agents | Creation to first claim, P50 / P95 / P99 s | Admission call to observed completion, P50 / P95 / P99 ms | Checked-out PG connections / configured max | Peak admitted futures / simultaneous model calls | Peak process RSS MiB |
| ---: | ---: | ---: | ---: | ---: | ---: |
| 500 | 97.89 / 158.36 / 166.39 | 645 / 972 / 1,144 | 4 / 8 | 2 / 2 | 82.1 |
| 1,000 | 230.83 / 306.51 / 312.50 | 569 / 1,133 / 1,387 | 4 / 8 | 2 / 2 | 88.9 |
| 2,000 | 384.79 / 490.93 / 499.95 | 369 / 1,561 / 2,165 | 4 / 8 | 2 / 2 | 87.3 |

These shared-host runs used Linux, CPython 3.12.14, PostgreSQL 17.11 and an in-process fixture adapter. The three sizes ran concurrently against separate disposable clusters alongside other qualification jobs. A batch waits for provisioning before drain. The increasing queue time is visible even though the execution-slot and database peaks remain bounded. No external model or HTTP/MCP request was counted in the workflow rates. RSS includes the driver and the application, excluding separate database processes. In the 2,000-agent run, the two books accounted for 248/247, 247/248, 248/247 and 257/238 observed completions in successive quarters of the drain. The second book contained all 20 pre-dispatch cancellations. Weighted arbitration across four books in two organizations is covered separately in the [recovery matrix](../../code_review/p59_runtime/RECOVERY_MATRIX.md).

The verifier and product were pinned to local `e23b8e0`, tree-identical to [GitHub ce74f265](https://github.com/SKYdayDUCK/NovelFactory/tree/ce74f265b9072df52e9a485c6dfd48d5612d1907). Each JSON contains the benchmark source hash, per-task generation, execution/Attempt count, result witness, wake receipt, timing and final state. [500 agents](raw/final-500.json.gz) · [1,000 agents](raw/final-1000.json.gz) · [2,000 agents](raw/final-2000.json.gz) · [Methods and commands](METHOD.md)

## Before and after the first-claim failure

The first 500- and 1,000-participant runs completed with 490 and 980 successful results. The first 2,000-participant run on the same selector failed on its first real claim. In an A/B on one 2,000-task populated queue, the original selector exceeded the 15-second statement timeout in both trials. So did a scope-guard-only alternative. Keeping the job lookup bound to its indexed task completed in 296.994 and 391.768 ms. On 100 tasks the four selectors chose exactly the same task, with overlapping runtimes. No numeric speedup is assigned to the censored old 2,000-task results. [Pre-fix 500](raw/workflow-500.json.gz) · [Pre-fix 1,000](raw/workflow-1000.json.gz) · [Original timeout](raw/workflow-2000.log.gz) · [Plans and timings](raw/dispatch/)

The first complete post-fix 2,000 run on [GitHub 01fff659](https://github.com/SKYdayDUCK/NovelFactory/tree/01fff6596456736733b7208fa3675463f887bb07) took 517.17 s including setup, reached 1,960 successful results and reached 4.64 successful results/s while draining. The later matrix above uses stricter per-task postconditions and a different shared-host load. The throughput difference across these runs is not a causal measure of a product regression or improvement. Reservation SQL fell from 15 statements to 12. Static bound-tool discovery fell from 19 to 9. The HTTP batch change was rejected after its end-to-end comparison. [First completed 2,000 run](raw/workflow-2000-fixed.json.gz) · [Reservation comparisons](RESERVATIONS.md) · [Discovery comparisons](role-discovery/) · [Rejected HTTP experiment](http-batch/README.md)

## Original HTTP capacity gates

The frozen 200-agent overload still requires 950 first call starts per second. It offers 1,000 logical open-loop calls per second and also validates mixed writes, conflicts, recovery and the correct world effects. The independent 1,000-agent open-arrival burst offers 5,000 reads over five seconds, measures actual HTTP header starts, and validates each reply. The target, payloads, credentials, correctness rules and retry budgets were retained.

| Original probe | Frozen main 4348405 | Candidate ce74f265 under concurrent qualification |
| --- | --- | --- |
| 200 agents, first call starts/s / header starts/s | 47.20 / 31.34 | 21.78 / 19.02 |
| 200 agents, validated business calls/s excluding duplicate receipts | 11.47 | 8.39 |
| 200 agents, all attempts including retries | 2,071 HTTP 200, 1,144 HTTP 429, 19 HTTP 409 | 1,885 HTTP 200, 1,940 HTTP 429, 19 HTTP 409 |
| 200 agents, authentication rejected / authentication timeouts / business timeouts | 1,106 / 25 / 13 | 1,393 / 288 / 259 |
| 200 agents, DB peak / pool limit | 8 / 8 | 8 / 8 |
| 1,000 agents, header starts/s / validated reads/s | 48.09 / 17.53 | 37.32 / 8.47 |
| 1,000 agents, validated 200 / HTTP 429 / transport errors | 1,870 / 3,090 / 40 | 1,163 / 3,776 / 61 |

Both variants were `NOT_MET` on first-attempt capacity. Both 200-agent business/recovery gates passed with 21 expected new revisions and no unresolved operational errors. All 1,000-agent HTTP 200 responses in this table passed validation, with no change in world head. At 200 agents, CPU load averages and competition were higher for the candidate. Lower rates in these observations are retained. These two nonisolated snapshots do not quantify the contribution of any individual refactor. Separately, the same client, fixed 2,104-byte response and 5,000 scheduled requests achieved 199.57 and 194.29 header starts/s in two transport-only calibration runs. That path has no NovelFactory business work. [Baseline 200](raw/baseline-at30-200.json.gz) · [Candidate 200](raw/final-at30-200.json.gz) · [Baseline 1,000](raw/baseline-at30-open.json.gz) · [Candidate 1,000](raw/final-at30-open.json.gz) · [Transport calibration](raw/calibration.json.gz) · [Open capacity issue](https://github.com/SKYdayDUCK/NovelFactory/issues/13)

### Serial repeat on the same host

Each version used a separate locked checkout and a new PG17 cluster. The 200-agent pair ran baseline then candidate. The 1,000-agent pair reversed that order. The four HTTP probes ran sequentially. The ongoing qualification loads on the shared machine could still vary. Both versions again failed the original capacity gates. Both 200-agent runs passed business/recovery with zero correctness failures.

| Measurement | Frozen main | Candidate |
| --- | ---: | ---: |
| 200 agents, first call starts/s (minimum 950) | 44.95 | 45.20 |
| 200 agents, header starts/s | 34.28 | 35.79 |
| 200 agents, effective calls/s excluding duplicate receipts | 11.80 | 11.62 |
| 1,000 agents, header starts/s | 47.50 | 48.12 |
| 1,000 agents, validated reads/s | 16.99 | 18.24 |
| 1,000 agents, validated reads / HTTP 429 / transport errors, out of 5,000 | 1,835 / 3,132 / 33 | 1,941 / 3,036 / 23 |

In the serial repeat, the 1,000-agent candidate delivered more validated reads than the baseline. In the concurrently loaded run above and the hosted runs in the independent audit, it delivered fewer. This evidence does not establish a general HTTP performance gain. [Serial 200 baseline](raw/controlled-base-200.json.gz) · [Serial 200 candidate](raw/controlled-candidate-200.json.gz) · [Serial 1,000 baseline](raw/controlled-base-open.json.gz) · [Serial 1,000 candidate](raw/controlled-candidate-open.json.gz) · [Hosted comparison](../../code_review/p59_runtime/INTEGRITY_CI.md)

## MCP protocol throughput

A separate ABBA run used two independent native stdio processes, two differently authorized roles, and the real MCP SDK. Each closed-loop phase checked 240 full task/context results against the corresponding role. Frozen main delivered 56.32 and 56.57 validated reads/s. The candidate delivered 82.98 and 71.53 validated reads/s. In the independent 12 requests/s open-arrival phases, frozen main validated 240/240 twice. The candidate validated 240/240 and 236/240, with four explicit `BACKPRESSURE` responses. All returned business payloads matched their own bindings. These short protocol runs contain no model sends. [Complete samples, P50/P95/P99 and reproduction](STDIO.md)

## Fixed reads for one hour

On frozen main all 500 identities participated in 72,000 scheduled requests. The original fixture ran for 3,600.02 s at 20.000 call starts/s, validated 70,978 reads (19.716/s), and returned 736 `429 BACKPRESSURE` and 286 `409 LEASE_EXPIRED`. The original one-hour gate is `NOT_MET`. All backpressure responses occurred between 1,521.85 and 1,782.15 s, during concurrent qualification work. Lease-expired responses occurred in the final 14.35 s, after the original maximum 3,600-second leases began expiring. The 50th, 95th and 99th percentiles across attempts were 114.91, 1,943.26 and 2,280.53 ms. The pool reached 8/8 and released all connections at the end. There were zero world changes and zero external calls. [Original read-hour manifest](raw/stability/soak-baseline-manifest.json) · [Full payload reconstruction](raw/stability/README.md)

The separate active-owner variant on [GitHub c4ae391e](https://github.com/SKYdayDUCK/NovelFactory/tree/c4ae391ee938ea756062d02528e278e03cf70f60) ran for 3,600.04 s at 20.000 call starts/s. Its two renewal rounds each renewed all 500 leases. It validated 71,133/72,000 requests (19.759/s) with zero lease errors, 866 `429 BACKPRESSURE` responses and one transport read error. The unsuccessful requests began between 1,012.11 and 1,276.50 s, covering the same wall-clock load burst as the original fixture. No incorrectly scoped or malformed business result was observed. P50/P95/P99 were 117.26/1,950.08/2,292.76 ms across all attempts, the pool reached 8/8 and ended with zero checkouts, and both the world-commit and model-call counts were zero. The two API processes' summed peak RSS was 232,768 KiB (237,684 KiB in the original). This read-hour is also `NOT_MET`. Both full payloads include all 72,000 request records and the PostgreSQL samples. [Active-owner read-hour manifest](raw/stability/soak-renew-manifest.json)

A further local attempt pinned the active-owner run to five eligible Linux CPUs. Its execution session disappeared before the requested duration. It supplied no completed request trace or exit receipt and is recorded as `INTERRUPTED`. [Interruption record](raw/stability/local-interrupted.json)
