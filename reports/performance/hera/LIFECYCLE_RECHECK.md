# Seven-call REST lifecycle against main

The HERA candidates have yet to demonstrate the frozen +30% correct full-lifecycle throughput gate across low, medium and high load. Independent API, client and PostgreSQL 17.11 processes used CPU 1, 0 and 2 respectively. Each new arm used a new database, two works, a separate active grant per lane, four application SQL connections, zero overflow, two authentication slots and two operation slots. Both arms used the HERA 0027 schema. Main (bcb03637) executed its original behavior. HERA enabled durable admission, compact creation, an empty suspended projection and fenced status. There were no model calls in this REST contract. The host was shared with other work. [Frozen plan](raw/lifecycle-final/frozen-plan.txt) · [Drivers, patches and original samples](raw/lifecycle-final/)

The first candidate (ca26f64) grouped task-input, execution, fairness and command rows within the authoritative transaction; allowed direct addressed claims to share the scheduler lock while unaddressed selection remained exclusive; narrowed ordinary live bearer lookup to an autocommit read; and bounded verification witnesses for interleaved world snapshots. It made 53 application SQL statements per cycle compared with 60 on the inherited opt-in HERA and 67 on main. It preserved input/Outbox/command records. Ordinary PostgreSQL BEGIN/COMMIT round trips dropped from 14 to 7 per cycle. SQLAlchemy's logical transaction events still count autocommit statements; they are not PostgreSQL commit counts.

Each line shows adjacent alternating controls. Warm rounds per client were 16, 8 and 4 for 2, 16 and 32 lanes. All 2,704 first-use and warm lifecycles, including 18,928 real HTTP responses, passed both protocol and database oracles for the two complete matrices. The database oracle checked every task, one execution, two receipts and three state events, terminal generation, no provider attempt and unchanged canonical heads. Row-level intervals, PostgreSQL query/lock/WAL and process observations are inside the raw files. Decoder: gzip JSON. Raw cursor traces use indices into the same file's SQL-shape catalog.

| Listener | Lanes | Main / HERA / HERA / main cycles/s | Paired changes | HERA request P95 / P99, ms |
|---|---:|---|---|---|
| Legacy | 2 | 4.38 / 4.22 / 3.89 / 4.12 | −3.7% / −5.6% | 90 / 128; 133 / 248 |
| Legacy | 16 | 10.22 / 14.54 / 14.45 / 11.30 | +42.3% / +27.8% | 231 / 403; 253 / 331 |
| Legacy | 32 | 11.88 / 11.23 / 12.24 / 9.07 | −5.5% / +34.9% | 1254 / 2276; 792 / 1478 |
| Production-style TCP | 2 | 10.85 / 12.10 / 8.02 / 11.93 | +11.6% / −32.7% | 56 / 75; 96 / 197 |
| Production-style TCP | 16 | 11.39 / 13.90 / 5.55 / 11.98 | +22.0% / −53.7% | 263 / 321; 1108 / 2026 |
| Production-style TCP | 32 | 12.67 / 15.76 / 13.79 / 10.68 | +24.4% / +29.1% | 461 / 641; 513 / 586 |

One legacy baseline at sixteen lanes measured 345.86 ms total PostgreSQL execution for the 64 addressed scheduler lock calls during 64 warm cycles; the telemetry continued to count another 192 command locks separately. Changing the addressed scheduler acquisition to a shared lock removed that exclusive scheduler query. The grouped-create/claim code reduced SQL/cycle from 60 to 55, then shared claims and joined immutable claim inputs to 53. Application CPU and business throughput varied on repeated trials. An ordinal pilot with grouped records measured 13.58 and 6.50 cycles/s despite identical SQL. The latter remains in the raw series. One complete TCP 16-lane candidate arm reached P99 2,026 ms on this host and remains in the matrix. [Independent TCP-only calibration](LISTENER_CALIBRATION.md)

## Further candidate after the two matrices

A new product experiment (9148aae) fused the owning work/active grant locks for status lookup and reused a private, immutable, boundary-validated HTTP task-create envelope. It kept the original binding and permission fences and reduced the workload to 50 SQL/cycle. The full 16-lane A/B/B/A with six warm cycles per lane measured **10.685 / 15.873 / 13.243 / 10.697** valid cycles/s, gains of **48.6% and 23.8%** compared to adjacent controls. P95/P99 were 345/428, 217/283, 263/311 and 326/391 ms. All 448 cycles and 3,136 HTTP replies passed the oracle. Two earlier profile/launch mistakes and their logs remain recorded. Two candidate launches had been blocked by the driver's clean-checkout guard; the measured replay used a new detached checkout. 22 targeted PostgreSQL/authorization tests and a further 21 real HTTP/MCP, durable-task, scheduler and restart tests passed across these stages. The source patches are experimental; they have not been selected as release defaults. [Wire and DB trace for first candidate run](raw/lifecycle-final/10-joined.json.gz) · [Second run](raw/lifecycle-final/11-joined.json.gz) · [Verification](raw/lifecycle-final/transport-regression.xml)

## Reproduction

Use clean worktrees for bcb03637, the historical HERA head eb1a75e and the measured candidate. For ca26f64 apply candidate-ca26f64.patch to eb1a75e and create a local experiment commit. For 9148aae add joined-9148aae.patch and commit. Pass the actual local commit to the clean-tree guard. Use a PostgreSQL 17 administrative test DSN and enable pg_stat_statements preload, pg_stat_statements.track=all, track_io_timing=on and track_functions=pl. Pin the postmaster to the declared CPU before startup. Put the driver and its sibling standard_http_load.py in the same directory for both arms. The original seven_call.py imports each checkout's historical loopback launcher. The corrected seven_call_tcp.py imports the identical explicit-TCP launcher in both arms. Keep SQL/fsync and connection limits unchanged.

```sh
export PYTHONPATH="$PWD/src:$PWD/workshop/src:$PWD"
export NF_HERA_COMPACT_CREATE=1 NF_HERA_GROUP_TASK_RECORDS=1
export NF_HERA_DIRECT_CLAIM=1 NF_HERA_NARROW_AUTH=1
python "$EVIDENCE/seven_call_tcp.py" --product candidate --revision "$(git rev-parse HEAD)" \
  --quota --trigger suspend --schema-root "$HERA_SCHEMA" --server-cpus 1 --client-cpus 0 \
  --lanes 16 --rounds 8 --output "$RUN_DIR/candidate.json.gz"
```

Use --product main --trigger off and omit --quota in the clean main worktree. For the 9148aae run also export NF_HERA_JOIN_STATUS=1 and NF_HERA_PREPARED_HTTP_TASK=1 and use --rounds 6. Each arm creates and drops its own isolated database. Copy the supplied driver out of the checkout to protect import isolation. The experiment stage patch and reports do not alter production semantics on the HERA integration branch.
