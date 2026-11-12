# Performance and evaluation

[中文](../zh-CN/10-性能与实验.md) · [Capacity issue #13](https://github.com/SKYdayDUCK/NovelFactory/issues/13)

## What the measurements cover

The baseline was main `5e322e8af8ddb4465f68f8cab84c9685832461a9`. Runs used Python 3.12 and PostgreSQL 17.11 with checksums, fsync and synchronous commit on a shared Linux host. The historical AT-30 load generator and its service shared two visible CPUs. A logical agent is a principal in the workload. An HTTP start, a valid business response, a unique world commit and a provider request are four different counts.

On a 250-item world with four revisions, median context compilation took **47.34, 60.99 and 131.92 ms** for 1, 20 and 100 subjects respectively. Each compile made 48 SQL calls. This benchmark excludes population setup. Its first application read already had warm filesystem and database buffers. A separate instrumented 100-subject run spent 217.94 of 354.48 ms in contract validation. The allocation trace peaked at 4,886,725 bytes. Instrumentation affects elapsed time.

## Changes measured in isolation

| Measured operation | Baseline | Candidate | Conditions |
| --- | ---: | ---: | --- |
| Guarded accept, 5 items | 39.60 ms | 37.39 ms | Median of 12 paired trials; 60 to 57 SQL calls |
| Guarded accept, 100 items | 72.40 ms | 68.35 ms | Same transaction boundary and SQL counts |
| Update 64 affected scopes | 36.106 ms | 2.995 ms | Median of 10 paired trials; 66 to 3 SQL calls |
| Update 256 affected scopes | 145.909 ms | 6.984 ms | 258 to 3 SQL calls |
| Resolve checked-in operator routing rules | 20.501 µs | 14.883 µs | P50, pure Python ABBA comparison |
| Resolve 256 routing rules | 942.809 µs | 616.548 µs | P50; one-rule P99 rose from 70.706 to 79.319 µs |

A sorted, deduplicated scope update runs in the existing transaction. The timer stops immediately before commit; each pair rolls back to the same proposal. A separate durable commit and replay checked the resulting receipt. Routing selects the best matching rule without sorting all the matches. A separate validator experiment stopped at the first invalid field. This helped rejected payloads but slowed the single-subject successful compile. Copy avoidance also slowed complete compiles. Both experiments stayed out of the product.

## Capacity and fairness

| Original load | Observed outcome |
| --- | --- |
| 10, 50, 200 and 500 mixed | The recorded mixed business/capacity runs passed. One-task behavior is covered separately by task and recovery tests. |
| 50 and 200 overload | Capacity unmet. At 200, client call starts reached 41.01/s on the baseline and 35.39/s on the candidate against the original 950/s minimum. Separately, the transport started sending headers at 40.08/s and 27.80/s. Business recovery passed. The runs had different CPU contention. |
| 500 steady reads | Two 600-second runs at 20 requests/s each completed 12,000/12,000. P50/P95/P99 were 199.65/1071.38/1519.10 ms and 130.08/649.23/996.15 ms. |
| 1000 open arrival | Out of 5,000 attempts, the baseline had 1,319 valid replies, 3,627 HTTP 429s and 54 transport errors. The candidate had 1,434 valid replies, 3,534 HTTP 429s and 32 transport errors. Both missed the gate. |
| Four books, one model slot | With three equal-sized tasks per book, all four finished three. Fixture endpoints exercised fixed, mixed and failed providers for one, two and four books. |

The 200-agent baseline recorded 716 authentication-admission rejections, 80 ingress rejections, 46 business-admission rejections and seven wait timeouts. The pool reached 8/8 connections, but only two observation samples saw transaction-ID lock waits. The same load client with a 2,104-byte fixed response achieved 140.59 and 153.94 wire starts/s in two calibration runs. Both the client and application limit this setup. It sent no requests to commercial model providers. The original capacity gate remains **NOT_MET**. One-hour and multi-hour sustained runs are **NOT_EXECUTED**.

## Reproduce a workload

Use a clean Linux checkout and a PostgreSQL 17 installation as in the quick start. The three commands below run the original 200-agent overload, 1000-agent open-arrival and 500-agent read-soak workloads. Preserve each JSON output and the process exit status, including a nonzero capacity result. Use a new output directory for each repetition. Compare paired experiments under the same CPU allocation, data and initialization conditions.

```bash
export PATH="$PWD/.venv/bin:$PATH"
out=$(mktemp -d)
NF_POOL_SIZE=8 NF_POOL_OVERFLOW=0 NF_BENCH_DIAGNOSTICS=1 \
python scripts/with_postgres.py --log-dir "$out/pg-overload" -- \
  python -m benchmarks.p2_behavioral_load --agents 200 --profile overload \
    --hot-fraction .8 --arrival-rps 10 --open-loop-rps 1000 --rounds 5 \
    --max-attempts 5 --retry-budget-seconds 30 --gate-scope capacity \
    --service-mode isolated-process --api-processes 1 --output "$out/overload.json"
python scripts/with_postgres.py --log-dir "$out/pg-open" -- \
  python -m benchmarks.p3_open_arrival --agents 1000 --rounds 5 --arrival-rps 1000 \
    --hot-fraction .8 --api-processes 2 --output "$out/open.json"
python scripts/with_postgres.py --log-dir "$out/pg-soak" -- \
  python -m benchmarks.p3_stability --agents 500 --seconds 600 --read-rps 20 \
    --api-processes 2 --output "$out/soak.json"
```

A genuine Codex CLI process with a fixture upstream exercises the host protocol. The loopback HTTP adapter exercises requests, cancellation, provider faults and usage recording. Billable inference, provider latency on the public network, settled invoices and ratings by human readers each require their own evidence. [Benchmark programs](../../benchmarks) · [Quick start](02-quick-start.md)
