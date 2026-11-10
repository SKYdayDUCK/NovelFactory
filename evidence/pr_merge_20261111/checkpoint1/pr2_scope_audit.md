# PR #2 合并范围、安全、生成产物与迁移审计

审计基线：`main` 的 P1 提交 `721f155c9cc912cddfdae559afe4f2516d0967f2` → PR #2 `e0b0f2b080f3225d8be8e202e13258bc480f5b84`。当前工作树 `/workspace/scratch/f6d7dc8c770b/NovelFactory` 进入审计时由根 Agent 确认全部2419个远程blob匹配。本文为静态审计，不声明新跑 PostgreSQL、构建或负载测试。未删除任何文件、未进行远程写入。

## 1. 总结

范围符合 **P2完整工程链路＋已存在的有限P3-01/P3-03基础**，没有发现 PR #3 高级宏微替换、跨卷、异步派生工作者或在线备份/PITR源码混入。未发现实际凭据、数据库文件、私有材料。大量差异来自可追溯历史证据，建议本轮保留，不因缩小PR而破坏完整审计包。迁移仅增量增加3张不可变协调表，已存在真实数据库旧结构→新结构测试；精确旧main代码种子→当前代码兼容性可额外补一项作为最终集成证据。

**本结论不构成合并PASS。原50/200容量门槛失败仍是阻断，必须由根任务实际修复与重测。**

## 2. 2112项差异的分类

新增/修改文件当前字节总和为22,581,731（约22.58MB），不是Git压缩后的增量大小。静态逐文件索引和SHA-256在同目录 `pr2_scope_inventory.json`。

| 类别 | 文件数 | 当前字节数 | 处置 |
|---|---:|---:|---|
| evidence/p2 | 1925 | 13,849,179 | 历史成功/失败、短卷完整产物、原始负载，保留 |
| evidence/p3 | 57 | 5,753,090 | 有限Context/记忆评测原始证据，保留 |
| evidence/p3_p4 | 5 | 65,859 | 名称虽含P3/P4，实际是CP1接管/有限性能证据，保留 |
| evidence/recovery_20261110 | 12 | 171,709 | 恢复交付证据，保留 |
| src | 31 | 1,317,519 | 正式源码/生成Schema bundle，必须保留 |
| benchmarks | 16 | 764,633 | 复现脚本、合成语料和金标准，必须保留 |
| tests | 30 | 280,531 | 新增29项文件、1项原测试增补，必须保留 |
| contracts | 7 | 159,090 | P2契约增量，必须保留 |
| migrations | 2 | 3,556 | 0003冻结DDL和执行器，必须保留 |
| 其他工作流、文档、报告、脚本 | 27 | 216,565 | 保留入口与复现链 |

证据共1999项，占文件数量94.65%、当前字节87.86%。短卷历史包约7.04MB，P2原始负载约4.50MB，单个Context配对结果约3.58MB。精确相同内容有118组、388个额外副本，总冗余约634,732字节；许多重复是不同运行/产物ID的相同正文或角色快照，不能仅凭内容相同抹掉其运行身份与索引关系。

### 长期保留策略

- 必须保留：源码、测试、Schema及生成bundle、冻结迁移、锁文件、固定输入语料/金标准、可复现脚本、验收矩阵、失败原因、提交/CI索引、原始文件哈希及关键历史基线。
- 新运行的大型逐请求日志、JUnit全文、周期采样、完整生成产物适合Actions Artifacts；现工作流已使用 `if: always()`，确保失败亦上传，保留90天。
- **90天Artifacts不是永久备份。** 本轮未验证有覆盖现有Git内所有原始材料的永久外部档案，不能据此删文件。根任务如未来整理，应逐运行归档，保留提交/路径/源文件SHA-256/归档SHA-256/恢复命令，实际下载并校验一次，且保留失败运行与运行内完整索引关系。已发布旧commit不可重写。
- 本轮所有原始证据保留在当前Git及其历史；不增加仅为“变小”而做的删除提交。后续可把新证据的存放规则改善，不需要先清理原证据才修性能。

## 3. 实际范围与内核兼容

P2源码包括：planning、simulation、agent_gateway/runtime_tasks、narrative、editorial、release、short_volume与fixture/extraction，复用原kernel、task、grant、artifact、receipt和事务机制。新增SQL仅simulation interaction/intent/outcome的封闭候选结构，世界真相仍在不可变revision及已选事件中。

有限P3范围：上下文依赖登记批量化、point_key纯值缓存、元数据血缘查询、配对剖析/独立服务进程测量、确定性中文记忆检索。它们已在前轮授权与历史报告中声明，不能把PR #2写成“完全不含P3”。`benchmarks/p3_context_comparison.py`及工作流名 `p3-p4-evidence.yml` 不等于已纳入高级P3/P4业务。

PR #2未包含这些PR #3功能入口：`continuity.py`、`refinement.py`、`derived_worker.py`、`online_backup.py`、`pitr_drill.py`、`runtime_upgrades`。离线评测完整foundation也留在PR #3。本轮不把这些功能重新实现或搬回PR #2。

原P1测试没有删除或降阈值；唯一修改的原测试文件是 `tests/reliability/test_recovery.py`，增加角色/技能备份保全验证与wheel资源定位目录，不删除原授权profile断言。原核心2.0/literary契约和P1的0001/0002迁移未改。正式完整测试与实际CI仍由根任务对将提交的SHA重新确认。

## 4. 安全与运行时文件扫描

对差异文件做路径/内容扫描，并对全跟踪文件检查JSON敏感字段、Bearer字面量及数据库URL：

- 0个二进制文件；0个数据库/SQLite/dump/备份包/私钥/`.env`/`.pgpass`类型路径。
- 未发现高置信GitHub token、OpenAI样式密钥、AWS访问密钥、PEM私钥或长字面Bearer token。
- 未发现带签名/秘密查询参数的外链；出现的数据库URL均为loopback开发/CI目标。
- JSON扫描的唯一非空 `secret` 字段来自原契约故意无效示例 `unknown_leaks_data.json`；不是凭据。
- 证据中出现Authorization/Bearer字样来自源码快照、角色契约和CI日志的配置说明/遮罩，不是运行时token原文。原始负载仅留请求/回执哈希与身份ID；模型环境探针只描述可用性。
- 未发现新增文件中的个人邮箱标记或私有文档内容。历史运行包含临时工作路径、机器型号和合成作品内容，属于明确的工程证据。

本扫描是有限静态模式与结构核验，不是“任何秘密都绝无可能存在”的证明。未打印疑似秘密原文。无已确认安全阻断。

## 5. P1→0003迁移与恢复

`migrations/versions/0003_p2_simulation.py` 以0002为父，只执行冻结JSON中的6条DDL：创建3张表及其3个不可变触发器；不UPDATE既有P1表、不回填/重算历史，不移除约束。冻结JSON的SHA-256实核为 `c5e30e7d3169079d3298ab8cf2ef4e091f253d9fb3920dff69dce4e70f73c651`，与执行器一致。

新表外键绑定真实work/branch/revision、task/generation及grant；新表一并加入现有metadata和IMMUTABLE_TABLES。Alembic入口要求显式数据库URL、真实PostgreSQL17，使用单次事务迁移，不自动转向生产库。DDL可能取得被引用表的短锁，不能将隔离小数据测试外推为生产零停顿迁移。

已有 `tests/integration/test_p2_upgrade.py`：创建隔离数据库，先升0002，用内核形成真实已接受世界与回执，逐表快照后升head；检查所有原表摘要不变、新3表为空、原accept请求仍得到原回执、HEAD不变。历史 `evidence/p2/batch1/upgrade.xml`为1 passed；全套回归也包含此测试。

该测试使用**当前源码**在0002结构构造旧世界，未直接运行main721旧源码，因此证明边界是旧数据库结构兼容，不完全等同“旧发布应用写入的所有历史都兼容”。建议根任务在隔离空间以实际721代码建立一个已接受P1世界，再以当前修复代码升级并核验世界/事件/回执/旧查询；这是明确剩余风险的有限补验，不需要全量历史反复测试。

0003 `downgrade()` 明确拒绝删除封闭输入/候选。回退应保存新增数据并用兼容旧代码或隔离备份恢复，不能声称 `alembic downgrade` 可无损回退。P2逻辑备份增加完整runtime角色/技能版本，并兼容旧asset manifest v1读取；恢复后还需匹配实际解释/软件包，不能把验证备份当成跨版本任意恢复许可。

## 6. 构建与依赖静态检查

- `uv.lock`相对P1未变，源码新增外部imports均已在pyproject运行依赖中。模型HTTP适配器使用标准库urllib，未将仅dev的httpx偷偷引入正式模型路径。
- wheel将 `contracts` 和完整 `runtime` 目录打包；P1只包含query_profiles的配置已扩成完整角色/技能目录。RuntimeCatalog先读wheel资源，源码方式再回退repo目录；备份服务采用同样包资源逻辑。
- 生成 `_schema_bundle.py`虽大，运行注册器直接依赖其数据，并由source hash核验机制防漂移；不是可删除的临时生成日志。
- 当前设计以仓库命令执行Alembic迁移，wheel只包含应用包与资源，不应宣传单独wheel即可脱离仓库执行全部迁移/开发脚本。独立构建/启动检查应由根任务在最终SHA上确认。
- 本审计未构建wheel或运行PG，不用静态结论冒充实际启动证据。

## 7. 本次已做的最小文档纠正

只修改 README.md、AGENTS.md开头阶段段落、pyproject.toml description：明确P2与有限P3基础、容量NOT_MET、后续P3/P4另PR；README原有410回归与过时负载数字替换成明确标注e0b历史检查点的423项及21/21、14/21。保留真实模型/人工文学未验收说明。

AGENTS从“## 1. 开始任务”起全部规范字节未变；未修改任何权限、开发自主性、审批、时间/版本/安全约束或测试门槛。版本号、依赖、锁文件和历史报告未改。`git diff --check`通过。后续根任务可根据实际新SHA结果更新当前状态，不能仅复制该历史数值作为新测量。
