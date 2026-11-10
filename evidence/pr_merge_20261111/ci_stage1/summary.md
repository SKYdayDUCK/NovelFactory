# 第一远程检查点实际 CI 证据索引

提交：`0a0af490c7768fe4b7d2da16dbaa74f0d9585f0d`。ZIP digest 逐项核对；原始 ZIP、解包文件及 job logs 均保留。

## 实际负载（包括所有已下载重复）

| Run / repeat | Agent / profile | 正式提交 | 首试失败 | 容量 | 业务恢复 | 有效调用/s（扣重复） | 首试 P95 / P99 ms | 实际 call/header start/s |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| 42855273814 / run-1 | 50 / mixed | 6/6 | 0 | PASS | PASS | 8.1564 | 96.93/131.76 | N/A |
| 42855273814 / run-2 | 50 / mixed | 6/6 | 0 | PASS | PASS | 8.1381 | 94.96/333.21 | N/A |
| 42855273814 / run-1 | 50 / overload | 6/6 | 128 | NOT_MET | PASS | 11.8559 | 3806.86/5921.71 | 84.22/57.23 |
| 42855273814 / run-2 | 50 / overload | 6/6 | 136 | NOT_MET | PASS | 11.1277 | 4089.75/4728.78 | 115.46/83.63 |
| 42855273814 / run-1 | 200 / mixed | 21/21 | 0 | PASS | PASS | 7.9076 | 288.26/577.10 | N/A |
| 42855273814 / run-2 | 200 / mixed | 21/21 | 0 | PASS | PASS | 7.9036 | 280.01/608.47 | N/A |
| 42855273814 / run-1 | 200 / overload | 21/21 | 620 | NOT_MET | PASS | 10.9469 | 10668.26/17238.34 | 39.81/30.22 |
| 42855273814 / run-2 | 200 / overload | 21/21 | 580 | NOT_MET | PASS | 10.9687 | 12122.52/19772.16 | 40.98/30.01 |
| 42855273912 / single | 200 / mixed | 21/21 | 0 | PASS | PASS | 7.9113 | 199.00/291.69 | N/A |
| 42855273912 / single | 200 / overload | 21/21 | 579 | NOT_MET | PASS | 11.4064 | 12991.21/20070.40 | 52.55/31.66 |
| 42855526861 / run-1 | 50 / overload | 6/6 | 127 | NOT_MET | PASS | 11.5077 | 3732.27/4798.30 | 142.94/88.47 |
| 42855526861 / run-2 | 50 / overload | 6/6 | 123 | NOT_MET | PASS | 11.4837 | 3725.26/4304.25 | 129.38/89.64 |
| 42855526861 / run-1 | 200 / mixed | 21/21 | 0 | PASS | PASS | 7.9141 | 204.94/396.44 | N/A |
| 42855526861 / run-2 | 200 / mixed | 21/21 | 0 | PASS | PASS | 7.9133 | 202.47/356.34 | N/A |
| 42855526861 / run-1 | 50 / mixed | 6/6 | 0 | PASS | PASS | 8.1487 | 95.99/123.29 | N/A |
| 42855526861 / run-2 | 50 / mixed | 6/6 | 0 | PASS | PASS | 8.1551 | 108.59/202.43 | N/A |
| 42855526861 / run-1 | 200 / overload | 21/21 | 661 | NOT_MET | PASS | 10.1715 | 12683.90/22130.91 | 32.18/22.74 |
| 42855526861 / run-2 | 200 / overload | 21/21 | 627 | NOT_MET | PASS | 10.1995 | 14291.10/23575.93 | 32.73/22.51 |
| 42855527063 / single | 200 / overload | 21/21 | 617 | NOT_MET | PASS | 10.1516 | 14144.82/23307.20 | 29.87/22.85 |
| 42855527063 / single | 200 / mixed | 21/21 | 0 | PASS | PASS | 7.9117 | 310.76/661.84 | N/A |

## JUnit 实际计数

- `42855274085/13781814428/extracted/postgres-all.xml`: tests=436, failures=0, errors=0, skipped=0, time=342.377s
- `42855274085/13782636646/extracted/unit.xml`: tests=240, failures=0, errors=0, skipped=0, time=4.430s
- `42855526952/13783415135/extracted/unit.xml`: tests=240, failures=0, errors=0, skipped=0, time=5.265s
- `42855526952/13783446363/extracted/postgres-all.xml`: tests=436, failures=0, errors=0, skipped=0, time=354.572s

## 证据边界

- All listed load repetitions are included; no best-run selection.
- This index describes the first frozen checkpoint only. Current local source may have moved.
- Service diagnostic timing is inclusive and overlapping, and instrumentation may affect latency/throughput.
- Local no-NF_BENCH_DIAGNOSTICS experiments are separate and are not synthesized into GitHub results.
- Connection-sample wait totals are sample-weighted observations, not elapsed time.
- Declared 1000 RPS remains NOT_MET whenever actual call starts or first attempts fail the frozen gate.
