# Fast reads and workflow progress

All comparisons return the original authorization-filtered response. A uses the original full read. B uses the fenced task metadata query or the per-branch workflow projection, as identified below. Normal remote MCP business calls also pay the original tool invocation audit cost. Timings include full loopback request and response.

## Task metadata

Real A/B/B/A, ten authorized lanes, two works, four database connections, 50 requests per arm. No client retries. Every one of the 1,000 responses matched its task, generation, state and frozen WorldRef. Each cell below lists the two adjacent comparisons in execution order. SQL is the number of application cursor executions per request, including transport-specific accounting. [Raw 20 runs](raw/paired/) · [Driver](../../../benchmarks/hera_mixed.py)

| MCP share | A→B goodput/s | B→A goodput/s | A vs B SQL/request | B P95 ms |
|---|---:|---:|---:|---:|
| 0% | 97.7→107.8 | 114.9→81.1 | 5→3 | 143, 115 |
| 20% | 67.2→75.2 | 59.7→65.4 | 7.6→5.6 | 161, 308 |
| 50% | 56.8→47.5 | 60.6→48.6 | 11.5→9.5 | 284, 211 |
| 80% | 43.9→51.6 | 44.6→39.8 | 15.4→13.4 | 243, 279 |
| 100% | 41.4→45.0 | 38.6→43.5 | 18→16 | 274, 366 |

Two saved SQL executions per request did not produce a repeatable throughput win across all mixes. The 3× and P95 <100 ms gates failed. Keep the singleton query opt-in through NF_HTTP_FENCED_TASK_STATUS. For pre-formed multi-ID lookups see the separate batch comparison.

## Workflow projection

On an existing branch with 5,000 tasks, four clients shared one resume grant, with two actual MCP sessions and two REST clients. Each A/B/B/A arm read the full authorized workflow projection 128 times. All 512 responses matched the original result, including counts, blockers and grant expiration. The maintained relation occupied 32,768 bytes in this fixture. Both variants executed 2,368 SQL statements per arm; their SQL work per statement differs.

| | A1 | B1 | B2 | A2 |
|---|---:|---:|---:|---:|
| Correct responses/s | 20.85 | 23.81 | 24.38 | 22.31 |
| P95 ms | 271.64 | 240.71 | 230.64 | 284.56 |
| P99 ms | 307.06 | 265.86 | 251.78 | 429.07 |

Relative throughput gains were 14.2% and 9.3%. This comparison measures a populated branch under reads. It does not quantify the cost of maintaining a highly contended branch's counter during simultaneous writes. The projection remains opt-in at both reader and branch level. Run `python -m benchmarks.hera_progress --tasks 5000 --reads 128 --output /tmp/progress.json.gz`. See raw revision, environment and request records in [progress-5000.json.gz](raw/progress-5000.json.gz).

The initial development run overlapped a code change in a lazily imported remote adapter and failed before measuring. The reported repetition used a frozen checkout. The workflow projection and delta trigger reproduce authoritative state across a backfill, subsequent commits and a rolled-back transition. A revoked reader fails the authority check. [Regression receipts](raw/progress-delegation.xml)
