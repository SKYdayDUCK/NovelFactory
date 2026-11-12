# Frozen baseline

All P5.9 BASELINE runs in this directory use main `5e322e8af8ddb4465f68f8cab84c9685832461a9`. The host runs Linux, Python 3.12 and PostgreSQL 17.11 with fsync, synchronous commit, data checksums and loopback TCP. The developer host is shared. AT-30 uses two visible CPUs (7,8), one load scenario at a time. The remaining workloads run on the shared host's normal CPU set. The isolated database wrapper writes actual versions, pool settings, elapsed time and exit statuses under raw/. Real provider inference calls are zero. The endpoint tests use loopback HTTP.

| PG17 operation | Conditions | Result |
| --- | --- | --- |
| Context compile | 250 items, owner, four revisions, 5 measured per size, separately profiled calls | 1, 20, 100 subjects took median 47.34, 60.99, 131.92 ms. Each compile executed 48 SQL statements including one batched dependency insert. |
| Accepted relation update | 100 items, 7 successive ordinary proposals and guarded accepts; replay each receipt | 60–61 SQL calls. Four scope keys accounted for 5 calls per write and a median 4.45 ms inside acceptance. Median entire acceptance was 73.73 ms. |
| Historical AT-30 | Different commits and hosted runners | Issue #13 records 31.52–34.47 call starts/s on its four-round 200-agent ABBA versus 950/s required. Some 500 sustained-read runs passed; others returned 429s. Current reruns are organized by scenario under raw/at30. |

Original observations, full SQL counts, CPU profiles, per-attempt call logs, original arguments and failed infrastructure attempts are preserved. The context benchmark excludes fixture construction. Its first application read has warm OS and PG buffers. No cold-platter latency was measured.

Sources: `raw/context-baseline.json`, `raw/write-baseline.json`, and the [historical capacity issue](https://github.com/SKYdayDUCK/NovelFactory/issues/13).
