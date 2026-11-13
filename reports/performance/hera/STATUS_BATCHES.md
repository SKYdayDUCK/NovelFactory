# Bounded multi-task status reads over MCP and REST

The ordinary single-task read remained active. A new explicit multi-ID request authorizes the work and branch, restricts every task to the same live principal/grant, joins the immutable input to the current task generation, and returns the full individual status records in the submitted order. If any ID fails its owner/branch fence, both transports reject the entire call. [Product interface](../../../docs/design/HERA_UNIFIED_MCP_HTTP.md) and [PG regression](raw/set-status.xml).

The [preregistered experiment](STATUS_BATCH_PLAN.md) used a clean detached checkout of `7e7dd4d`, one Linux client+server process with actual loopback TCP, Python 3.12, MCP SDK 1.29.0 and PostgreSQL 17.11. Database pool 4, overflow 0; two authentication and two operation slots. There were no model workers. Across 24 trials 10,560/10,560 logical reads returned the full expected record. There were 6,243 actual wire calls. The [raw files](raw/status-batches) include original due times, goodput, full per-read correctness, CPU time, SQL statistics, batch size per actual request and dependency-independent timing. A/B/B/A compares adjacent runs for each candidate, with A sending one ID per request.

| Traffic | Candidate | Actual wire calls A/B/B/A | Correct IDs/s A/B/B/A | Offered P95 ms A/B/B/A | Offered P99 ms A/B/B/A | SQL statements A/B/B/A | Paired gate |
|---|---|---|---|---|---|---|---|
| low | fixed | 64 / 56 / 53 / 64 | 24.1 / 31.3 / 32.1 / 28.4 | 757 / 434 / 499 / 375 | 777 / 454 / 526 / 406 | 608 / 588 / 550 / 608 | fail |
| low | adaptive | 64 / 62 / 62 / 64 | 30.7 / 32.2 / 32.3 / 26.6 | 288 / 281 / 250 / 579 | 300 / 305 / 286 / 602 | 608 / 578 / 578 / 608 | pass |
| medium | fixed | 256 / 40 / 44 / 256 | 39.0 / 154.3 / 175.7 / 38.7 | 5292 / 903 / 682 / 5339 | 5684 / 972 / 734 / 5698 | 2432 / 420 / 436 / 2432 | pass |
| medium | adaptive | 256 / 43 / 43 / 256 | 40.0 / 179.6 / 169.5 / 37.5 | 5128 / 589 / 724 / 5570 | 5489 / 637 / 761 / 5886 | 2432 / 432 / 432 / 2432 | pass |
| high | fixed | 1000 / 140 / 140 / 1000 | 36.7 / 192.7 / 216.3 / 38.6 | 25295 / 4489 / 4148 / 23926 | 26948 / 4925 / 4434 / 25575 | 9500 / 1470 / 1470 / 9500 | pass |
| high | adaptive | 1000 / 140 / 140 / 1000 | 40.9 / 252.0 / 209.7 / 42.3 | 22663 / 3491 / 3989 / 22000 | 24164 / 3861 / 4604 / 23405 | 9500 / 1470 / 1470 / 9500 | pass |

The low profile offered 64 logical IDs at 30 ms global intervals across four principal-bound clients. The medium profile offered groups of four per identity every 120 ms across eight clients. The high profile simultaneously made 1,000 logical status reads eligible across twenty principal-bound clients, ten SDK sessions and ten REST connections. A lane issued at most one wire request at a time and kept its own pending IDs. Half the *logical reads* used each transport; adaptive batch fill can change the actual wire-call ratio, which is available per request. The throughput measure counts correct task records returned per second; grouped responses legitimately amortize authentication, SDK audit and SQL. The high profile does not represent 1,000 client connections.

The fixed host algorithm waited up to 10 ms from original due time to fill eight slots; the adaptive host took already-due compatible IDs up to eight and passed an isolated singleton through the existing status endpoint without an intentional hold. Service slowdown can itself build a host queue even under paced arrivals. Low-load tail results are sensitive to the shared machine and include every originally offered ID, not only wire service time. Use the explicit set-read endpoint for already available, same-scope work. The server does not introduce a global fill delay or share payloads among principals. Old clients can keep the original endpoint and single-ID tools. API peak throughput for isolated independent grants, model work or world acceptance cannot be derived from per-ID throughput here.

Reproduce on isolated PG17:

```sh
python -m benchmarks.hera_status_batches --output /tmp/hera-status-batches
```
