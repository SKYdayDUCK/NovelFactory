# PR3 008fccf 实际 CI 摘要

原始 ZIP、日志、result.json 均保留；不以 job 绿色替代实际计数。

## JUnit

- `42856309898/13782393842/extracted/unit.xml`: tests=344, failures=0, errors=0, skipped=0
- `42856309898/13784410260/extracted/postgres-all.xml`: tests=595, failures=0, errors=0, skipped=0
- `42856323577/13782883364/extracted/postgres-all.xml`: tests=595, failures=0, errors=0, skipped=0
- `42856323577/13783171528/extracted/unit.xml`: tests=344, failures=0, errors=0, skipped=0

## 业务负载

| Run | Agent/profile | 正式提交 | 首试失败 | 容量 | 业务恢复 | 有效调用/s | 首试P95/P99 ms |
| --- | --- | --- | --- | --- | --- | --- | --- |
| 42856309340 | 200/overload | 21/21 | 647 | NOT_MET | PASS | 10.1778 | 16550.43/25748.81 |
| 42856309340 | 200/mixed | 21/21 | 0 | PASS | PASS | 7.9090 | 263.08/439.97 |
| 42856309344 | 500/mixed | 51/51 | 0 | PASS | PASS | 7.8513 | 713.30/876.05 |
| 42856323239 | 200/overload | 21/21 | 585 | NOT_MET | PASS | 11.0196 | 12088.71/19256.72 |
| 42856323239 | 200/mixed | 21/21 | 0 | PASS | PASS | 7.9066 | 293.75/533.21 |
| 42856323313 | 500/mixed | 51/51 | 0 | PASS | PASS | 7.8743 | 247.10/448.32 |

## 1000 Agent / 5000 次只读尝试

| Run | 有效成功/计划 | wire start/s | 429 | transport | P95/P99 ms | 容量 |
| --- | --- | --- | --- | --- | --- | --- |
| 42856309344 | 1129/5000 | 37.86 | 3793 | 78 | 8954.13/11461.65 | NOT_MET |
| 42856323313 | 1339/5000 | 43.45 | 3572 | 89 | 8514.99/10911.05 | NOT_MET |

## 600秒探针

- 42856309344: 11942/12000有效；20.0005 call-start/s；status={'200': 11942, '429': 58}；probe=NOT_MET；long_duration=NOT_EXECUTED
- 42856323313: 11156/12000有效；20.0006 call-start/s；status={'200': 11156, '429': 844}；probe=NOT_MET；long_duration=NOT_EXECUTED

## PITR

- 42856309403: PASS；target commits lost=0；old_worker_rejected=True；artifact保留8/11个manifest文件、已保留项hash=True；RTO=0.486s（小型隔离fixture）
- 42856323203: PASS；target commits lost=0；old_worker_rejected=True；artifact保留8/11个manifest文件、已保留项hash=True；RTO=2.813s（小型隔离fixture）

## 范围

- Only frozen checkpoint 008fccf is represented; no later HEAD is inferred from these measurements.
- 500-mixed and scale read probes run two API processes with four DB pool slots each (aggregate 8), on shared 4-CPU GitHub runners.
- Scale workflows do not enable NF_BENCH_DIAGNOSTICS; independent 200 workflows do. Do not combine them as identical instrumentation conditions.
- 1000-open validates responses after network measurement; every success still requires full schema and exact state validation.
- completed_requests counts finished attempts, not valid successes. Raw ReadError and 429 rows remain present.
- 600-second probe is not a multi-hour production experiment; long_duration_gate remains NOT_EXECUTED even if probe passes.
- Derived worker recovery is covered by ten executed PostgreSQL testcases per engineering run, not by the scale load.
- The scale harness does not start a DerivedWorldWorker: shared API-plus-derived-worker throughput/resource budgets remain unmeasured.
- Standalone derived worker uses Database default pool_size=5, sequential messages (default max_messages=100, CLI maximum 10000), default lease_seconds=30; operators must include its separate pool in total deployment budget.
- PITR SHA256 manifest lists eleven files but upload glob retains eight: archive_segment.py, restore.conf and source.conf are absent. Retained file hashes and ZIP digest match; do not claim the full manifest is restorable.
