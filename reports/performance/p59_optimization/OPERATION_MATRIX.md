# Original PG17 operation samples

The 18 scenario checks in raw/matrix-final/junit.xml passed. Durations cover successful calls and expected refusals from correctness tests. SQL is the parent thread's cursor-call count; MCP children and HTTP server processes have separate SQL sessions. RSS is the parent high-water mark. Nested calls and concurrent threads must not be added. Percentiles use nearest rank. With fewer than 100 observations P99 is the sample maximum. These calls have varied real fixture inputs, and the table describes the observed distribution rather than independent stationary throughput. Exact tests and command are in scripts/matrix.sh.

| Operation | Calls | Returned | P50 ms | P95 ms | P99 ms | CPU P50 ms | SQL P50 |
| --- | --- | --- | --- | --- | --- | --- | --- |
| 1 Task creation | 68 | 68 | 11.34 | 16.30 | 20.78 | 7.96 | 15 |
| 2 Context compile | 66 | 65 | 48.85 | 100.56 | 195.51 | 35.03 | 48 |
| 3 World STATE query | 13 | 12 | 14.34 | 34.62 | 34.62 | 11.23 | 16 |
| 4 Historical TIMELINE | 3 | 2 | 20.62 | 26.16 | 26.16 | 17.99 | 18 |
| 5 Agent dispatch | 43 | 42 | 80.10 | 532.40 | 1161.55 | 44.41 | 90 |
| 6 Guarded world writes | 24 | 24 | 56.15 | 80.93 | 140.51 | 40.81 | 63 |
| 7 Redis stream read/reclaim | 30 | 30 | 0.45 | 3.96 | 5.41 | 0.26 | 0 |
| 8 MCP call over stdio | 10 | 10 | 42.35 | 173.94 | 173.94 | 0.99 | 0 |
| 9 Model routing | 12 | 12 | 1.59 | 6.89 | 6.89 | 1.18 | 2 |
| 10 Local HTTP generation | 16 | 15 | 2.86 | 274.42 | 274.42 | 1.86 | 0 |
| 11 Delegation receipt lookup | 2 | 0 | 2.34 | 3.63 | 3.63 | 1.85 | 3.5 |
| 13 Cross-volume advance | 43 | 43 | 7.62 | 113.82 | 140.98 | 5.73 | 14 |
| 14 Fine-grained creative review | 5 | 3 | 83.45 | 147.59 | 147.59 | 63.11 | 136 |
| 15 Statistics read | 2 | 2 | 6.71 | 6.80 | 6.80 | 3.31 | 8 |
| 16 Recovery | 25 | 25 | 17.45 | 100.52 | 127.45 | 6.17 | 15 |

## 12. Concurrent project scheduling and provider failures

Each authorized supervisor has a different work, budget and task. Endpoints listen on loopback. Idle admission is retried on the same task. Each task receives exactly one model Attempt. A simulated 503 after send retains its UNKNOWN Attempt. The healthy endpoint completed all its independently routed work.

| Works | Mode | Endpoint sends 1/2 | Stored results | UNKNOWN | Stored tasks/s | Max latency ms |
| --- | --- | --- | --- | --- | --- | --- |
| 1 | fixed | [1, 0] | 1 | 1 | 2.59 | 385.79 |
| 1 | mixed | [1, 0] | 1 | 1 | 4.74 | 208.07 |
| 1 | failure | [1, 0] | 0 | 1 | 0.00 | 289.63 |
| 2 | fixed | [2, 0] | 2 | 2 | 3.85 | 517.70 |
| 2 | mixed | [1, 1] | 2 | 2 | 5.01 | 394.64 |
| 2 | failure | [1, 1] | 1 | 2 | 2.58 | 387.10 |
| 4 | fixed | [4, 0] | 4 | 4 | 3.92 | 1013.30 |
| 4 | mixed | [2, 2] | 4 | 4 | 4.46 | 895.54 |
| 4 | failure | [2, 2] | 2 | 4 | 2.70 | 739.63 |

See raw/multi-project.json for per-project results, waiting and peaks. The first one-poll-only attempt is under raw/attempted; it exposed legitimate quota deferral when more projects competed.
