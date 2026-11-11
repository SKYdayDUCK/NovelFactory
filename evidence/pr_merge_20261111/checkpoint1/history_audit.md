# NovelFactory 本地历史、上传映射和原验收门槛审计

审计范围：只读旧工作树和根 Agent 已取得的最新远程递归树；未启动服务、负载或测试，未写远程，未修改任何旧工作目录。本笔记及同目录 `history_hash_inventory.json` 为临时接管材料。

## 1. 实际基线与完整性结论

- 根 Agent 重新取得的 PR #2 HEAD：`e0b0f2b080f3225d8be8e202e13258bc480f5b84`；PR #3 HEAD：`24b203f6fcda5f36105dfd9a37cd833671d7215c`，与用户历史检查点一致。两个 PR 的 comments/review_threads 当前为空（根 Agent 提供的实时查询结果）。
- 对 PR #3 最新递归树中的 **2497 个 blob** 逐文件计算 Git blob SHA-1，与 `/workspace/scratch/fe1ee59b659d/NovelFactory` 比较：**2497/2497 完全一致，0 缺失、0 差异**。此旧目录的 Git HEAD 停在 206eaa5，不能把 `git status` 的脏状态误认为尚未上传。
- checkpoint1/2/2_fix/3/4/final_handoff 合并后的 **107 个最新变更 blob** 全部与最新远程树一致。`checkpoint4_partial.json` 是已由最终 checkpoint4 覆盖的历史中间状态；不能据其 BLOBS_UPLOADED_NOT_COMMITTED 判定 CP4 遗失。

| 检查点 | Commit | Tree |
|---|---|---|
| CP1/P2 | e0b0f2b080f3225d8be8e202e13258bc480f5b84 | e1a8b5b4e3c72314a5f26152215de8d90c5b9e29 |
| CP2 | 6de6e15ae3051736eedfadde7a53b47f2d07b913 | 3132c2dd35cefd51a45a81c62784e2a79aa5e4a5 |
| CP2 收集修复 | f6d48def76b982dfd9fbd5fca60a20f937576e8b | 7eca257ef51fca3023fc85bc63c7f59737b79260 |
| CP3 | 75273ab96cf16b0f1ae9c4cd809835a37c7cf2aa | e8293035075b34c7396a0e53a54f713da011140a |
| CP4 | 9bf3794fbabb1074e1309e3217d6bbe78ce3c704 | 8591e40df3c0fb50616c45c23f63e5d394e23402 |
| 最终交接 | 24b203f6fcda5f36105dfd9a37cd833671d7215c | feb6d07992c6339d64ec5804fe6651bcebade045 |

上传原始映射保留在 `/workspace/scratch/fe1ee59b659d/api-delivery/`。父提交严格串联上述检查点。所有旧目录保持原样，建议从已核验 tree 建立新的独立工作副本继续修复。

## 2. 本地未跟踪/已修改项分类

完整逐文件路径、字节数、Git SHA 和 SHA-256 在 `history_hash_inventory.json`。这里只列会影响接管判断的类别。

| 目录（/workspace/scratch/ 下） | 已修改/未跟踪文件情况 | 建议 |
|---|---|---|
| b26d73243868/NovelFactory | 干净 206eaa5 基线 | 保留基线 |
| b26d73243868/load-work | 12 项与 PR3 同路径完全一致；1 项旧负载驱动已被新版替代；21 个本地独有负载 JSON/日志/PG 会话元数据/源码快照 | 原始失败记录必须保留；无需重写已上传负载修复 |
| cf3ee8d0c6c4/NovelFactory | 2 个本地独有旧交接报告及 github_upload_map | 保留历史映射与说明，不作为当前源码覆盖 |
| cac4fdab7f30/NovelFactory | 6 项与 PR3 完全一致；3 项路径已有更新版本；60 项本地独有旧证据、评测说明、Retcon 用例 | 见下列风险说明，先保留，不直接恢复覆盖 |
| fe1ee59b659d/NovelFactory | 107 项全部与 PR3 相同；并核对整个2497 blob树相同 | 当前完整源码可恢复来源 |
| fe1ee59b659d/stage2 | 44 项相同，4 项是已被 CP3/CP4 取代的旧版本 | 保留阶段快照 |
| fe1ee59b659d/stage4 | 92 项相同，5 项旧报告被最终交接补充 | 保留阶段快照 |

### 真正需要保留的旧候选及证据

1. `cac4fdab7f30/NovelFactory/src/novel_factory/application/planning.py` 本地额外 `_replan(..., receipt_context, guard, on_commit)` 组合入口。SHA-256 `d83eb0757860f65f7fafe0e1c421a676523edf28b507d12dc9ee990fc59fe3b7`。它是旧派生 worker 候选接口；现版本另有已验收的 derived_worker，不能无依据覆盖当前规划服务。其内容已在前轮 takeover 索引登记。
2. `cac4fdab7f30/NovelFactory/tests/integration/test_retcon_literary_memory.py`，SHA-256 `58c9dba0e03226da4b59e971ad1ca781eff9ce7aaa6d758dfb0c397f4d088354`。它依赖已不在当前基线的 `test_p2_combinations`，本地保留的 `retcon-memory/tests.json` 实际 **1 failed / 1 collected**，不应报告已通过或机械并入当前回归。前轮明确用跨卷服务测试承接场景，原用例及失败证据仍保留。
3. `cac4fdab7f30/NovelFactory/evidence/p3/foundation/` 原22项通过的离线评测证据、示例对象、ledger和盲评包；当前 PR3 已恢复评测源码并增加循环金标准漏洞修复，不应恢复旧 store.py/test_foundation.py 覆盖新版本。`docs/implementation/P3_03_评测基础.md` 属旧设计说明，保留但不自动视为最新架构。
4. `b26d73243868/load-work/evidence/p2/recovered-load/` 的4个原始50/200正常/热点测试、命令日志及源码快照。这些未全部入Git，并非可随意删除的冗余物；删除前需实际持久化备份及哈希恢复索引。90天Actions Artifacts有时效，不能永久取代所有原始基线。
5. `cf3ee8d0c6c4/NovelFactory/evidence/p2/batch3/github_upload_map.json` 与 `reports/P2_第三批与P3候选交接.md`，保留追溯用途。

结论：**未发现需要重新开发或紧急找回的最新 P3/P4 源码。** 最新整个工作树已远程化。旧本地独有候选与失败记录需保全，不应混入正式基线制造倒退。

## 3. 原始硬门槛（不得偷换）

已阅读根 AGENTS/SKILLS、benchmarks/AGENTS、nf-concurrency-review、docs/15、相关实际报告和3个容量工作流。旧根 AGENTS 仍写“P3其他/P4未授权”；当前用户任务显然覆盖本轮范围，该旧段落需要更新，不能让其覆盖当前明确授权。

### P2 50/200 档

`benchmarks/p2_acceptance_matrix.py::load_coverage` 要求四格覆盖：50 mixed、50 hotspot_overload、200 mixed、200 hotspot_overload。热点要求 hot_fraction≥0.8、有实际 open-loop rate/rounds；`.github/workflows/p2-load.yml` 和 `.github/workflows/p3-p4-evidence.yml` 冻结参数为常规到达10/s、open-loop1000/s、rounds5、最多5次/30秒重试，`--gate-scope capacity`。

`p2_behavioral_load.run_benchmark` 的业务通过须同时满足：

- correctness_errors 和 unresolved_operational_errors 均为空，所有原定逻辑请求均已发出。
- 新正式修订和新增选定效果组均为 `agents/10 + 1`：50档6，200档21，500档51；重复回执不可计为新提交。
- 共享及所有独立资源最终量均为99（初始100、预定合法扣减1）。
- 指定阶段连接全归还，应用池峰值≤8、结束借出0。

`first_attempt_capacity_gate` 还要求首试失败数为0；`capacity_gate` 再要求热点首次调用开始实测速率≥冻结1000/s的95%，即≥950/s。正常场景到达门槛为NOT_APPLICABLE。业务有界恢复PASS不自动令容量PASS。`engineer_correctness=FAIL` 混合包含业务未完成，不等于发现权限/状态破坏，报告须分开解释。

请求数固定公式为 `5*agents + 7*independent + 1`，热点另加 `agents*5`：50正常286/热点536，200正常1141/热点2141，500正常2851。不能从统计分母删掉未发送的下游操作。

### P3 500/1000 与稳定性

| 场景 | 原固定条件 | 通过要求 |
|---|---|---|
| 500 mixed | 同P2规则，双API、总池8、原5次/30秒 | 2851逻辑请求，51新提交，业务不变量；首试0失败才容量PASS |
| 1000只读开放到达 | 1000真实任务、5轮5000请求、1000/s、客户端池256、双API、无自动重试 | 全5000请求都有wire-start，实际header发送开始≥950/s；所有响应有效且无correctness error才capacity PASS |
| 500只读600秒 | 20/s、12000次、同服务进程、双API | 所有12000有效响应，无client_drop/429/其它错误，实际开始≥19/s，连接归还 |

1000 mixed写入未执行；600秒探针不是多小时长稳。原脚本一小时仅作为PARTIAL_READ_ONLY标签下限，不是生产SLO；设计文档建议4小时/24小时，但仍未执行。没有发现已冻结的数字P95/P99强制SLO；P95退化20%在设计文档中只是候选门禁，不能当现有批准阈值。吞吐、延迟、业务完成、正确性必须分别呈现。

## 4. 已有失败证据与限制

- CP1 GitHub `42837414314`：200 mixed21/21且容量PASS；200 hotspot14/21、10关键操作耗尽、37下游未发、实际到达32.5749/s，业务/容量NOT_MET。正常与热点runner CPU型号不同，不能作为固定硬件配对。
- CP3 GitHub `42843349105`：500 mixed17/51、148逻辑未发、333终结失败；1000读1403有效/3506个429/91传输错误，发送45.0452/s；600秒探针11997/12000及3个429。三项NOT_MET。
- 最终HEAD24b203f的独立容量运行 `42848907323`：200 mixed13/21，热点13/21，所有原尝试/重试与未发逻辑数量保留。最新结果优先，不能引用更早 CP4 的18/21、21/21或本地好成绩替代。

优先核对：

- `evidence/p3_p4/checkpoint1/github_verified.json`；SHA-256 b7b356ccf5f5eec51e41cf80e48e1a6b21d2936d059e0918ae7ca9045538683b。
- `evidence/p3_p4/checkpoint3/github_scale_verified.json`；SHA-256 9c9242621f6d83ac517bbfbf17878c47171b83a47b0e55581cfc3a6ce6c4019d。
- `/workspace/scratch/fe1ee59b659d/api-delivery/final_head_performance_check.json`，最终HEAD独立重算结果。
- `/workspace/scratch/fe1ee59b659d/api-delivery/final_handoff.json`，最终HEAD工程CI/PITR/容量读取及对象映射。
- 详细解释见 `reports/P2_P3_01_性能专项.md`、`reports/P3_04_并发扩展与长稳验证.md` 和 `reports/P3_P4_开发与验收总报告.md`；注意总报告主体早于最终24b203f文档提交，其“最终200”行实际指9bf3794，需要按实际SHA阅读。

既有 profiler 证据定位重复 Schema/point_key 验证、JSONB载荷及血缘读取；已实施有限point_key LRU和元数据血缘查询。剩余成本含JSONB解码/哈希、完整重放、上下文47条SQL、准入排队及客户端调度。SQL cursor累计时间含等待且排除结果解码；各进程peak相加不是同时峰值；pg_stat_activity样本数不是等待时长。当前不能仅CPU总值归因。

## 5. 对合并资格的建议

当前仍有明确、适用且用户要求保留的容量门槛失败。工程CI和PITR绿色不能抵销，必须真实优化并复验；若无法满足应保留分支与失败证据，按用户授权停止在合并前。历史源码恢复本身不阻断；重点在稳定业务完成、原容量目标及可复现硬件资源条件。真实模型为0，文学NOT_EVALUATED，人类全文评价PENDING_AUTHOR，发布说明继续保留。
