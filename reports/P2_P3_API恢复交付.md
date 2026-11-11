# P2 第三批恢复、有限 P3 与 GitHub API 交付

记录日期：2026-11-10。仓库：`SKYdayDUCK/NovelFactory`；开发分支：`dev/p2-production-loop`；唯一评审入口：[Draft PR #2](https://github.com/SKYdayDUCK/NovelFactory/pull/2)。

## 1. 结论与验收边界

丢失会话的 P2-07、P2-08、P3-01 和 P3-03 工程成果已恢复到正式开发分支，并完成本轮真实 PostgreSQL、故障恢复、短卷、负载和记忆评测。新增了有界同请求重试、严格的容量计数及独立容量 CI。当前应用代码的 GitHub Actions 完整回归为 **410 passed、0 failed、0 skipped**；Schema/单元/契约/ASGI 分组为 **219 passed**。两个分组有重叠，不能相加为 629 个独立测试。

**整体 P2 验收仍为 `NOT_MET`。** 200 Agent 的 CI 正常/热点负载只完成 **13/21、18/21** 个新世界提交；50 热点虽完成业务，开放到达目标仍未满足。真实模型为 `BLOCKED_ENVIRONMENT`，作者全文评价为 `PENDING_AUTHOR`。确定性夹具不能替代上述门槛。

| 范围 | 本轮实际交付 | 验收状态 |
|---|---|---|
| P2-07 | 原六场景短卷、完整过程与不可变发布恢复；新 CI 再运行；正文错误反例 | 确定性工程闭环通过；真实模型、作者评价待完成 |
| P2-08 | A–H 映射、故障/权限/代际测试、真实 50/200 负载、有界恢复与容量 CI | 已执行范围通过；完整门禁 `NOT_MET` |
| P3-01 | 原事务内批量依赖登记与 payload 指纹复用；新的严格同世界配对测量 | 有限候选通过语义等价检查，不代表容量通过 |
| P3-03 | 中文语料、金标准、四方法×三预算评测、真实服务九项探针 | 确定性评测基础通过；文学质量提升未评价 |

## 2. 恢复事实与安全提交链

开始时重新通过 GitHub 连接器读取分支、PR、历史与 Actions。开发分支实际 HEAD 为 `0609f680fad782c827dd3834a073f8f9bcddaf0d`，Tree 为 `e661a0dff390f1f1714a0abc1f52a1db83e3262f`；P1 主线基线为 `721f155c9cc912cddfdae559afe4f2516d0967f2`。

用户提供的五个历史本地提交不能直接从远程按原 SHA 读出。但执行环境中实际找到了旧工作目录及 Git 对象，并从其中保留的上传映射找到了四个**已经在 GitHub 创建、尚未连到开发分支**的提交：

| 原本地阶段 | 已存在的 GitHub 对象 | 完整 Tree |
|---|---|---|
| `0d8880e` / P2-07 | `2966667f7cb044812155c69b1a1c6bb0a24d0926` | `5087b33cb7a440b0da3349ac7704b5b027091fa5` |
| `c5f6647` / P2-08 | `6d44847159886ccf3d66f43877eafae46837e733` | `7a6d259784e34930cbbc2566e4bd7499f8814371` |
| `246a9e2` / P3-01 | `4c73a206241325ed331bc4d81c26767d9ee8fbea` | `5386862efcf4f92e5c3ec11606d29148e0764183` |
| `3da4748` / P3-03 | `73f190e2f90147624b8a4fc50dce022bb68ae0a7` | `00b28d21eab1bc6abbee6f1ee9bf75940c2c160c` |

逐项核对了递归 Tree 的全部路径、模式、类型和 Blob SHA，并回读 16 个关键文件重新计算 Git Blob SHA；没有仅凭旧报告认定代码已上传。原最终本地 `20971bc...` 的完整 Tree 未在远程发现，所需末尾修正经检查后逐项恢复。详见 [远程对象核验记录](../evidence/recovery_20261110/remote_objects_verified.json)。

随后复用既有对象，按批更新开发分支；后续批次基于实际当前父提交重建 Tree，保留前批修正。所有远程写入均通过已授权 GitHub 连接器的 Blob、Tree、Commit 和 `update_ref(force=false, expected_sha=...)` 完成。每次更新前重新读取 HEAD，更新后回读 HEAD、Commit、Tree 和关键文件。没有执行 `git push`，没有强制更新、重复 PR 或合并 main。

| 正式分支检查点 | 远程提交 SHA | 实际工程 CI |
|---|---|---|
| P2-07 恢复 | `2966667f7cb044812155c69b1a1c6bb0a24d0926` | [42825899216](https://github.com/SKYdayDUCK/NovelFactory/actions/runs/42825899216)，成功，373 项完整回归 |
| P2-08 恢复及 CI/断言修正 | `8b100581c6eed2f7a6e5e6c6471697f230376162` | [42826207659](https://github.com/SKYdayDUCK/NovelFactory/actions/runs/42826207659)，成功，387 项 |
| P3-01 有限优化恢复 | `31f0784f4dac644490957f515dfe6ecfb3dc36bc` | [42826779866](https://github.com/SKYdayDUCK/NovelFactory/actions/runs/42826779866)，成功，390 项 |
| P2-08 有界恢复与容量 CI | `44b9f7fa22a73207f1370ee7e5169daeb4c2dc76` | [42827075042](https://github.com/SKYdayDUCK/NovelFactory/actions/runs/42827075042)，成功，398 项 |
| P3-03 评测基础恢复 | `a1a1cded8634ee4994ea5fbf9ee79f432db81c78` | [42827183713](https://github.com/SKYdayDUCK/NovelFactory/actions/runs/42827183713)，成功，410 项 |

本报告随后的证据整理提交只更新报告、索引和 CI 测量步骤，不改变上述应用/负载实现。其最终 SHA、读回结果及新增 CI 链接写入 PR #2，避免在提交内容中自引用尚未生成的提交 SHA。[API 与 CI 证据索引](../evidence/recovery_20261110/github_evidence_index.json)记录本报告生成前已核验的检查点。

## 3. P2-07：真实服务上的短卷闭环

原短卷 [《潮闸余灯》](../evidence/p2/batch3/short-volume/run/short_volume.md)为六场景、2204 Unicode 码点的原创确定性测试作品。找回原文件后重新核对 **551 个校验文件，零差异**；产物索引包含 **538 条记录**。因此这里是实际恢复原字节，不是凭旧报告重写后声称相同。

流水线复用既有规划、调度、Kernel、任务、Agent Gateway、叙事、审核与发布服务。两条粮运路线经角色任务和联合裁决交汇；不同角色获得不同知识切面；毁印意外使两个未来规划节点失效，局部重规划保留另外四个节点，并拒绝旧代际任务。并行首稿固定 WorldRef，接续任务获得实际前文。

正文抽取读取实际 TextDelivery，覆盖有限中文数量、状态、所有权、知识、位置与成员关系表达。正式世界事件与正文声明对账，不能把预期场景事实直接充当抽取结果。真实 PostgreSQL 反例分别把 30 袋改成 40 袋、毁印改成完好、加入无法识别的重要新事实，均拒绝无依据发布。

审核保留修改意见和新旧正文，整卷经历结构、接缝、语言编辑后生成不可变内部发布。正式世界事件在叙事前后及发布后均为 **7**；粮食只执行一次 `100/0 → 70/30` 转移，没有因多处叙述再次扣减。原发布 manifest SHA-256：`95f63f4936c91afa8621a206fa7f8f306e25f90f6721c89c04404204e5309a3e`。

完整记录入口：

- [运行清单](../evidence/p2/batch3/short-volume/run/run_manifest.json)、[全部校验值](../evidence/p2/batch3/short-volume/run/checksums.json)、[产物索引](../evidence/p2/batch3/short-volume/run/artifact_index.json)。
- [过程轨迹](../evidence/p2/batch3/short-volume/run/process_trace.jsonl)、[世界接受回执](../evidence/p2/batch3/short-volume/run/world_receipts.json)、[模型调用记录](../evidence/p2/batch3/short-volume/run/model_call_results.json)。
- [实现说明](../docs/implementation/P2_端到端短卷生产.md)及 `tests/integration/test_short_volume.py`。

新的 D 批 CI 再次实际生成短卷与完整过程，其原始记录位于 run `42827183713` 的 `p1-postgres-concurrency-1` artifact，和原恢复记录属于不同次运行。原运行的 15 次生成均为 `TEST_FIXTURE`，真实提供商调用为 0。抽取器只覆盖已声明测试语法；runner 保存了可审计轨迹，但不宣称能够自动从任意 Python 程序位置续跑。

## 4. P2-08：A–H、故障和负载

把本轮 GitHub 的 410 项完整 JUnit 与四份 50/200 原始负载 JSON 输入现有矩阵生成器，使用 `--gate-scope full`。真实命令退出 **1**，结果为：37 条设计检查中 **34 PASS、1 PARTIAL_SYNC_FENCE_ONLY、1 PASS_VISIBLE_SHAPE_ONLY、1 NOT_IN_SCOPE_P3_02**；已映射测试执行通过，四种负载覆盖已执行，完整容量和整体工程门禁均 `NOT_MET`。见 [机器可读矩阵](../evidence/recovery_20261110/acceptance_matrix.json)和[实际命令退出记录](../evidence/recovery_20261110/acceptance_command.json)。

测试涵盖历史状态/未来计划失效、历史修订/分支隔离、多线交汇/联合裁决、角色认知、宏观效果与微观叙事隔离、并发/撤权/任务代际、固定快照写作、发布/崩溃/幂等恢复。保留的边界是：

- A-04 验证同步任务 fence，尚无真实异步依赖 worker 暂停再恢复的验收。
- E-02 验证可见返回结构和拒绝信息，没有统计时间/大小侧信道证明。
- G-03 宏微等价替换属于 P3-02，未借本轮测试宣称完成。
- H-05 使用最小 watermark 消费器，不代表完整生产索引 worker；JOINT 重绑定仍须明确重新资格审查。

### 4.1 GitHub Actions 容量结果

[真实容量工作流 42827058501](https://github.com/SKYdayDUCK/NovelFactory/actions/runs/42827058501)运行于 `44b9f7f...`，四个 job 为 **1 成功、3 失败**，全部保留 artifacts。各 runner 为 4 个可见 CPU、PostgreSQL 17.11、单 API 工作者、连接池 8、fsync 开启；不同 job 的 CPU 型号不同。200 档是 262 个实体的小夹具，不能当成长篇大世界容量。

| CI 场景 | 发出逻辑/计划 | HTTP 尝试 / 额外重试 | 新世界提交/目标 | 总时长 s | 业务恢复 | 容量 |
|---|---:|---:|---:|---:|---|---|
| 50 正常 | 286/286 | 286 / 0 | 6/6 | 34.19 | PASS | PASS |
| 50 热点与开放到达 | 536/536 | 713 / 177 | 6/6 | 54.12 | PASS | NOT_MET |
| 200 正常 | 1113/1141 | 1400 / 287 | 13/21 | 167.63 | NOT_MET | NOT_MET |
| 200 热点与开放到达 | 2128/2141 | 2297 / 169 | 18/21 | 202.84 | NOT_MET | NOT_MET |

| CI 场景 | 全部 HTTP 尝试 P50/P95/P99 ms | 逻辑完成 P95 ms，含计划等待 |
|---|---|---:|
| 50 正常 | 46.97 / 379.13 / 461.30 | 383.02 |
| 50 热点 | 1503.95 / 4562.58 / 5468.66 | 12488.14 |
| 200 正常 | 1523.01 / 6974.63 / 9679.26 | 13796.18 |
| 200 热点 | 1733.16 / 6170.28 / 9597.79 | 36541.70 |

50/200 热点的实际首次调用开始速率为 **61.48/s、25.70/s**，明显低于冻结的 **1000/s** 目标。生成器和服务处于同进程，call-start 还可能包含 HTTPX 连接等待，因此当前数据也不能直接等同于独立客户端的线速。没有把 429、额外尝试或重复回执记成新世界吞吐。

四个 CI artifact 下载后核对 ZIP SHA-256，独立重算 **4696 次 HTTP 尝试、4063 个逻辑操作、633 次额外重试**。398 个重试操作保持请求哈希不变；43 个重复回执保持请求字节、响应哈希和完整回执一致。全部世界 revision、事件数和资源余额不变量一致，`correctness_errors` 为空。见 [独立计数记录](../evidence/recovery_20261110/load_independent_review.json)。这说明未观测到错误世界效果，不意味着未完成业务可以通过。

本地另外执行四档同冻结驱动负载，50 正常/热点分别 6/6、6/6；200 正常/热点分别 **17/21、14/21**。本地与 CI 宿主和应用 Git 来源不同，分别保留，不能择优替代。完整结果、冷热查询、连接预算、失败位置、源码指纹和原始文件哈希见 [负载诊断报告](P2_08_恢复后负载与重试验证.md)及[精简数值](../evidence/p2/recovered-load/summary.json)。

### 4.2 旧失败原因与本轮改动

重算旧 7/21 和 8/21 原始记录：40 个提案分别只有 27、26 个成功，随后证书分别只有 14、15 个成功。旧驱动遇到操作队列背压就结束后续步骤，两次均记录 50 次队列超时且基础设施重试为 0。已经准备完成的独立候选接受成功。队列峰值低于配置上限，直接失败点是 2 秒操作等待预算耗尽。

本轮只增加负载客户端的有界基础设施恢复：默认仍不重试；测量固定最多 5 次尝试、30 秒新尝试启动预算。请求只序列化一次，幂等键、任务代际、WorldRef 和证书保持不变。仅重试校验通过的 429/503 恢复动作或真实传输错误，不重试权限、历史、代际和语义冲突。退避醒来后再次检查截止时间；已经在途的 HTTP 请求可能在 30 秒启动预算之外结束。

另增真实 PostgreSQL/TCP 测试：正式提交后丢弃成功响应，观察实际 `RemoteProtocolError`，原请求恢复同一回执，正式世界效果始终一次；随后撤权、改代际必须拒绝。没有降低任务数、原定目标、权限校验或服务端队列要求。首次容量、恢复后业务完成、总体容量分开门禁；未发出的下游调用仍算未完成。

没有数据库死锁或新增物理块读取；应用高用户态 CPU、PostgreSQL 多为等待客户端，支持应用状态解码/校验和同进程竞争的解释，但尚不足以定位到某个函数。新 200 档仍有背压耗尽及调度长尾，因此本轮停止继续调参，保留 `NOT_MET`。

## 5. P3-01：重新测量有限优化

恢复的改动仅将相同 payload 的 canonical 指纹移出逐 subject 循环，并在同一事务内分批登记依赖（每批最多 256 行）。不改变权限、WorldRef、证书、scope 或代际。原有查询减少无关读取的 P1 修复没有重新开发。三项真实 PostgreSQL 原子性测试覆盖完整 scope、批量插入失败的全事务回滚和采样后撤权。

新配对测量使用同一 Kernel、固定 WorldRef、不同编译任务，读取精确 `0609f680...` 基线 Git blob，并验证六个共享核心文件及旧 add 方法保持不变。夹具实际 1001 个实体、1000 条初始关系、12 个已接受事件；每档 5 对，总 15 对。

| 每次 subject 数 | 基线 SQL → 候选 SQL | 基线 → 候选中位延迟 ms | 本次下降 |
|---|---|---|---:|
| 1 | 51 → 47 | 77.37 → 73.39 | 5.15% |
| 20 | 89 → 47 | 146.30 → 124.81 | 14.69% |
| 100 | 249 → 47 | 429.36 → 230.26 | 46.37% |

15 对完整 facts（包括 evidence）和验证后的 dependency guard material 均相等；world head、state hash、manifest hash、scope versions 和 event count 在编译前后全部不变。独立审核重新计算保留哈希和中位数。见 [本轮配对摘要](../evidence/recovery_20261110/context_paired_summary.json)与[独立重算](../evidence/recovery_20261110/context_independent_review.json)。

这是共享开发宿主上暖缓冲区、单一夹具的有限比较。指纹复用和 SQL 批处理同时变化，没有分别估计因果贡献；没有置信区间、生产 SLO 或 200 Agent 容量结论。历史 JSONB 大载荷重复读取和验证成本仍存在，未建立新的状态缓存或第二套世界状态系统。

## 6. P3-03：文学记忆与检索评测基础

新运行重新生成 **48 章、384 个来源片段、78952 Unicode 码点**的中文语料，包含 **35 个金标准问题**（28 检索、7 权限/秘密隔离），四种方法和三个上下文预算共 **420 条结果**。固定摘要、角色来源索引、无来源链接时态图谱、有来源链接时态图谱使用相同评测框架；预算为 1500/3000/6000 UTF-8 字节，包含上下文头部，不把字节预算伪称提供商 token 数。

检索不读取金标准作为输入，先按身份和时间切面过滤候选；越权 related-source ID 不泄漏。相较找回的历史运行，除耗时外全部 420 条结果字段一致。3000 字节预算的证据召回均值分别为 **0.690476、0.755952、1.0、1.0**；这些只是该合成任务的度量，方法间候选覆盖不同，不能单独归因到“图结构更好”。普通摘要是固定首尾摘录，不是语言模型摘要。

真实 Kernel、WorldQuery 与 Context Compiler 服务探针在 PostgreSQL 17.11 再运行，每种策略 3 次，九项门槛全部满足：完整状态、编译回执幂等、认知不继承客观秘密、其他角色/世界越权拒绝、暖缓存及旧回执重新授权、历史固定切面、毁坏与待定状态分离、硬预算不部分绑定、检索零世界效果。focused/broad 每次均 47 条 SQL，上下文分别 1158/2237 字节；世界前后不变。

见 [离线结果](../evidence/recovery_20261110/memory_offline_summary.json)、[真实服务摘要](../evidence/recovery_20261110/memory_service_summary.json)、[独立复核](../evidence/recovery_20261110/memory_independent_review.json)。没有真实模型或独立文学评价，因此文学质量改善结论为 `NOT_EVALUATED`。

## 7. CI、原始证据与复现

本轮保留 Schema、原始 35 项机制实验、单元、PostgreSQL 17、竞态、正文对账、审核发布和崩溃恢复的实际执行。工作流显式使用 bash，使管道中命令失败不会被 tee 的成功隐藏；基准使用模块入口，避免脚本路径导入失败；按 SHA 区分并发组，避免后批取消前批。失败 job 仍上传 artifacts，容量工作流没有 `continue-on-error`。

最后证据提交扩充主 CI：完整 Git 历史用于核验精确基线 blob，实际执行严格 Context Compiler 配对测量和记忆服务探针。完整 `paired-run.json`、服务 traces、短卷、JUnit 和日志放入 Actions artifacts，仓库保存本轮小摘要、证据哈希和稳定索引。Actions 保留期为 90 天；保留期之后需要重新运行或按项目归档政策另行保存。

本地复现必须先准备 PostgreSQL 17，并设置 `NF_DATABASE_URL`/`NF_TEST_DATABASE_URL`，或使用仓库 `scripts/with_postgres.py` 启动隔离实例：

```bash
uv sync --locked --group dev --python 3.12
uv run --no-sync python validation/validate_package.py
uv run --no-sync python -m alembic upgrade head
uv run --no-sync python -m pytest tests -q --junitxml=ci-evidence/postgres-all.xml
uv run --no-sync python scripts/run_short_volume.py \
  --execution-kind TEST_FIXTURE --output ci-evidence/short-volume \
  --object-root /tmp/nf-short-volume-objects
uv run --no-sync python -m benchmarks.context_paired_profile \
  --entities 1000 --history 12 --subjects 1 20 100 --repeats 5 \
  --output ci-evidence/context/paired-run.json
uv run --no-sync python benchmarks/memory/evaluate.py --output ci-evidence/memory
uv run --no-sync python -m benchmarks.memory.service_probe \
  --repeats 3 --output ci-evidence/memory-service
```

四档容量命令见 [P2 负载工作流](../.github/workflows/p2-load.yml)和负载诊断报告。完整 A–H 门禁应输入真实 JUnit 和四份 50/200 JSON，使用 `--gate-scope full`；预期当前结果仍退出 1。主工程 CI 的 `executed-tests` 门禁只是已执行测试范围，不覆盖未通过的正式容量目标。

## 8. 未解决项与下一阶段

优先独立审核恢复后的状态、权限、证书、任务代际和幂等边界，然后校准独立进程/机器的开放到达生成器，并用真实 CPU/事务采样定位大 JSONB 状态解码、重复校验和长临界区。只有严格语义等价检查和原 50/200 目标同时成立时，才能推广新的性能优化。不要靠降低任务、降低到达率或延长预算把验收改成通过。

随后补齐 A-04 真实异步 worker 恢复、H-05 完整索引 worker 和需要的侧信道测量，再推进 P3-02 宏微等价替换。授权模型路由可用后重跑真实模型生产；作者全文阅读提供独立文学评价。当前只检查相关环境变量是否配置，未读取凭据文件或发送真实模型请求，详见 [模型环境记录](../evidence/recovery_20261110/model_environment.json)。

PR #2 保持 Draft，等待独立审核；本轮不合并 main。
