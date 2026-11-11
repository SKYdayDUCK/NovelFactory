# 第四远程检查点实际 CI 证据索引

提交：`e3c7b79b0058656f11257ea55edc75371382b73e`。ZIP digest 逐项核对；原始 ZIP、解包文件及 job logs 均保留。

## 实际负载（包括所有已下载重复）

| Run / repeat | Agent / profile | 正式提交 | 首试失败 | 容量 | 业务恢复 | 有效调用/s（扣重复） | 首试 P95 / P99 ms | 实际 call/header start/s |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| 42859219256 / single | 200 / mixed | 21/21 | 0 | PASS | PASS | 7.9018 | 334.52/586.64 | N/A |
| 42859219256 / single | 200 / overload | 21/21 | 551 | NOT_MET | PASS | 11.8190 | 11183.76/17388.05 | 55.40/35.16 |
| 42859219454 / run-1 | 50 / mixed | 6/6 | 0 | PASS | PASS | 8.1636 | 94.87/113.36 | N/A |
| 42859219454 / run-2 | 50 / mixed | 6/6 | 0 | PASS | PASS | 8.1402 | 94.10/213.04 | N/A |
| 42859219454 / run-1 | 50 / overload | 6/6 | 125 | NOT_MET | PASS | 11.6056 | 3614.93/4456.58 | 171.38/90.64 |
| 42859219454 / run-2 | 50 / overload | 6/6 | 123 | NOT_MET | PASS | 12.0922 | 3905.95/5984.35 | 91.25/58.39 |
| 42859219454 / run-1 | 200 / mixed | 21/21 | 0 | PASS | PASS | 7.9099 | 194.11/282.55 | N/A |
| 42859219454 / run-2 | 200 / mixed | 21/21 | 0 | PASS | PASS | 7.8948 | 244.19/367.10 | N/A |
| 42859219454 / run-1 | 200 / overload | 21/21 | 606 | NOT_MET | PASS | 11.1494 | 12212.67/18607.53 | 43.41/29.78 |
| 42859219454 / run-2 | 200 / overload | 21/21 | 589 | NOT_MET | PASS | 11.1933 | 11222.71/18002.40 | 41.64/31.18 |
| 42859202233 / run-1 | 50 / mixed | 6/6 | 0 | PASS | PASS | 8.1063 | 137.48/198.45 | N/A |
| 42859202233 / run-2 | 50 / mixed | 6/6 | 0 | PASS | PASS | 8.1151 | 129.66/175.21 | N/A |
| 42859202233 / run-1 | 50 / overload | 6/6 | 112 | NOT_MET | PASS | 12.3709 | 2955.40/3780.81 | 156.49/93.67 |
| 42859202233 / run-2 | 50 / overload | 6/6 | 104 | NOT_MET | PASS | 12.7315 | 2946.15/4038.59 | 185.76/101.67 |
| 42859202233 / run-1 | 200 / mixed | 21/21 | 0 | PASS | PASS | 7.9172 | 170.80/277.02 | N/A |
| 42859202233 / run-2 | 200 / mixed | 21/21 | 0 | PASS | PASS | 7.9169 | 178.12/340.56 | N/A |
| 42859202233 / run-1 | 200 / overload | 21/21 | 616 | NOT_MET | PASS | 11.1129 | 10319.49/16995.61 | 40.32/30.51 |
| 42859202233 / run-2 | 200 / overload | 21/21 | 647 | NOT_MET | PASS | 10.9794 | 12496.16/20434.46 | 39.66/26.66 |
| 42859202425 / single | 200 / mixed | 21/21 | 0 | PASS | PASS | 7.9228 | 113.87/196.85 | N/A |
| 42859202425 / single | 200 / overload | 21/21 | 601 | NOT_MET | PASS | 11.0817 | 11381.25/18694.76 | 40.13/30.64 |

## JUnit 实际计数

- `42859202765/13783448378/extracted/unit.xml`: tests=380, failures=0, errors=0, skipped=0, time=9.986s
- `42859202765/13784987189/extracted/postgres-all.xml`: tests=631, failures=0, errors=0, skipped=0, time=400.950s
- `42859219253/13784316828/extracted/unit.xml`: tests=380, failures=0, errors=0, skipped=0, time=9.982s
- `42859219253/13785390382/extracted/postgres-all.xml`: tests=631, failures=0, errors=0, skipped=0, time=228.088s

## 证据边界

- All listed load repetitions are included; no best-run selection.
- This index describes the fourth frozen checkpoint only. Current local source may have moved.
- Service diagnostic timing is inclusive and overlapping, and instrumentation may affect latency/throughput.
- Local no-NF_BENCH_DIAGNOSTICS experiments are separate and are not synthesized into GitHub results.
- Connection-sample wait totals are sample-weighted observations, not elapsed time.
- Declared 1000 RPS remains NOT_MET whenever actual call starts or first attempts fail the frozen gate.
- Business recovery PASS and completed_logical_operations do not mean every overload read succeeded: retain retry_exhaustion counts.
- GitHub runners expose 4 CPUs; the actual model is recorded for every result and varies between jobs. Only same-job repetitions share the same runner allocation.
