# Repeated batch, cache, notification and delivery tests

The established host batch and coalescing drivers used real loopback HTTP and Streamable HTTP MCP with a shared client/server interpreter. These are service-level replications. The split-process matrix and SDK-specific ablations report separate results. Source of these legacy-driver replications was 861b8e8174f93c259f91fee6d8200942fe5f0fe5. They ran on a shared host; individual percentile differences across workloads should not be treated as deployment guarantees. [Raw records](raw/component-reruns/)

## Authorized batching

Each row runs direct / candidate / candidate / direct. Counts are individual status records returned and actual wire calls are listed separately. A batch contains only an installed work/branch/principal's own tasks. High-load adaptive grouping returned 1,000 correct records per arm with 140 wire calls in either adaptive arm. The two gains relative to the adjacent direct arms were 3.35× and 5.38×. At low load the first paired adaptive P99 increased. Keep batching explicit or opportunistic when compatible requests are already available.

| Profile and strategy | Correct records/s in order | P99 ms in order | Wire calls in order |
|---|---:|---:|---:|
| low · adaptive | 32.7 / 33.4 / 30.5 / 30.1 | 145.4 / 288.6 / 234.8 / 336.7 | 64 / 62 / 64 / 64 |
| low · fixed | 33.4 / 33.0 / 30.6 / 31.2 | 236.7 / 66.5 / 480.8 / 200.6 | 64 / 64 / 56 / 64 |
| medium · adaptive | 30.8 / 170.2 / 156.0 / 36.5 | 7360.7 / 686.6 / 885.1 / 5973.5 | 256 / 43 / 40 / 256 |
| medium · fixed | 47.2 / 211.7 / 148.6 / 25.5 | 4496.0 / 473.1 / 943.4 / 8718.6 | 256 / 53 / 51 / 256 |
| high · adaptive | 39.5 / 132.1 / 107.3 / 19.9 | 24989.1 / 7332.8 / 9000.4 / 49721.1 | 1000 / 140 / 140 / 1000 |
| high · fixed | 43.9 / 250.8 / 252.3 / 36.2 | 22597.1 / 3776.1 / 3870.1 / 27428.9 | 1000 / 140 / 140 / 1000 |

## Overlapping reads

Order off / on / on / off. This bounded deduplication window is limited to the same principal, binding, task and status contract and discards each completed result. The data do not support unconditional activation.

| Profile | Correct requests/s | Joined readers |
|---|---:|---:|
| serial | 13.5 / 14.0 / 13.4 / 14.9 | 0 / 0 / 0 / 0 |
| hot | 27.7 / 30.3 / 28.3 / 7.0 | 0 / 4 / 6 / 0 |
| distinct | 11.7 / 12.6 / 14.0 / 29.3 | 0 / 0 / 0 / 0 |

## Fixed-cut world query and wake-up

Four arms made 24 authorized fixed-cut world queries apiece, with cache capacities 0/32/32/0. The existing cache recorded 0/23/23/0 hits and returned the same verified payload and dependency handle on all 96 queries. P95 was 85.5 / 51.3 / 63.6 / 82.5 ms. The delivery path still rechecks current grants and version constraints. This cache stores immutable-cut results under their original scope; mutable singleton task status remains authoritative. [Production query delivery](../../../src/novel_factory/application/queries.py) · [Revocation and invalidation cases](../../../tests/queries/test_query_delivery.py)

Sixteen ordinary authorized tasks were discovered with 15 ms polling and sixteen using the existing PostgreSQL LISTEN channel. The database emits an empty hint only after Outbox commit; the consumer establishes LISTEN first, reads the authoritative task, waits, then rereads under the same principal. A 300 ms reconciliation timeout covers a lost hint. Every task's ID, state, generation and WorldRef matched. Listening used 32 authorized reads versus 123 with polling, plus one separately budgeted LISTEN connection per active listener. The sequential short-delay workload measures read amplification; it does not establish a deployment-wide notification throughput. [Driver](../../../benchmarks/hera_cache_wake.py)

## Model waits and Workshop

The two model slots used a fixture Provider. All 400 single-task status requests and 320 project-progress responses returned their expected state. Loaded and quiet single-task runs produced 22.56 / 20.88 / 16.37 / 22.24 correct reads/s; loaded P95 exceeded one second. This test did not send paid model calls. In the independent two-service Workshop regression, both cross-protocol completion directions and stdio tests completed (4/4). Workshop tests ran against isolated PostgreSQL and Redis, with 124 passes. Immediate batching and session reuse in the 128-message relay took 3748.1 / 380.2 / 1257.8 / 6247.6 ms in ungrouped/grouped/grouped/ungrouped order. SQL inbox effects and Redis pending state were checked by the original relay driver.
