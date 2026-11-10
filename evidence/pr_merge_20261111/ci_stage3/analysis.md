# 第三检查点 CI 只读独立核验

## 身份与完整性

远程 PR2 HEAD `971fbef29eceb422d77271d11841bae1abdfd73f`；PR checkout `4eec0c5d8fa5a48fadde8fface13e3fb3dacf749`。两者整树均为 `e87b88d343e47361d16161f2721a596083a71085`，已通过 GitHub connector 读取 Git commit 证明；此结论只绑定该冻结源码。共 6 个 run、18 个 job、18 个 artifact ZIP，全部下载且 GitHub SHA256 digest 相等，18 份 job 日志保留。20 份完整原始负载 JSON 与 4 份 JUnit 均未删记录。

PR 工程 run [42857853036](https://github.com/SKYdayDUCK/NovelFactory/actions/runs/42857853036) 与 push [42857838549](https://github.com/SKYdayDUCK/NovelFactory/actions/runs/42857838549) 成功；实际各 PostgreSQL 全套 472 项、unit 子集 276 项，均 0 failure / 0 error / 0 skip。276 是全套中的子集，不能相加成 748 项独立测试。

PR/PUSH 容量矩阵与独立 200 run 均 failure。所有失败 job 都是 overload 的原有强制容量判定未通过；不是工程测试失败。具体完整逐次表见 summary.md。

## 业务恢复与容量分开判定

50 正常 4 次均 6/6 正式新版本、首试失败 0；200 正常 6 次均 21/21、首试失败 0。50 热点 4 次亦 6/6，200 热点 6 次均 21/21，业务恢复与工程正确性全部 PASS；但所有热点首试容量与预设 1000 RPS 均 NOT_MET。200 热点实际 call-start 30.33–52.39/s、HTTP header-start 22.56–33.20/s，不是达到 1000 RPS。

| 200热点 run/repeat | 首试失败 | 最终预算耗尽读任务 | 少于5次/达到5次 | 客户端CPU/验证CPU/服务CPU秒 | pool wait P95 ms | header/call比率 |
|---|---:|---:|---:|---:|---:|---:|
| 42857853046/run-1 | 643 | 122 | 107/15 | 55.03/9.08/118.59 | 72.994 | 0.7030 |
| 42857853046/run-2 | 649 | 142 | 123/19 | 55.75/9.15/119.11 | 164.095 | 0.7548 |
| 42857853112/single | 568 | 35 | 23/12 | 36.44/4.97/77.20 | 0.108 | 0.6336 |
| 42857838475/single | 639 | 116 | 98/18 | 47.41/7.05/103.50 | 189.480 | 0.6568 |
| 42857838507/run-1 | 663 | 108 | 96/12 | 53.52/8.98/113.44 | 60.099 | 0.7135 |
| 42857838507/run-2 | 613 | 119 | 97/22 | 53.51/8.84/112.22 | 226.858 | 0.6910 |

这些耗尽项全部为 overload 查询、最终码 BACKPRESSURE，不能把 completed_logical_operations 当成每条查询均成功。重试政策仍为最多 5 次/30 秒预算；少于 5 次表示时间预算或下一次退避无法容纳，达到 5 次可能同时遇到时间限制。所有原始失败行均保留。

## 生成器、服务器与 PostgreSQL

全部 CI 负载记录包含诊断成本；不可与本地无 NF_BENCH_DIAGNOSTICS 的实验直接混为同条件。GitHub runner 均分配 4 CPU，但本阶段实际包含 EPYC 7763/9V45/9V74、Xeon 6973P-C 与 8573C；跨 runner 的波动不能单独归因于补丁。客户端 CPU 中响应验证占约 13.6%–16.8%。200 热点成功 overload 响应 median=max=2104 bytes，429 为 median 152/max 154 bytes；pool wait 与调度延迟均有实际观测，wire/header 速率低于逻辑调用开始速率，客户端调度也限制了 offered-rate。因此不能以单一服务 CPU 总量解释全部吞吐。

同时，服务端真正返回了 429，不能全归因于生成器：PR 独立 200 热点共 1051 条 429，其中 ingress 拒绝28，鉴权 admission 拒绝858+超时1，query admission 拒绝82+超时82，逐项合计1051。该 run 客户端36.44 CPU秒、响应验证4.97秒、API77.20秒。服务端 inclusive CPU：query51.18、sample39.64、Kernel.revision29.89、contract.validation13.03、JSON decode10.87、Context Compiler7.53秒；它们嵌套重叠，不能累加。

同一 PR 独立 run PostgreSQL pool=8、peak8、结束0，SQL错误0；最大应用事务0.220秒，cursor0.138秒，采样最大事务0.204秒。其他矩阵重复保留了少量真实 WAL 等待与1次 transactionid Lock 采样，不能声明从未锁等待。无证据表明数据库长时间锁阻塞是唯一瓶颈。每份完整 diagnostics 及采样在 index.json 和原始 JSON。

## 配对实验与不可扩大解释的边界

| 实验 | PR结果 | push结果 |
|---|---|---|
| genesis 5对 warm replay 中位墙钟 | 340.83 → 32.65 ms | 338.19 → 32.29 ms |
| genesis cold 单次（单列） | 347.13 ms | 344.27 ms |
| canonical 8对顺序query 中位墙钟 | 36.916 → 32.027 ms | 36.062 → 37.785 ms |
| canonical中位CPU/query | 27.708 → 26.550 ms | 30.079 → 28.176 ms |

两份 canonical 实验 status 都仅 SEMANTICS_AND_EVIDENCE_PASS：8 对 × 2 arms 的完整 payload/evidence/cut 与已存 reference hash 逐项核对，world before/after 相同；push 墙钟反而退步约4.78%，原结果完整保留。可确认语义与 CPU 小幅改善的测量，不能宣布 HTTP 容量达标。genesis 所有5对完整 snapshot 及 reference hash 相等，warm收益与cold成本分列。旧历史 Context 实验仍在固定 e0b worktree 执行，不能充当新 HEAD 测试；独立 context-p3 当前核心配对结果另存。

实际工程 job 没有执行 Ruff 命令（日志只显示安装 ruff）；冻结 canonical fixture 原样快照的 I001 因而不是本阶段 CI 的失败原因。不得为修复格式改写已绑定 SHA 的冻结参考源码。

r1-acceptance 收集器记录34 PASS与3项有边界状态；load_gate/load_coverage_gate=NOT_EXECUTED、overall_engineering_gate=NOT_MET，仅对该收集器自身作用域成立。外部真实模型 BLOCKED_ENVIRONMENT、人工全文阅读 PENDING_AUTHOR、整体 P2 NOT_FULLY_ACCEPTED，不能用工程 CI 绿灯覆盖这些未验收目标。

## 结论

当前源码工程回归及约定50/200业务恢复已实测通过。热点首试与1000 RPS容量仍失败，未满足用户规定合并门槛，不能直接合并。保留全部失败证据并继续将性能容量标记 NOT_MET。最新 PR3 的 stage4 结果应另行核验，不以本阶段较好结果替代。
