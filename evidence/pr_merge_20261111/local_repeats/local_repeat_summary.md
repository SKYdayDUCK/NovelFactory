# 本地固定版本重复实验完整归纳

仅汇总已结束的 4 场 baseline 与 8 场 candidate1 原始 JSON。未重跑、未修改原始证据、未挑选最佳结果。逐场原始路径、SHA256、全部原报告指标、运行参数和重新核对结果在 `local_repeat_summary.json` 中保留。

## 结论

200 Agent 普通场景在候选版本两次均实现 21/21 新正式世界提交，首试失败为 0，全部响应 P95 为 206.723/200.839 ms；旧基线分别为 734.822/2143.582 ms、首试失败 0/23。普通场景改善一致，但本组使用不同冻结 harness 且宿主共享，不能把所有差值严格归因于单项代码变更。

200 Agent 热点场景两组都完成 21/21 正式世界提交，但最终仍有读请求失败：旧版 77/107，候选 63/82；首试失败旧版 589/608、候选 609/566。候选 call-start 43.326/48.433 RPS，header-start 28.743/30.020 RPS，与 1000 RPS 原目标差距明显。四场热点容量均为 `NOT_MET`，业务恢复 PASS 不改变这一点。

候选 50 Agent 普通场景两次 6/6 新提交、0 首试/终态失败；热点两次仍有 88/102 首试失败，但全部逻辑读请求在预算内恢复。热点 call-start 202.307/215.256 RPS，容量仍为 `NOT_MET`。这组数据没有旧版 50 Agent 对照。

## 固定版本、资源与参数

- baseline：`e0b0f2b080f3225d8be8e202e13258bc480f5b84`，harness SHA256 `c3533086aed8ef2ee309e44c16751d16804aa99ed78cc72cc4ed374cd5c6b940`。
- candidate1：`0a0af490c7768fe4b7d2da16dbaa74f0d9585f0d`，harness SHA256 `07c0644803a08f0f512dd14595975211d3dfb6483e07505133566fe5fee5a06f`。原始 JSON 均 `git_dirty=false`，各源码快照 hash 已复核。
- 共享 AMD EPYC 9V74，环境暴露 9 个逻辑 CPU。执行协调指定 CPU 0–3；候选 JSON 直接记录 `[0,1,2,3]`，旧版 JSON 没有 affinity/load-average 字段，不能把它写成独立观测。
- Python 3.12.14，SQLAlchemy 2.0.57、psycopg 3.3.8、Uvicorn 0.54.0、HTTPX 0.28.1。PostgreSQL 17.11，max_connections=60、shared_buffers=128MB、fsync/synchronous_commit=on。独立 API 进程 1、聚合数据库池 8、HTTPX 连接上限 256。
- seed=20261110；普通 hot_fraction=0.2、热点=0.8；通常到达率 10/s、写到达率 1/s；热点追加 agents×5 次请求、计划 1000/s；最多 5 次尝试、重试预算 30s、单次 HTTP 超时 30s、同载荷/同幂等键，无语义重建。HTTP admission、队列上限和所有原门槛见 JSON 原样保留。
- 两组详细 service diagnostics 关闭，候选 `service_diagnostics=[]`；增强的客户端/SQL 计时仍存在。基线与候选各用其冻结 harness，不能完全排除计时仪表差异及共享主机噪声。

## 全部运行：业务、逻辑请求与容量

“逻辑终态”是终止并被记录，包含失败；“终态失败”按每个 operation_id 的最高 attempt 重算，排除原本预期的 409 冲突。所有场次业务必需请求终态失败为 0；非零终态失败均是 overload 读请求，且均有 retry_exhausted 标记。

| 版本/运行 | 新提交/预期 | 计划/逻辑终态 | 尝试总数 | 首试失败 | 终态失败 | P95 ms | P99 ms | 负载窗口 s | 容量 |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---|
| baseline/200-mixed-1 | 21/21 | 1141/1141 | 1141 | 0 | 0 | 734.822 | 1,262.641 | 140.004 | PASS |
| baseline/200-overload-1 | 21/21 | 2141/2141 | 3245 | 589 | 77 | 14,143.938 | 22,767.689 | 184.875 | NOT_MET |
| baseline/200-mixed-2 | 21/21 | 1141/1141 | 1172 | 23 | 0 | 2,143.582 | 2,969.488 | 146.274 | NOT_MET |
| baseline/200-overload-2 | 21/21 | 2141/2141 | 3257 | 608 | 107 | 15,904.891 | 23,660.818 | 184.840 | NOT_MET |
| candidate1/50-mixed-1 | 6/6 | 286/286 | 286 | 0 | 0 | 106.749 | 164.881 | 33.951 | PASS |
| candidate1/50-overload-1 | 6/6 | 536/536 | 666 | 88 | 0 | 2,873.014 | 3,463.164 | 40.329 | NOT_MET |
| candidate1/50-mixed-2 | 6/6 | 286/286 | 286 | 0 | 0 | 98.967 | 168.022 | 33.997 | PASS |
| candidate1/50-overload-2 | 6/6 | 536/536 | 698 | 102 | 0 | 2,570.859 | 3,260.384 | 41.939 | NOT_MET |
| candidate1/200-mixed-1 | 21/21 | 1141/1141 | 1141 | 0 | 0 | 206.723 | 324.572 | 139.433 | PASS |
| candidate1/200-overload-1 | 21/21 | 2141/2141 | 3281 | 609 | 63 | 13,791.542 | 20,633.388 | 185.177 | NOT_MET |
| candidate1/200-mixed-2 | 21/21 | 1141/1141 | 1141 | 0 | 0 | 200.839 | 295.711 | 139.115 | PASS |
| candidate1/200-overload-2 | 21/21 | 2141/2141 | 3188 | 566 | 82 | 14,953.195 | 23,098.870 | 182.409 | NOT_MET |

## 到达率、客户端池与重试

call/header rate 均只用热点阶段首试。“未记录/不适用”不能解读为 0。pool P95 汇总所有尝试的 HTTPcore await wall time，含事件循环调度。

| 版本/运行 | call-start /s | header-start /s | pool P95 ms | 调度延迟 P95 ms | infrastructure retries | 终态 HTTP 200 | 预期冲突 |
|---|---:|---:|---:|---:|---:|---:|---:|
| baseline/200-mixed-1 | 未记录/不适用 | 未记录/不适用 | 未记录/不适用 | 2.290 | 0 | 1122 | 19 |
| baseline/200-overload-1 | 43.855 | 未记录/不适用 | 未记录/不适用 | 34,338.497 | 1104 | 2045 | 19 |
| baseline/200-mixed-2 | 未记录/不适用 | 未记录/不适用 | 未记录/不适用 | 3.978 | 31 | 1122 | 19 |
| baseline/200-overload-2 | 47.718 | 未记录/不适用 | 未记录/不适用 | 31,479.203 | 1116 | 2015 | 19 |
| candidate1/50-mixed-1 | 未记录/不适用 | 未记录/不适用 | 0.003 | 2.091 | 0 | 282 | 4 |
| candidate1/50-overload-1 | 202.307 | 102.243 | 0.026 | 4,336.549 | 130 | 532 | 4 |
| candidate1/50-mixed-2 | 未记录/不适用 | 未记录/不适用 | 0.002 | 1.921 | 0 | 282 | 4 |
| candidate1/50-overload-2 | 215.256 | 137.090 | 0.021 | 4,040.137 | 162 | 532 | 4 |
| candidate1/200-mixed-1 | 未记录/不适用 | 未记录/不适用 | 0.003 | 2.078 | 0 | 1122 | 19 |
| candidate1/200-overload-1 | 43.326 | 28.743 | 118.316 | 33,710.535 | 1140 | 2059 | 19 |
| candidate1/200-mixed-2 | 未记录/不适用 | 未记录/不适用 | 0.003 | 2.102 | 0 | 1122 | 19 |
| candidate1/200-overload-2 | 48.433 | 30.020 | 0.096 | 33,496.766 | 1047 | 2040 | 19 |

## CPU、SQL 与事务观测

父进程 CPU 是负载生成器与观测线程的进程 CPU；API 子进程 CPU 单列。SQL cursor 秒为多连接累计 wall time，排除了结果解码，不能用作 PostgreSQL CPU 或与单窗口直接相减。候选事务计时截至 commit/rollback 回调之前，排除最终服务器 commit 等待。

| 版本/运行 | 父进程 CPU s | API CPU s | SQL statements | SQL cursor s | transactions | DB pool peak | 事务累计 s |
|---|---:|---:|---:|---:|---:|---:|---:|
| baseline/200-mixed-1 | 6.845 | 77.932 | 33003 | 109.390 | 3682 | 8 | 未记录/不适用 |
| baseline/200-overload-1 | 43.048 | 118.844 | 52646 | 254.242 | 6711 | 8 | 未记录/不适用 |
| baseline/200-mixed-2 | 6.940 | 80.018 | 33034 | 142.273 | 3713 | 8 | 未记录/不适用 |
| baseline/200-overload-2 | 42.507 | 121.622 | 51967 | 311.527 | 6572 | 8 | 未记录/不适用 |
| candidate1/50-mixed-1 | 1.696 | 8.130 | 8096 | 2.565 | 922 | 2 | 6.908 |
| candidate1/50-overload-1 | 5.098 | 13.659 | 13399 | 27.209 | 1725 | 8 | 34.944 |
| candidate1/50-mixed-2 | 1.598 | 7.698 | 8096 | 2.279 | 922 | 2 | 6.417 |
| candidate1/50-overload-2 | 5.295 | 14.510 | 13390 | 27.080 | 1716 | 8 | 35.328 |
| candidate1/200-mixed-1 | 6.586 | 55.609 | 32276 | 20.045 | 3682 | 6 | 58.130 |
| candidate1/200-overload-1 | 43.034 | 97.675 | 52213 | 188.275 | 6753 | 8 | 268.593 |
| candidate1/200-mixed-2 | 6.270 | 54.599 | 32276 | 20.748 | 3682 | 6 | 58.394 |
| candidate1/200-overload-2 | 42.183 | 93.408 | 51844 | 180.863 | 6726 | 8 | 257.477 |

## 逐次数据库等待与负载观测

数据库观察器每 0.2 秒抽样。抽样锁等待为 0 不能证明不存在短锁等待；旧版没有 oldest transaction 字段。所有数据库等待、admission/鉴权/ingress 拒绝数和累计等待原值均保存在 JSON。

| 版本/运行 | 结果采集时 load avg (1/5/15m) | DB 样本数 | 抽样连接峰值 | 含 Lock 等待样本 | 最老事务 s |
|---|---|---:|---:|---:|---:|
| baseline/200-mixed-1 | 未记录 | 694 | 8 | 11 | 未记录/不适用 |
| baseline/200-overload-1 | 未记录 | 910 | 8 | 5 | 未记录/不适用 |
| baseline/200-mixed-2 | 未记录 | 725 | 8 | 15 | 未记录/不适用 |
| baseline/200-overload-2 | 未记录 | 910 | 8 | 18 | 未记录/不适用 |
| candidate1/50-mixed-1 | 0.332/1.036/0.879 | 169 | 3 | 0 | 0.036 |
| candidate1/50-overload-1 | 0.544/0.985/0.869 | 200 | 8 | 0 | 0.102 |
| candidate1/50-mixed-2 | 0.529/0.925/0.854 | 169 | 2 | 0 | 0.097 |
| candidate1/50-overload-2 | 1.469/1.123/0.926 | 208 | 8 | 0 | 0.123 |
| candidate1/200-mixed-1 | 1.612/1.355/1.046 | 691 | 7 | 0 | 0.472 |
| candidate1/200-overload-1 | 2.199/1.698/1.238 | 912 | 8 | 0 | 0.653 |
| candidate1/200-mixed-2 | 1.620/1.693/1.310 | 690 | 7 | 0 | 0.195 |
| candidate1/200-overload-2 | 1.451/1.458/1.284 | 898 | 8 | 0 | 0.285 |

## 复核与解释限制

全部 12 场均通过仅对证据的核对：原始行数等于 reported emitted/completed；首试及终态 operation_id 数等于 planned；首试失败/status counts 可重新得到；唯一 revision 回执数等于新世界提交；所有终态被 HTTP 200、预期冲突或失败完全覆盖；源码快照 hash 与各次元数据一致。这些核对不是新增容量通过门槛。

- Both groups used their own frozen harness: e0b0f2b/c3533086 for baseline and 0a0af49/07c06448 for candidate1. They are not byte-identical measurement code.
- Detailed service diagnostics were off for the comparison. The candidate still includes always-on HTTPcore header/pool timing, response-validation CPU timing and enhanced SQL/transaction meters. Measurement differences cannot be completely excluded.
- The execution resource assignment reported by the coordinating session was CPUs 0-3 for the independent client/API/PostgreSQL experiment. Candidate JSON directly records affinity [0,1,2,3]. Baseline JSON did not record affinity or load average, so those baseline values remain unobserved in this JSON-only summary.
- Host is shared with other work. Fixed affinity does not ensure exclusive CPU/cache/I/O capacity. These sequential repeated observations are not a controlled crossover causal estimate or a sustained production SLO.
- Completed logical operations in the original harness means a terminal record, not necessarily a successful response. Final overload failures are explicitly recomputed and retained here; the business recovery gate excludes overload-only final rejections.
- Expected stale shared-command 409 conflicts are retained but excluded from first/final operational failure counts using the original harness rule. Successful duplicate receipts never increase unique world commits.
- SQL cursor seconds are summed across potentially overlapping connections, exclude result decoding, and are not PostgreSQL CPU. Candidate SQL meter also records failures/transaction boundaries absent in baseline.
- Header start is HTTPcore request-header-send-start, not packet capture, body sent, server accepted, or business success. Baseline did not record it. Client pool await time includes event-loop scheduling.
- Generator/observer process CPU excludes API children and PostgreSQL. API CPU is separate. PostgreSQL process CPU, run queue, stage CPU and JSONB decode detail were not captured with diagnostics off. Peak RSS is process-lifetime high-water.
- Mixed rows have no open-loop phase; their capacity PASS cannot establish the original 1000 RPS objective. All overload capacity gates remain NOT_MET.
- No baseline 50 Agent runs exist in this selected repeat set. No 500/1000 Agent result or second canonical optimization result is inferred from these 12 runs.

## 原始证据索引

| 运行 | 原始 JSON SHA256 |
|---|---|
| baseline/200-mixed-1 | `ece17784a318cf9ccaa8b969c702c05f229fe16f71c15ff22bcfb8eba323e9ef` |
| baseline/200-overload-1 | `adf88a9b6edb333c1af88c34e2a9a2e5fa4fca8ee6a3ccf6309a348bb806bc96` |
| baseline/200-mixed-2 | `3f7119a8fc21e35e3fea2d6f19abc6a1f464bceaca4cfc393784f95201764e53` |
| baseline/200-overload-2 | `abed8472e7642f72339e809ab87dd5f1c6138b6cb622383da11ee5674f4fa1ae` |
| candidate1/50-mixed-1 | `5d4c96a78cbda473ccace9cdd78d0e751c30f591431286f707bf23fd238ea6af` |
| candidate1/50-overload-1 | `8f28dee942063c60675d117428a0003a6ed4f087947453358d80b7469bf27f23` |
| candidate1/50-mixed-2 | `e6c8f2cda82539d8aa274aabfa481158c494d99f1b68a044482894636d9db6a7` |
| candidate1/50-overload-2 | `9777429ca1de029710a2c9f62f7d68b03dfe227ce29db773baffda05f5505a4b` |
| candidate1/200-mixed-1 | `b4481566f0af9f704525b934fd6fe1c4b78f64295f17070d1f2fcc95a0501c17` |
| candidate1/200-overload-1 | `ad63a5de6dc5db60a259a7fdb0578732b8b60553ffa7957dee964442f24b7cb6` |
| candidate1/200-mixed-2 | `94246a04f3d9d9c1f4f7fc554b0f8ccb94479c8b2c258d756d0a23350810a085` |
| candidate1/200-overload-2 | `ae09a5b6bf70f130482608d80089e0c119745bc79e574c158d0840a5275d5f87` |
