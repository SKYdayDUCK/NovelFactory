# 第四检查点工程与50/200 Agent CI独立核验

## 被测提交与证据完整性

PR3 远程 HEAD `e3c7b79b0058656f11257ea55edc75371382b73e`，PR checkout `9fd27f6dae5a157e29d92b3797764d0d74683a5c`；通过 GitHub connector 读取两份 Git commit，完整树同为 `72328df65eafff795a5df1a10117c68c2d899bb6`。PR checkout 父提交是 PR2 `971fbef...` 与 PR3 `e3c7b79...`，PR3 分支父是 `008fccf...` 与 `971fbef...`；没有用前阶段源码替代当前源。

本索引仅负责 6 个 run、18 个 job、18 个 artifact，全部终态。每份 ZIP SHA256 与 GitHub公布digest匹配；解包文件和全部18份实际 job 日志已保存。所有20次负载重复与4份JUnit都纳入 summary/index，不挑最佳。另4个 scale/PITR run 由 ci-stage4-scale 单独核验，不能从本索引替代或推断它们通过。

## 工程回归与容量结论

| 执行 | 工程run | PostgreSQL全套 | unit子集 |
|---|---|---|---|
| PR | [42859219253](https://github.com/SKYdayDUCK/NovelFactory/actions/runs/42859219253) | 631项，0fail/0error/0skip，228.088秒 | 380项，0fail/0error/0skip |
| push | [42859202765](https://github.com/SKYdayDUCK/NovelFactory/actions/runs/42859202765) | 631项，0fail/0error/0skip，400.950秒 | 380项，0fail/0error/0skip |

380为631的子集，不能相加为1011项独立测试。工程run成功，原有事务、权限、幂等、历史、迁移、并发、恢复和P3/P4测试仍实际执行。

正常50 Agent四次均6/6新正式版本、正常200 Agent六次均21/21，全部首试失败0。热点50四次亦6/6、200六次亦21/21；所有记录的业务恢复和工程正确性PASS。全部热点原有容量/首试/预设1000 RPS门槛仍NOT_MET，因此P2矩阵和独立200 run都failure。失败步骤是固定数量实际请求测量的原有capacity gate，没有禁用或下调。

| 200热点 run/repeat | 首试失败 | 最终耗尽overload读 | 少于5次/达到5次 | call/header start每秒 | P95/P99毫秒 |
|---|---:|---:|---:|---:|---:|
| 42859219256/single | 551 | 30 | 14/16 | 55.40/35.16 | 11183.76/17388.05 |
| 42859219454/run-1 | 606 | 52 | 44/8 | 43.41/29.78 | 12212.67/18607.53 |
| 42859219454/run-2 | 589 | 56 | 42/14 | 41.64/31.18 | 11222.71/18002.40 |
| 42859202233/run-1 | 616 | 75 | 47/28 | 40.32/30.51 | 10319.49/16995.61 |
| 42859202233/run-2 | 647 | 91 | 74/17 | 39.66/26.66 | 12496.16/20434.46 |
| 42859202425/single | 601 | 72 | 54/18 | 40.13/30.64 | 11381.25/18694.76 |

所有最终耗尽项仍在原始JSON：它们是BACKPRESSURE查询，不能把21/21世界提交或completed_logical_operations解释为每一查询都成功。原重试仍最多5次、预算30秒；少于5次表示时间预算或下一退避不能容纳，达到5次可能同时受时间限制。50热点无最终耗尽查询，但首试仍失败，容量判定保持NOT_MET。

## 真实瓶颈与测量边界

每份负载保留客户端、独立API进程、PostgreSQL与分阶段诊断；这些计数来自实际执行，未用模拟推算替代。CI分配4 CPU，机型包括EPYC7763/9V45/9V74与Xeon8370C；同一job的重复才共享runner资源。所有本索引负载均含NF_BENCH_DIAGNOSTICS成本，本地关闭诊断的结果另列，不能混成同条件。

| 200热点 run/repeat | 客户端CPU秒 | 响应验证CPU秒 | API CPU秒 | pool wait P95/P99 ms | header/call比率 |
|---|---:|---:|---:|---:|---:|
| 42859219256/single | 34.74 | 4.55 | 72.51 | 33.791/148.597 | 0.6347 |
| 42859219454/run-1 | 41.44 | 5.70 | 86.79 | 0.048/110.490 | 0.6859 |
| 42859219454/run-2 | 40.46 | 5.55 | 86.23 | 74.165/294.192 | 0.7488 |
| 42859202233/run-1 | 42.70 | 5.72 | 90.37 | 52.407/539.810 | 0.7567 |
| 42859202233/run-2 | 44.56 | 5.78 | 88.87 | 239.451/567.181 | 0.6722 |
| 42859202425/single | 41.54 | 5.68 | 87.82 | 53.686/376.196 | 0.7635 |

客户端响应验证占自身CPU约12.97%–13.75%；成功overload响应median=max=2104 bytes，429 median152/max154 bytes。HTTPX连接池等待和调度延迟真实存在，header-start仅是发送头部事件计数，不等同服务成功吞吐。200热点实发header约26.66–35.16/s，未达到1000 RPS，必须区分生成器能力、服务容量和原SLO。

服务端也确实背压。PR独立200热点1036条429可完整归因：ingress拒绝37 + 鉴权拒绝820/超时1 + query拒绝104/超时74 = 1036。push独立热点1224条429为鉴权拒绝923 + query拒绝129/超时172，ingress0。响应不是全部客户端本地超时。

PR独立热点服务inclusive CPU：query48.61、sample37.23、revision28.46、contract验证11.93、JSON解码12.33、Context Compiler6.47秒；这些层级嵌套不可相加。该run PostgreSQL pool8、峰值8、结束0、SQL错误0；应用事务最大0.213秒、cursor最大0.117秒、采样最大事务0.179秒。其他重复观察到WALWrite/WalSync等真实等待，push200热点run2采样最大事务0.896秒；没有把SQL或锁等待隐藏在总体CPU中。全部细项保留于index和raw JSON。

## 当前源码配对实验

| 实验 | PR | push |
|---|---|---|
| genesis 5对warm replay中位墙钟 | 266.90 → 21.83 ms | 337.32 → 32.42 ms |
| genesis cold单次（单列） | 270.46 ms | 357.84 ms |
| canonical 8对query中位墙钟 | 20.034 → 19.222 ms | 32.649 → 31.616 ms |
| canonical中位CPU/query | 17.044 → 16.212 ms | 27.581 → 25.437 ms |

所有genesis pair的完整snapshot/reference hash逐项相等，所有canonical pair的完整payload/evidence/cut/reference hash逐项相等，before/after世界身份不变。门槛仅SEMANTICS_AND_EVIDENCE_PASS，不能推导HTTP容量通过。保留PR2 stage3中canonical墙钟退步的push结果，不因本阶段更好而删除。当前context-p3完整配对结果两份也存档；历史Context步骤仍固定e0b worktree且原assert保留，只标历史复现。

r1收集器34PASS+3项带边界状态仍完整保留，其自身load_gate/load_coverage_gate=NOT_EXECUTED、overall_engineering_gate=NOT_MET；外部真实模型BLOCKED_ENVIRONMENT、人工全文阅读PENDING_AUTHOR、overall_p2=NOT_FULLY_ACCEPTED，工程绿灯不覆盖这些目标。

## 结论与归档范围

P2修复合入P3后，当前代码631项工程回归及50/200约定业务完成稳定恢复；原热点/1000 RPS容量仍未满足。未满足用户强制合并门槛，不能凭工程绿灯合并。500/1000/600秒和独立PITR的实际最新结论请使用同HEAD的ci-stage4-scale报告。

永久归档将保留全部20原始负载、4JUnit、canonical/genesis/current-context结果、全部18Actions job日志、诊断与恢复/验收记录及source snapshots，按原字节SHA256去重gzip但保留每个来源。短卷可再生成材料不重复纳入；原ZIP/解包目录不删除，manifest列出每项省略文件哈希，不声称省略字节已归档。每个选中成员先与已核digest ZIP逐字节相等，再压缩、解压比对原hash。
