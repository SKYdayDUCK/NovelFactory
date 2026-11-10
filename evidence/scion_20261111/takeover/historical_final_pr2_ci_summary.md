# PR2 最终资料提交实际 CI 证据索引

提交：`a9547bd651e177d48c9d0e810362e411f510d62d`。ZIP digest 逐项核对；原始 ZIP、解包文件及 job logs 均保留。

## 实际负载（包括所有已下载重复）

| Run / repeat | Agent / profile | 正式提交 | 首试失败 | 容量 | 业务恢复 | 有效调用/s（扣重复） | 首试 P95 / P99 ms | 实际 call/header start/s |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| 42863611810 / single | 200 / mixed | 21/21 | 0 | PASS | PASS | 7.8996 | 290.37/429.95 | N/A |
| 42863611810 / single | 200 / overload | 21/21 | 636 | NOT_MET | PASS | 10.2284 | 13568.72/20481.91 | 31.90/22.52 |
| 42863611898 / run-1 | 50 / mixed | 6/6 | 0 | PASS | PASS | 8.1164 | 131.94/212.56 | N/A |
| 42863611898 / run-2 | 50 / mixed | 6/6 | 0 | PASS | PASS | 8.1156 | 133.63/171.85 | N/A |
| 42863611898 / run-1 | 50 / overload | 6/6 | 117 | NOT_MET | PASS | 13.3423 | 2450.60/2975.36 | 294.50/228.20 |
| 42863611898 / run-2 | 50 / overload | 6/6 | 110 | NOT_MET | PASS | 13.0604 | 2371.07/2974.38 | 267.35/164.16 |
| 42863611898 / run-1 | 200 / mixed | 21/21 | 0 | PASS | PASS | 7.9149 | 176.41/242.11 | N/A |
| 42863611898 / run-2 | 200 / mixed | 21/21 | 0 | PASS | PASS | 7.9141 | 158.83/198.00 | N/A |
| 42863611898 / run-1 | 200 / overload | 21/21 | 654 | NOT_MET | PASS | 10.0713 | 12668.18/22669.66 | 31.66/22.89 |
| 42863611898 / run-2 | 200 / overload | 21/21 | 638 | NOT_MET | PASS | 10.0842 | 12877.00/22925.68 | 32.21/22.48 |

## JUnit 实际计数

- `42863601042/13785488582/extracted/unit.xml`: tests=276, failures=0, errors=0, skipped=0, time=6.008s
- `42863601042/13786452601/extracted/postgres-all.xml`: tests=472, failures=0, errors=0, skipped=0, time=342.105s
- `42863611743/13785514592/extracted/postgres-all.xml`: tests=472, failures=0, errors=0, skipped=0, time=261.257s
- `42863611743/13785832438/extracted/unit.xml`: tests=276, failures=0, errors=0, skipped=0, time=5.002s

## 证据边界

- This verifies the actual final PR2 documentation/evidence HEAD; protected code, tests, workflows, benchmarks and dependencies are identical to 971fbef.
- All 10 current-head load observations and four JUnit files are included; no best-run selection, no workflow rerun, no threshold change.
- Four workflows are expected: three pull_request runs plus one push engineering run. Existing push path filters do not trigger load workflows for documentation/evidence-only changes.
- Full PostgreSQL and unit JUnit counts overlap. Do not add them as distinct tests.
- Service diagnostic timing is inclusive and overlapping; instrumentation and shared CI hardware affect latency and throughput.
- GitHub runner CPU models and contention differ across jobs; only paired arms and repeated observations within one job use the same runner allocation.
- Business recovery PASS is distinct from capacity PASS and from success of every overload read. Last retryable/exhausted logical operations remain failures.
- The requested 1000 RPS target is unchanged. Call starts, HTTP header starts, valid responses and unique world commits have separate measurement boundaries.
- This is read-only evidence collection for an unmerged PR; a synthetic pull_request merge checkout is not a formal GitHub merge.
