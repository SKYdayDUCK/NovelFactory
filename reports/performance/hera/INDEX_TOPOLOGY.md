# Fast status reads with two real index workers

Eight client identities (four REST clients, four initialized MCP SDK sessions) released 128 verified status requests per arm. Separate client, API and PostgreSQL processes were pinned to one CPU (8). Each new migrated DB had two reader works and a third branch with 60 real transfers. Each loaded arm drained all 60 distinct index receipts and verified the final authoritative timeline watermark. Quiet had the same index backlog with workers stopped. No model calls.

All arms had two API business slots, two authentication slots and six product database connections, zero overflow, plus one observation connection attached to the admin database. The benchmark capped the production controller before serving so pool size changes could not silently change API concurrency. Shared allocated the six connections to one API/index pool. Logical also limited its two index threads to one CPU_COMPUTE permit. Pools used the same process with four API and two dedicated worker connections. Process used that same 4+2 split with index execution in a second Python process.

| Arm in execution order | Reads/s | Read P95 / P99 ms | Index/s during reads / through drain | API CPU s | PG backend CPU s | API queue wait sum s | Peak API checkout |
|---|---:|---:|---:|---:|---:|---:|---:|
| Quiet | 40.82 | 282 / 350 | 0 / 0 | 1.88 | 0.48 | 6.19 | 4 |
| Shared | 25.16 | 407 / 467 | 6.29 / 8.75 | 3.17 | 1.11 | 13.56 | 6 |
| Logical | 19.29 | 645 / 740 | 3.01 / 5.50 | 3.60 | 1.21 | 16.91 | 6 |
| Pools | 20.07 | 558 / 663 | 5.49 / 6.91 | 3.75 | 1.10 | 19.46 | 4 |
| Process | 19.58 | 500 / 601 | 4.44 / 6.54 | 2.32 | 1.04 | 17.90 | 4 |
| Process | 18.16 | 588 / 697 | 4.26 / 6.45 | 2.44 | 1.14 | 19.78 | 4 |
| Pools | 19.67 | 550 / 606 | 5.22 / 6.74 | 3.89 | 1.11 | 18.39 | 4 |
| Logical | 6.93 | 12871 / 13066 | 1.03 / 2.75 | 5.08 | 5.67 | 90.91 | 6 |
| Shared | 18.67 | 713 / 820 | 4.81 / 3.90 | 3.90 | 1.50 | 18.91 | 6 |
| Quiet | 35.80 | 314 / 373 | 0 / 0 | 2.02 | 0.52 | 7.43 | 4 |

The shared index load reduced fast throughput by 38.4% and 47.8% relative to adjacent quiet controls. In the first shared arm PostgreSQL CPU more than doubled, all six connections were checked out, and 14 of 85 read-window snapshots observed a transaction-ID lock wait. Quiet had zero of 51. Physical worker isolation limited API pool checkout to four but left API and index execution on the same CPU and database. No arm met the 100 ms read P95 target.

PG CPU sums first-to-last per-PID samples during the read window. API CPU includes colocated threads; raw worker usage for the separate process includes the read and drain phases. Each sample includes per-PID RSS, physical I/O, active DB waits and session state. The overlap boundary in this run used the server timestamp immediately before the release acknowledgement to the client. The updated driver records the exact client release. First-sweep backends had zero observed physical read/write bytes; cached I/O and background PG writes are outside that measure. The reverse logical arm contained a 12.52 s sampler gap, about 177 MiB of physical reads and a 12.49 s SQL call. This arm has material host/I/O interference. Measured CPU, lock and queue pressure establish concurrent costs; their individual causal shares of the throughput regression are unresolved.

All 1,280 status requests and 480 index receipts were verified. The experiment does not establish a general benefit of physical worker isolation under higher CPU budgets. No deployment default changed. Replay on Linux with `python -m benchmarks.hera_index_topology --output comparison.json.gz` against an isolated PostgreSQL 17 administrative URL and explicit CPU affinity. [Driver](../../../benchmarks/hera_index_topology.py) · [All ten trials and per-event records](raw/index-topology/comparison.json.gz)
