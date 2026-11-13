# Complete role-task and role-context reads

The reference is the previously shipped opt-in compact role read at eb1a75e, with shared slots, eight independent authenticated actors on two works, two PostgreSQL connections with zero overflow, an initialized SDK per client, and exact object equality on both tools. A separate client on CPU 6 calls a Uvicorn process sharing CPU 7 with PostgreSQL 17.11. Alternating the two tools exercises context payloads as well as task metadata. Every successful start and finish had a matching Outbox and each successful finish retained the exact context evidence ID.

| Mirrored comparison | Correct reads/s A, B, B, A | SQL per call A → B | Transactions A → B |
|---|---|---:|---:|
| Remove duplicate role discovery at valid invocation | 21.13, 19.92, 32.35, 20.06 | 28 → 19 | 5 → 4 |
| Add the atomic audit and Outbox write to the compact role read | 27.55, 28.86, 18.36, 12.61 | 19 → 17 | 4 → 4 |
| Old compact path against all three changes, including one owner-scoped audit lookup | 9.24, 34.04, 28.06, 20.92 | 28 → 15 | 5 → 4 |

All 2,560 replies matched their full role-specific objects across these twelve arms. Some arms overlapped other scheduled qualification work on the same host. The combined baseline run that achieved 9.24/s overlapped a separate concurrency burst. In the mirrored block the final control had P95/P99 553.7/631.5 ms; the adjacent candidate had P95/P99 643.5/787.5 ms. The P99 increased by about 24.7%, above the 20% tolerance. No general default or 3× end-to-end claim follows from the SQL reduction. The archived candidate and release sources are distinguished by their Git trees; raw reports carry the local experiment SHA. [All arms](raw/final-round/)

The first 1,000-client experiment acknowledged all exact task results and 2,000 audit/Outbox pairs in 57.70 s. The source checkout changed while the fixture was provisioned. Its 17 SQL/call confirms the paired-write version was loaded, but its end-of-run SHA refers to a later revision. Exclude this arm from revision-matched comparisons; retain the raw file. The revised driver checks the source tree at the start, in the spawned server, and after the measurement. [Exploratory record](raw/final-round/role-burst-probe.json.gz)

The clean 1,000-client experiment verified its product tree f914f3104cd66a5cf32daab24dcee709958b4193, unchanged heads, zero Provider attempts, all 1,000 responses, 2,000 audits with Outbox, 15,000 statements, and 4,000 transactions. Its 30.04 s drain was below the historical 35.26 s. P95/P99 were 28.21/29.33 seconds. Client and server peak RSS were 873,164/109,636 KiB. This workload keeps a distinct SDK session for every actor and reads the whole role-task object. Arrival, authentication and permission checks remain separate from task creation or model execution. [Clean raw trace](raw/final-round/role-final-1000.json.gz)

Run the same driver from each clean product worktree with its source, Workshop package and root on PYTHONPATH. Pin the PG17 server to CPU 7, use a disposable NF_TEST_DATABASE_URL, and run the same legacy listener for timing parity with these results. The separate TCP listener experiment also applies to these HTTP-based measurement tools.

```sh
taskset -c 6 python /path/to/hera_role_delivery.py --clients 8 --repeats 32 --context --server-cpu 7 --output roles.json.gz
taskset -c 6 python /path/to/hera_role_delivery.py --clients 1000 --server-cpu 7 --output burst.json.gz
```

The first stdio test invocation lacked the package path in the child interpreter. After installing the worktree in the child's environment, all 11 role/MCP tests passed. Fourteen additional tool-entry, audit and mixed-ingress cases passed on the paired-write path, including conflicting Outbox rollback. A draft audit patch failed Python import before execution; its log is preserved alongside the subsequent passing result. [Regression transcripts](raw/final-round/)
