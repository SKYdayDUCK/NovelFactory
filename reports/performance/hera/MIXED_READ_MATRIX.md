# Mixed REST and MCP read workload matrix

Measured the fenced task-status reader over real TCP with the production Python MCP SDK and PostgreSQL 17 on Linux. This is the candidate-only coverage sweep from the frozen plan. See [the paired trials](raw/paired/) for comparative evidence. Every cell has 20 separately authenticated principals across two works, each with one distinct pre-created READY task. No worker or provider executes in this read-only workload. Each lane owns one authenticated REST or SDK client. It issues the next request after its previous response. All 20 lanes start from a common barrier. The active operation budget is two, the auth budget is two, and their shared connection pool has four connections and zero overflow. Application and client share one OS process. Each source file contains individual request sequence, lane, actual transport, UUID of the target, response check, dispatch and completion time. The benchmark records the peak simultaneous client requests; it does not sample exact OS TCP socket counts.

Across 30 cells, 43,750 of 43,750 offered reads passed the response check. Exactly 21,875 used remote MCP and 21,875 used REST. PostgreSQL confirmed 20 READY tasks per cell and 600 distinct seeded tasks in the full sweep. The same 20 principals were reused over the main 25 runs; the five supplemental N=50 runs used a different disposable database and 20 separately provisioned principals. Every cell had 20 peak client operations, with 0/4/10/16/20 initialized SDK clients. All remaining lanes used REST. The 5,000-read cells therefore document sustained offered operations, not 5,000 simultaneous clients.

| Offered | MCP share | REST calls | MCP calls | Verified | Goodput/s | P50 ms | P95 ms | P99 ms | Drain s | DB peak | Queue peak |
|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| 50 | 0% | 50 | 0 | 50 | 73.94 | 244.0 | 338.3 | 365.4 | 0.68 | 4 | 13 |
| 50 | 20% | 40 | 10 | 50 | 53.00 | 347.4 | 392.9 | 441.6 | 0.94 | 4 | 18 |
| 50 | 50% | 25 | 25 | 50 | 26.78 | 648.1 | 791.5 | 861.3 | 1.87 | 4 | 18 |
| 50 | 80% | 10 | 40 | 50 | 22.52 | 693.0 | 1108.2 | 1176.4 | 2.22 | 4 | 18 |
| 50 | 100% | 0 | 50 | 50 | 16.82 | 1034.4 | 1273.3 | 1347.5 | 2.97 | 4 | 18 |
| 200 | 0% | 200 | 0 | 200 | 83.71 | 232.2 | 316.4 | 332.2 | 2.39 | 4 | 16 |
| 200 | 20% | 160 | 40 | 200 | 61.39 | 277.2 | 473.2 | 542.7 | 3.26 | 4 | 18 |
| 200 | 50% | 100 | 100 | 200 | 41.89 | 427.7 | 671.7 | 716.4 | 4.77 | 4 | 18 |
| 200 | 80% | 40 | 160 | 200 | 31.33 | 606.0 | 831.1 | 944.0 | 6.38 | 4 | 18 |
| 200 | 100% | 0 | 200 | 200 | 22.29 | 785.9 | 1419.2 | 1660.8 | 8.97 | 4 | 18 |
| 500 | 0% | 500 | 0 | 500 | 65.09 | 247.9 | 574.8 | 1333.7 | 7.68 | 4 | 17 |
| 500 | 20% | 400 | 100 | 500 | 38.56 | 462.2 | 768.2 | 923.8 | 12.97 | 4 | 18 |
| 500 | 50% | 250 | 250 | 500 | 31.18 | 584.3 | 925.6 | 1105.9 | 16.03 | 4 | 18 |
| 500 | 80% | 100 | 400 | 500 | 22.36 | 855.4 | 1160.2 | 1313.5 | 22.36 | 4 | 18 |
| 500 | 100% | 0 | 500 | 500 | 25.32 | 776.9 | 993.3 | 1074.7 | 19.75 | 4 | 18 |
| 1000 | 0% | 1000 | 0 | 1000 | 95.94 | 204.7 | 276.6 | 321.5 | 10.42 | 4 | 18 |
| 1000 | 20% | 800 | 200 | 1000 | 66.46 | 274.9 | 398.8 | 534.3 | 15.05 | 4 | 18 |
| 1000 | 50% | 500 | 500 | 1000 | 44.17 | 425.7 | 593.1 | 732.3 | 22.64 | 4 | 18 |
| 1000 | 80% | 200 | 800 | 1000 | 32.27 | 565.5 | 884.0 | 954.4 | 30.99 | 4 | 18 |
| 1000 | 100% | 0 | 1000 | 1000 | 25.30 | 781.8 | 1049.8 | 1290.3 | 39.53 | 4 | 18 |
| 2000 | 0% | 2000 | 0 | 2000 | 71.01 | 265.8 | 409.0 | 591.9 | 28.16 | 4 | 18 |
| 2000 | 20% | 1600 | 400 | 2000 | 52.30 | 355.7 | 517.8 | 616.5 | 38.24 | 4 | 18 |
| 2000 | 50% | 1000 | 1000 | 2000 | 39.49 | 476.8 | 645.2 | 784.1 | 50.64 | 4 | 18 |
| 2000 | 80% | 400 | 1600 | 2000 | 33.86 | 567.3 | 788.7 | 960.9 | 59.07 | 4 | 18 |
| 2000 | 100% | 0 | 2000 | 2000 | 29.93 | 647.3 | 858.6 | 1001.2 | 66.83 | 4 | 18 |
| 5000 | 0% | 5000 | 0 | 5000 | 79.29 | 235.7 | 393.2 | 481.2 | 63.06 | 4 | 18 |
| 5000 | 20% | 4000 | 1000 | 5000 | 60.29 | 294.1 | 493.3 | 728.1 | 82.93 | 4 | 18 |
| 5000 | 50% | 2500 | 2500 | 5000 | 46.71 | 393.4 | 585.8 | 714.2 | 107.05 | 4 | 18 |
| 5000 | 80% | 1000 | 4000 | 5000 | 30.08 | 621.9 | 975.0 | 1395.2 | 166.23 | 4 | 18 |
| 5000 | 100% | 0 | 5000 | 5000 | 28.57 | 650.0 | 1084.4 | 1493.0 | 174.98 | 4 | 18 |

Latency here runs from client dispatch to response; raw files also contain release-to-response latency. The queue peak covers all admitted operations, including SDK discovery. The benchmark's response predicate checks the task ID, generation, READY state and exact work/branch/world revision. It was not a byte-by-byte comparison of all status metadata. Each cell ran once, on a shared Linux machine while other tests could run. These rates describe the recorded runs, not a supported capacity or sustained availability guarantee.

## Reproduction and evidence

Run from a clean checkout with the locked Python dependencies, a private PostgreSQL 17 administration URL in NF_TEST_DATABASE_URL and a writable output directory. The helper creates and drops a dedicated database. Raw files include exact source and driver SHA, hardware, dependency versions, peak RSS (process high-water mark), SQL/transaction counters and individual request results. [Raw matrix and checksums](raw/mix-matrix/manifest.json).

```sh
python -m benchmarks.hera_mixed --operation read --populations 50 200 500 1000 2000 5000 --mcp-pct 0 20 50 80 100 --clients 20 --order B --output /tmp/hera-matrix
```

Main frozen revision f5a1c51 assigns protocols by lane. With 50 requests over 20 lanes, its final partial round offered 9 and 24 MCP calls instead of 10 and 25 at 20% and 50%. All five original cells are retained in [the pilot folder](raw/mix-matrix/initial-lane-based-n50/). Revision 544cfb9 fixes the allocation of shorter and longer lanes; the exact N=50 cells were rerun on a new database with a 20-client driver. The N≥200 cells all divide evenly by 20 and their original raw counts matched their target percentages. Reproduce the historical cells with the source revision in the manifest, or use the corrected driver for new runs.

Turn off NF_HTTP_FENCED_TASK_STATUS in the deployment to restore the original metadata path. Benchmark fixtures, bearer tokens, objects and isolated databases were ephemeral; no test credential is included in the raw artifacts.
