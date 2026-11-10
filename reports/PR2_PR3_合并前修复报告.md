# PR #2 / #3 合并前修复报告

日期：2026-11-11（北京时间；GitHub 运行日期为 2026-11-10 UTC）。仓库：`SKYdayDUCK/NovelFactory`。

## 1. 审核结论

本轮完成两轮性能修复、恢复缺陷修复、独立负载观测、重复实验、历史材料保全、独立安装启动和实际 GitHub CI 核验。**PR #2 与 PR #3 均未合并，继续保持 Draft；main 仍为 P1 提交 `721f155c9cc912cddfdae559afe4f2516d0967f2`。**

阻断来自原有强制容量条件：50/200 热点的首试与 1000 RPS 到达要求仍 `NOT_MET`；PR3 的 1000 Agent 和 600 秒探针也未全部通过。业务效果完成、程序正确性、工程测试通过与容量达标分别记录。未禁用工作流、降低阈值、删除失败请求、扩大重试预算或更改原业务预期。

修复分支可作为继续开发和排查的工程候选；它们不构成本轮获准合入 main 的完整验收基线，也不构成生产容量或文学质量验收。

## 2. 接管基线与历史

先检查旧工作目录、Git 对象、上传映射及未同步材料，再读取远程实际状态。下表与用户的历史检查点相同，但来自重新查询。

| 对象 | 初始实际 HEAD | 状态/依赖 |
| --- | --- | --- |
| main | `721f155c9cc912cddfdae559afe4f2516d0967f2` | P1 |
| PR #2 | `e0b0f2b080f3225d8be8e202e13258bc480f5b84` | Draft，base=main |
| PR #3 | `24b203f6fcda5f36105dfd9a37cd833671d7215c` | Draft，base=dev/p2-production-loop |

旧 `fe1ee59b659d/NovelFactory` 虽显示 Git HEAD 落后且工作树有变更，实际 2497/2497 个文件 blob 与初始 PR3 一致；107 个最新上传映射也全部匹配。没有用旧远程版本覆盖本地新增代码，也没有重复开发已上传模块。其他目录中 96 个独有或已被远程后续版本取代的历史材料原字节归档，不并入活动源码或通过统计。

初始两 PR 无评论、Review submissions 或 Review Threads，没有实际未解决审查意见。main 没有保护规则要求的检查上下文，仍严格执行用户的合并条件。Git 图可合并不代表容量验收通过。

原始失败保留：P2 的 200 普通 21/21、热点 14/21；初始 PR3 最新独立 200 普通/热点均 13/21；更早 PR3 500 混合 17/51、1000 读 1403/5000、600 秒 11997/12000。它们来自不同运行/硬件，不能直接作为同硬件因果对照。

## 3. 修复位置、行为与验证

| 问题 | 修复位置 | 行为和验证 |
| --- | --- | --- |
| 重放反复构建同一初始世界 | `src/novel_factory/domain/world.py` | 仅复用已验证且无事件效果的初始快照；8 项/16 MiB 有界纯计算 memo，不可变字节缓存、独立返回对象；事件重放和原检查不变。10 个针对性测试及完整快照配对。 |
| 已选事件逐项查询/插入 | `src/novel_factory/application/kernel.py` | 使用现有 many/add_many，保留 work_id、原选择顺序、同一事务与约束回滚；prepare 仅在同一 RR 事务 parent==base 时复用 validated revision。真实 PG 回滚/竞争回归。 |
| 严格指纹校验重复类型分派 | `src/novel_factory/domain/canonical.py` | 对原生字符串及已确认的字典键直接做相同 UTF-8 检查，保留安全整数、拒绝类型子类/float、自定义对象、首错误顺序及 JSON/hash 字节。冻结源码与 500 个生成输入、显式非法输入差异验证。 |
| 总 CPU 无法充分定位瓶颈 | `benchmarks/service_diagnostics.py`、`client_timings.py`、`http_load.py`、`p2_behavioral_load.py`、`process_server.py` | 增加阶段 wall/thread CPU、JSON 解码、执行器/准入等待、HTTPcore pool/header、进程/调度器样本、事务边界及 PG 等待观测。原业务/容量判定不变，重叠计时不得相加。 |
| 超时 SQL 未计入观测 | `benchmarks/http_load.py`、`tests/service/test_measurement_failures.py` | handle_error 记录失败语句与等待；真实 PG statement_timeout 回归通过。 |
| 备份验证漏掉新增可导入模块/Schema | PR3 `src/novel_factory/infrastructure/recovery.py` | require_runtime=True 先比完整文件集合，再比 hash；复现模块遮蔽及新增 Schema 漏检，修复前 2 项失败、修复后通过；archive-only 和 v1 manifest 兼容保持。 |
| CI 源码触发和重复证据不足 | `.github/workflows/p2-load.yml`、`p3-p4-evidence.yml`、PR3 `p3-scale.yml` | 补齐适用源码/迁移/依赖/PR/main 触发；P2 每格同 runner 重复两次，任一次失败均导致 job 失败，失败也保存 artifact，阈值不变。 |
| 历史 Context 实验错误绑定新内核 | `.github/workflows/p1-integrity.yml` | shared-core 原断言保留，固定 e0b worktree/导入路径重现历史实验并明确标注；当前内核用完整回归、当前 Context 配对、新 genesis/canonical 配对分别验证。 |
| PITR manifest 三文件未上传 | PR3 `.github/workflows/p4-recovery-drill.yml` | 显式保存 archive_segment.py、restore.conf、source.conf，上传前比选择集/哈希；新 CI 包 11/11 成员完整，旧 8/11 包与缺口事实不改写。 |
| 计划 1000 RPS 与客户端实际能力未分开 | PR3 `benchmarks/client_calibration.py` | 直接复用未改 offer，独立固定回包进程、两尺寸各两次、全部 20000 条；标为 GENERATOR_CALIBRATION_ONLY，业务/容量 NOT_APPLICABLE，不替代正式实验。 |

Schema 验证 memo 候选实测 35.787→37.204 ms、慢 3.96%，未写入正式注册器，负结果和候选代码保留。canonical 优化在本地配对中有收益，但 CI 一次 push 的墙钟回退 4.78%；两份完整结果均保留，不能宣称所有环境均稳定降低墙钟时间。

## 4. 核心不变量

世界效果接受仍在原事务、幂等回执、版本/证书/授权/任务代际检查下完成。未增加并发正式效果写入，没有缓存授权或当前 HEAD，没有把重复回执计成新提交。批量读取后重建原选择顺序，批量写入由原数据库约束和同一事务保证原子性。

初始快照 cache key 包含保序输入字节、解释器/投影版本和规则身份；进入前仍执行原 plain() 与严格 canonical 校验。缓存只保存有效初始快照，不携带未提交事件效果。16 MiB 是每工作者输入和输出字节预算，不是 RSS 或全部署预算。

原逻辑任务数、连接池总额、准入队列、HTTP 30 秒超时、最多 5 次/30 秒重试和全部成功判定未放宽。新增服务阶段包装由 benchmark 诊断开关控制。测量包有包含关系，不能将累计 wall 秒或 CPU 秒相加推算服务成本。

## 5. 实际测试、安装与迁移

| 范围 | 实际结果 | 边界 |
| --- | --- | --- |
| P2 第一修复 0a0af49 | 两次 GitHub 全套 PG 436/436，unit 子集 240/240 | 子集不能与全套相加。 |
| P2 最终代码 971fbef | 两次 GitHub 全套 PG 472/472，unit 子集 276/276 | 均 0 failure/error/skip。 |
| P3 第一修复 008fccf | 两次 GitHub 全套 PG 595/595，unit 子集 344/344 | 包含既有 P3/P4 与恢复回归。 |
| P3 最终代码 e3c7b79 | 两次 GitHub 全套 PostgreSQL 631/631；unit 子集 380/380，均 0 failure/error/skip | 仅绑定实际 SHA，不从前一版外推。 |
| 精确旧 main→P2→P3 | PASS | main 实际源码建立 0002，再升 0003；33 旧业务表摘要不变、3 新表为空、6 旧回执、7 历史 revision、权限拒绝与 47 对象 hash 保全。 |
| P2/P3 干净安装启动 | 两者 PASS | 新 venv，40 个依赖原锁 hash，离线 wheel 构建安装、包资源、迁移、独立 Uvicorn、真实 HTTP 200/401、历史查询、37 表/44 对象恢复和 task fencing。 |
| 最终 PITR 附件 | 两次实际成功包均 11/11 | PR 首次 curl56 公钥下载失败，恢复未执行；保留原日志后只重试失败 job。 |

P0/P1 既有测试未删减，0001/0002 迁移与原核心契约不变。0003 是既有 P2 增量，新增 3 张表及不可变触发器；本轮未改存储模型或新增迁移。精确旧应用兼容实验只证明所保留 fixture/历史，不能外推任意生产数据或零停顿升级。0003 原 downgrade 拒绝删除封闭候选，不能宣称无损降级。

独立安装的 requirements.lock SHA256 `f2badc1695fbf181f0c93c69b2f1dde9543f5d58dbf4aeaadd266f7ef6af443e` 前后不变，实际导入来自新 venv，不依赖 editable 源码。测试和原始 JUnit 见 evidence/pr_merge_20261111 下的 checkpoint、ci_stage、clean_install、pr3_recovery 目录。

## 6. PR2 文件范围与保全

初始 PR2 对 P1 有 2112 个变更文件、当前字节约 22.58 MB，其中 1999 个证据文件约 19.84 MB。精确重复仅约 0.63 MB；没有为缩小 PR 删除失败或破坏审计链。源码、测试、Schema bundle、冻结迁移、锁文件、语料/金标准与复现脚本均保留。

PR2 是 P2 完整链路及历史已声明的有限 P3-01/P3-03 基础：上下文批量化、元数据血缘、纯值 point_key 缓存、确定性检索和评测，并非“完全不含 P3”。宏微替换、跨卷、派生工作者、在线备份/PITR 等高级模块仍在 PR3；本轮未搬回 PR2，也未新建状态或权限系统。

新逐请求证据用保留原字节的 gzip、run/artifact/hash 映射长期保存，Actions 的 90 天附件另供下载。90 天不是永久备份，未据此删除旧材料。96 个本地独有/旧版材料在 PR3 legacy_preservation 中逐成员解压复核；旧 Retcon 失败用例不冒充通过，旧规划候选不覆盖现行实现。

有限静态路径/模式和结构扫描未发现确认的真实密钥、私钥、私有邮件、数据库数据文件或未声明依赖。兼容实验 private-state 和 bootstrap 访问文件未上传。扫描不是绝对无秘密的形式化证明。冻结参考源码保持原字节/hash；工程 CI 未执行全仓 Ruff，不能把绿色描述为全仓 lint 通过。新增可编辑 benchmark/测试定向 Ruff 和 diff 检查通过。

README/AGENTS 只纠正实际阶段和限制，原权限、审批、安全与验收要求不变。

## 7. 远程交付与发布边界

每个源码阶段均通过 GitHub 连接器执行 HEAD/tree 查询、blob/tree/commit 创建、expected SHA 非强制 ref 更新、完整树/关键源码读回和实际 CI 核验。PR3 以额外父提交继承 P2，保留祖先关系；没有 CLI push、force push、Squash 或历史重写。

原容量门槛未满足，未执行 Ready、PR merge、改 PR3 base 为 main 或生产版本标签。改变合并范围或接受 NOT_MET 需要用户额外明确批准，本轮没有假定该例外。

真实外部模型调用 0，`BLOCKED_ENVIRONMENT`；人工全文评价 `PENDING_AUTHOR`，文学质量 `NOT_EVALUATED`。工程短卷、检索金标准和完整流程不能替代真实模型及人工文学验收。

相关记录：[性能与容量修复对照](性能与容量修复对照.md)、[PR 合并与 main 验收记录](PR合并与main验收记录.md)、[未通过目标与后续工作](未通过目标与后续工作.md)。
