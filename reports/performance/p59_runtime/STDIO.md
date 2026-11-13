# Independent MCP stdio throughput

Two separately provisioned character actors read their own task and frozen context through the actual MCP SDK. Each actor had a distinct work, principal, task, context, process, client session and PostgreSQL connection pool. The load generator compared every returned JSON object with the complete expected object read directly from that actor's database record before loading. A mismatched object, unexpected error or unaccounted arrival fails the run. A pressure response and a client drop each retain a separate status and count zero toward verified throughput.

Candidates used two admission slots and a two-connection pool per process. The frozen baseline had the same connection pool and submitted calls through its default thread executor. Both used one driver process with two SDK sessions and one additional PostgreSQL connection for fixture setup and post-phase observation. Every phase finished with two observed PostgreSQL sessions per binding. No database sampling ran in the timed window. Initializing the two servers, listing tools and warming each tool once per role all finished before the measured requests.

## Measurements

Each phase offered 240 reads, equally divided by role and tool. A closed-loop phase used two continuously active callers per binding. The following open-loop phase scheduled 12 total arrivals per second across both bindings, with a limit of four client requests in flight per binding. Timing used monotonic time from scheduled arrival to the complete parsed reply, including driver scheduling delay. Nearest-rank percentiles include refusals. Raw samples also include the dispatch timestamp and dispatch-to-reply duration. Throughput divides complete correct responses by actual elapsed time through final completion, including the scheduling window. There were zero client drops, wrong returned objects, provider Attempts or world-head changes in these four runs.

| Phase / revision | Seconds | Correct / pressure | Verified reads/s | P50 ms | P95 ms | P99 ms |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| Closed A1 | 4.26 | 240 / 0 | 56.32 | 59.67 | 96.26 | 307.80 |
| Closed B1 | 2.89 | 240 / 0 | 82.98 | 42.40 | 67.65 | 214.20 |
| Closed B2 | 3.36 | 240 / 0 | 71.53 | 49.53 | 72.12 | 171.54 |
| Closed A2 | 4.24 | 240 / 0 | 56.57 | 64.67 | 101.58 | 242.13 |
| Open A1 | 20.23 | 240 / 0 | 11.86 | 43.87 | 165.83 | 280.90 |
| Open B1 | 19.95 | 240 / 0 | 12.03 | 34.69 | 155.86 | 244.36 |
| Open B2 | 19.97 | 236 / 4 | 11.82 | 36.55 | 198.64 | 347.73 |
| Open A2 | 20.04 | 240 / 0 | 11.98 | 50.38 | 130.13 | 228.31 |

A is frozen main `434840593179e6c1faa6b7aeca3d7d747644506d`. B is `307f916eb90a65850e7ed1235c276284559272db`. Both ran the exact same driver bytes on Linux x86-64, Python 3.12.14 and PostgreSQL 17.11 with fsync and synchronous commit enabled. Nine logical CPUs were visible. Other jobs shared the host. Each run started a fresh migrated cluster and new stdio processes. A longer preliminary candidate trial also observed one open-loop `BACKPRESSURE` out of 240 arrivals. In that trial a strict all-OK gate exited nonzero while preserving the complete measurement. The paired driver reports expected overload separately from incorrect responses. [A1](raw/stdio/A1.json.gz) · [B1](raw/stdio/B1.json.gz) · [B2](raw/stdio/B2.json.gz) · [A2](raw/stdio/A2.json.gz) · [Preliminary trial](raw/stdio/candidate-pilot.json.gz)

The paired closed-loop runs cover a small population already provisioned and warmed. At the lower, fixed open arrival rate, throughput was close to the offered rate and the second candidate run rejected four requests. Database connections, CPU, memory and latency for 5000 independent OS processes remain unmeasured. HTTP tests, provider execution, durable workflow completion and human evaluation use other workloads.

## Reproduction

Keep the driver from B fixed. In both detached worktrees run `uv sync --locked --group dev`. Set `STDIO_BENCH` to the absolute path to B's driver and `PG_BIN` to the native PostgreSQL 17 binary directory. From the runtime worktree for each label, run the following in A1, B1, B2, A2 order. Use a different output path for each label. The driver records the revision of the installed Kernel at the beginning of each run.

```sh
.venv/bin/python scripts/with_postgres.py --migrate --pg-bin "$PG_BIN" -- \
  .venv/bin/python "$STDIO_BENCH" "$OUTPUT_JSON" --samples-per-role 120 --offered-rps 12
```

[Driver](../../../benchmarks/bound_stdio_throughput.py) · [Single-host idle and reconnect observations](../../code_review/p59_runtime/entrypoints.md)
