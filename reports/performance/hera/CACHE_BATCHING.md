# Task status coalescing on a shared MCP and REST service

Source 9727013. Pre-registration 40d4135. Linux single-process application, PostgreSQL 17 with eight pool connections and zero overflow, six operation and two authentication slots, mcp 1.29.0. Clients and Uvicorn share this benchmark process. SQL counts are SQLAlchemy cursor statements, and CPU time includes clients and server but excludes the PostgreSQL processes. Six samples per serial arm and 36 per concurrent arm limit tail precision. Other HERA tests shared the machine during these runs. Full results, timing for each request, SQL statement/transaction counters, connection peaks and dependency versions are in [the raw artifact](raw/coalescing/measured.json.gz). Run `python -m benchmarks.hera_status_coalescing --output measured.json.gz` with NF_TEST_DATABASE_URL pointing to an isolated server that permits database creation.

| Requests | Arm | Correct | Reads saved | SQL | Correct/s | P95 ms | P99 ms | CPU s |
|---|---|---:|---:|---:|---:|---:|---:|---:|
| serial | A1 | 6/6 | 0 | 18 | 12.59 | 129.51 | 129.51 | 0.147 |
| serial | B1 | 6/6 | 0 | 18 | 16.32 | 87.64 | 87.64 | 0.089 |
| serial | B2 | 6/6 | 0 | 18 | 14.12 | 81.11 | 81.11 | 0.089 |
| serial | A2 | 6/6 | 0 | 18 | 13.55 | 93.27 | 93.27 | 0.132 |
| hot | A1 | 36/36 | 0 | 342 | 27.54 | 229.51 | 253.57 | 0.964 |
| hot | B1 | 36/36 | 5 | 332 | 22.28 | 330.15 | 337.72 | 1.081 |
| hot | B2 | 36/36 | 5 | 332 | 26.95 | 263.57 | 305.19 | 0.962 |
| hot | A2 | 36/36 | 0 | 342 | 25.33 | 307.58 | 325.57 | 1.030 |
| distinct | A1 | 36/36 | 0 | 342 | 28.14 | 241.45 | 264.81 | 0.887 |
| distinct | B1 | 36/36 | 0 | 342 | 26.85 | 307.32 | 341.49 | 0.835 |
| distinct | B2 | 36/36 | 0 | 342 | 27.89 | 227.75 | 250.14 | 0.844 |
| distinct | A2 | 36/36 | 0 | 342 | 16.58 | 565.34 | 634.24 | 1.608 |


All 312 requests returned the expected authoritative state. Each hot candidate arm combined five reads, saving ten SQL statements including the live-check logic. B1 achieved 22.28/s versus A1's 27.54/s, while B2 achieved 26.95/s versus A2's 25.33/s. B1 hot P95 rose from 229.51 ms to 330.15 ms. The first distinct arm also regressed on tails and saved zero SQL. These results fail the predeclared adoption gate. The server default remains direct, and the candidate can be disabled with NF_HERA_COALESCE_TASK_STATUS=0.

The initial driver passed milliseconds into a shared helper expecting seconds. Its aggregate percentile fields are invalid. Its raw rows are preserved [separately](raw/coalescing/invalid-summary-first-run.json.gz). That process started from 135d3b8; git HEAD advanced during the run, before its environment record was written. The corrected driver in the measured artifact derives percentile inputs from end minus scheduled in seconds.

Fixed/adaptive set-based SQL and mixed lease/status microbatches have separate historical PG17 evidence in [the database experiment](https://github.com/SKYdayDUCK/NovelFactory/blob/9364b154941bfbbe95a57fee5ceb600f5219fa5b/reports/performance/p59_database/ADAPTIVE_REVIEW.md). Those workloads do not establish a benefit in the HERA mixed transport workload. HERA leaves global microbatching, immutable knowledge-result caching and status TTL caching disabled. This run measured no such mechanism.

The subsequent [same-owner multi-ID experiment](STATUS_BATCHES.md) measures the explicit bounded set SQL endpoint and compares demand-sensitive and fixed-window gathering over real HERA mixed traffic, including a 1,000-logical-read burst.
