# 08｜MCP、API 与本地工具接口

> v2.0-R1｜协议核查日期：2026-11-10。v2.0契约保留在[core.schema.json](../contracts/core.schema.json)，R1追加契约见[temporal_concurrency.schema.json](../contracts/temporal_concurrency.schema.json)；入口以[工具目录](../contracts/tool_catalog.json)为准。

## 1. 决策：MCP 有价值，但不是核心依赖

最小产线可由本地编排器调用外部模型 API，再由本地执行受控工具，完全不依赖 MCP。MCP 的价值在于让 Codex/Claude Code 等现有宿主用同一套受权工具读世界、交提案和提交结果。它不负责小说状态、工作流持久化、任务代际或世界事件事务。

| 接入 | 最适合 | 优点 | 成本 | 本阶段安排 |
|---|---|---|---|---|
| 内部函数 | 同一应用进程 | 最少序列化，事务清楚 | 不能直接给远端Host | 所有业务用例的基础 |
| Python SDK | 受信本地开发、评测、worker | 类型清楚，便于脚本 | 直接库调用必须经过相同授权入口 | 薄客户端/服务门面，不复制规则 |
| REST | 作者管理页、大量远端受控Agent、压测客户端 | 无状态请求、批量读、任务和认证容易对齐 | 需网络配置和鉴权 | P1薄HTTP适配；异步用例显式返回操作/任务句柄 |
| MCP stdio | 本机外部Agent宿主 | 易接入宿主，启动简单 | 进程权限需约束；模型会话与请求身份隔离 | P1适配一种真实Host，不按角色数启动同数服务 |
| MCP HTTP | 远程Agent与多人宿主 | 跨机器可达，可复用无状态应用服务 | 协议/认证/来源检查更多 | R1保留远端正式契约，P2至少核验目标Host；容量另压测 |
| 任务包导出/回传 | 暂无可达工具接口的云端Host | 可离线审阅，易诊断 | 不能伪造实时工具交互；需回传核验 | 可作为受控过渡路径 |

云端Host无法凭 `localhost` 访问作者电脑。P1推荐“本地编排器→外部模型API”或“本地Host→stdio”中先选一条，记录真实兼容结果；无需为验证小说生产先开放公网数据库。

## 2. MCP 协议profile

| profile | 采用边界 | 必须核验 |
|---|---|---|
| `legacy_2025_11_25_stdio` | MVP互操作候选，固定官方SDK及宿主共同支持的2025-11-25版本 | 初始化协商、tools/list、tools/call、结构化结果/错误、进程退出/取消 |
| `modern_2026_07_28_stdio` | 目标Host仅支持新语义时单独做适配试验 | 每请求协议元数据、消息模式、取消；不得保留旧握手假设 |
| `modern_2026_07_28_http` | R1远程接入候选 | 每请求POST/响应流、body/header匹配、OAuth等当前规范要求 |

这是待互操作验证的配置选择，不保证当前任意Host都兼容旧profile。协议版本未测不能仅修改字符串标为支持。2026-07-28的transport是每请求元数据模型，旧版连接初始化/session假设不能直接迁移。参见[旧版生命周期](https://modelcontextprotocol.io/specification/2025-11-25/basic/lifecycle)、[新版transport](https://modelcontextprotocol.io/specification/2026-07-28/basic/transports)、[新版stdio](https://modelcontextprotocol.io/specification/2026-07-28/basic/transports/stdio)。

MCP 2026-07-28的连接/stdio进程不是会话身份，同一连接允许交错多个任务；每请求独立提供协议元数据并经业务认证，应用不能从前一个请求继承actor或权限。[基础规范](https://modelcontextprotocol.io/specification/2026-07-28/basic)

MCP JSON-RPC请求ID只关联传输请求，不是业务幂等键、task_id或effect_id。断开传输/取消请求也不等于业务任务已取消；调用 `task_cancel` 等业务命令撤销 generation 后才失去接受资格。协议SDK负责握手/帧，核心服务只接收已校验 DTO 和 Principal。

## 3. 统一调用封装

所有读请求至少具有当前任务身份/授权或已认证作者会话；已编译任务还核对上下文归属。`context_compile/task_get/task_cancel`不能依赖尚未生成的上下文。所有产生持久业务变化的命令需要 `idempotency_key`，重试不得改变原业务内容。世界写提案绑定完整 WorldRef、StoryPoint/interval、依赖指纹与产物哈希。task_id/generation 不由客户端任意创造；服务器根据任务令牌找到其授权，并校验客户端回传值与持久记录一致。

```json
{
  "task_id": "10000000-0000-4000-8000-000000000001",
  "generation": 3,
  "context_bundle_id": "10000000-0000-4000-8000-000000000002",
  "world_ref": {
    "work_id": "10000000-0000-4000-8000-000000000003",
    "branch_id": "10000000-0000-4000-8000-000000000004",
    "world_revision_id": "10000000-0000-4000-8000-000000000005"
  },
  "idempotency_key": "task-3-proposal-1"
}
```

正文内容、自由理由和研究材料都视为数据，即使其中写有“管理员命令”也不改变权限。运行时不暴露 execute_sql、任意shell、文件路径遍历、直接修改canon指针或通用任意表CRUD工具。

### 3.1 AnswerEnvelope

返回 `data`、`world_ref`、`story_time`或`story_interval`、`plane`、`truth_status`、`evaluation_status`、`knowledge_status`、`freshness`、`coverage`、`evidence_refs` 和服务端依赖句柄。空结果不能自动证明不存在。未知状态的具体语义见[03](03_数据模型与知识图谱.md)；对角色的UNKNOWN不透露“被隐藏对象”是否存在。

大原文按有权限的片段和内容哈希返回，使用有限游标分页。游标签名绑定principal、task、world_ref、查询和权限版本，不能在另一个角色会话复用。向角色返回错误、计数、相关词、图邻居和引用标题也必须同样裁剪。

## 4. 工具能力目录

下表是外部运行时的最小有用门面；工具存在不代表所有角色可见。具体字段和访问模式见 machine-readable catalog。标记“控制面”的工具不授予普通角色/写作Agent。

| 工具 | 主要输入 → 输出 | 共享服务 | 权限/边界 |
|---|---|---|---|
| `context_get` | task/generation → ContextBundle | ContextService | 只取该任务；不能自行选全知身份 |
| `world_query` | context、typed query、故事点 → AnswerEnvelope | WorldQueryService | 类型白名单；按服务端视图裁剪 |
| `world_history` | entity、WorldRef、时间区间 → 有证据历史 | WorldQueryService | 禁止跨作品、分支和未来越权 |
| `graph_query` | 起点、关系类型、跳数/范围 → 路径/证据 | WorldQueryService | 有界遍历；先过滤权限再推导 |
| `memory_search` | task、目标、范围 → 摘要/原文候选 | MemoryService | 摘要不升级为世界事实 |
| `evidence_read` | ArtifactRef、片段 → 经校验原文 | ContextService | 片段权限、hash校验、不可任意文件访问 |
| `plan_get` | plan ref → 精确计划版本 | PlanningService | 角色默认不可读作者计划 |
| `plan_propose` | 合同/前提/备选方案 → 计划候选 | PlanningService | 不接受其未来为历史 |
| `action_propose` | actor任务、意图、目标、期望时段 → action proposal | AdjudicationService | actor来自任务授权；成功结果不能由角色写定 |
| `advance_prepare` | scope、目标故事点 → 推进候选ArtifactRef（内含ready/blocked） | WorldSchedulerService | 控制面/导演任务；不自动裁决 |
| `adjudication_propose` | bundle、规则、语义后果 → 裁决候选 | AdjudicationService | 独立裁决任务；通过硬规则才有接受资格 |
| `artifact_put` | 受限文本/产物 → ArtifactRef | NarrativeService | 仅上传对象；服务返回sha256，不接受自报hash为真 |
| `draft_submit` | 场景版本、ArtifactRef、event alignment → 对账候选 | NarrativeService | 不从正文自动生成正式事件 |
| `review_submit` | 精确文本/编排、issues、证据 → 审核记录 | ReviewService | 不允许自审自批高影响事实 |
| `impact_analyze` | typed change、目标引用 → 影响报告 | ImpactService | 作者/编辑范围；角色不借报告读秘密 |
| `branch_create` | base WorldRef、kind、reason → candidate/simulation branch | VersionService | 有任务授权；不允许任意切canon |
| `branch_compare` | 两个WorldRef、scope → 差异/冲突 | VersionService | 都需读权限 |
| `change_propose` | 设定/retcon/refinement差异 → proposal | VersionService/KnowledgeService | 历史修订仅准备候选 |
| `commit_request` | proposal ref、expected head、approval refs → receipt/blocked | WorldCommitService | 仅授权提交控制服务；普通Agent可请求排队审核但不能自带批准越权 |
| `release_prepare` | 世界/编排/正文/披露 → manifest候选 | ReleaseService | 不对外发表；本地接受与外部发送分开 |
| `task_get` | task_id → 安全任务状态 | TaskService | 仅本任务或授权管理范围 |
| `task_result_submit` | task/generation/attempt、产物 → 收件确认 | TaskService | 入库成功不是世界接受成功 |
| `task_cancel` | task_id、expected generation、reason → 撤销回执 | TaskService | 任务所有者/控制面；更新代际再停止执行 |

`accept_world`、`promote_candidate`、`release_accept`、备份/恢复等核心命令有Python/管理API入口，但普通运行时工具表不直接提供管理员写权。预授权策略可以让控制面自动执行常规接受，不要求作者逐条点批准；策略必须在服务端持久化并与具体内容绑定。

### 4.1 R1统一时间查询、编译和接受入口

旧23个工具及core2.0载荷保持兼容；以下新增工具采用2.1契约。扩展输入不直接塞进旧 `additionalProperties: false` 的请求。

| 工具 | 主要输入→输出 | 应用服务 | 使用边界 |
|---|---|---|---|
| `timeline_query` | ReadContract＋KnowledgeCut＋类型化状态/时间线查询→UnifiedQueryResult | WorldQueryService | 六语义平面统一查询语法；平面由任务权限约束，事实/计划/信念保留类型 |
| `query_batch` | 一个读契约/知识切面＋有界查询项→一次pin的结果集 | WorldQueryService | 每项保留状态/错误；不能混写命令或换actor/branch绕过权限 |
| `context_compile` | 已授权task＋编译recipe/读契约→持久编译结果与ContextBundle引用 | ContextService | 外部只选获准recipe；当代WorldRef固定，版本不同要求控制面换代，不自动启动世界模拟 |
| `acceptance_prepare` | 原proposal/base＋批准/依赖句柄＋concurrency_mode→AcceptanceCertificate引用 | WorldCommitService | 控制面；验证core或R1类型化变化包，不信Agent自报范围完整 |
| `commit_guarded_request` | 固定原提案、base、证书、批准＋幂等键→GuardedCommitReceipt | WorldCommitService | 控制面；支持STRICT_HEAD与DEPENDENCY_GUARDED；后者仅注册模板资格可用 |

新 `ReadContract.revision` 使用FIXED、LATEST_AT_START或READ_YOUR_WRITES；`process_policy` 与 `index_policy` 分别指定推进覆盖和检索可靠性，`max_wait_ms`只约束等待时长。KnowledgeCut独立于revision选择。FIXED必须完整WorldRef；LATEST_AT_START解析branch_selector一次；READ_YOUR_WRITES包含min_receipt_ref并校验同枝祖先关系。实际字段和判别联合以R1 Schema为准。

查询状态默认只回答已有世界；计划到时、未来条件成立或查询时间较晚均不自动运行模拟。WAIT_EXISTING_ADVANCE只能在动态selector下观察已有授权推进任务；固定旧revision可等待同版投影构建，但不能变成新世界。普通搜索允许的索引滞后不能用于角色权限、规则裁决或接受校验。[一致性协议](14_高并发服务与一致性协议.md)

`UnifiedQueryResult.status=READY` 表示查询结果已形成，不表示其中一切都是已确认WORLD事实；payload仍保留自身的语义平面、求值、知识和覆盖。PENDING包含observed_world_ref、已有advance_task_id与retry_after_ms，不伪装成最终read_cut。ContextCompileResult单独使用READY（compiled及compiled_artifact_ref）、PENDING（compilation_task_id）或BLOCKED（ErrorEnvelope）。TaskService先解析读契约并固定新输入代际，编译推荐FIXED；若动态/等待得到不同于当代TaskSpec.world_ref的版本，返回BLOCKED，code=TASK_REBIND_REQUIRED、recovery=REVALIDATE。控制面显式换代后重编，context_bundle_id尚为空不授予同代改版权。

STATE payload的state_items逐subject_id/predicate返回TypedStateAnswer，分别说明认知、求值、覆盖和值状态；不会给整批实体共用一个null。服务核验每项world_ref、plane与目标时间匹配本次read_cut和查询，禁止静默遗漏/重复后宣称完整。空关系数组、未知、未推进和已确认NONE各自遵守其状态语义。

QueryBatchRequest最多32项，每项最多500条只是格式上限，服务还限制整个批次的预计成本、图扩展节点与返回字节。READY批次包含一次固定的read_cut、逐项SUCCESS/ERROR和all_items_succeeded；PENDING不同时附带另一切面的成功项。HTTP对READY通常200、持久PENDING通常202；MCP保留类型化工具结果，不把所有非READY都改成传输错误。

ResolvedReadCut与AcceptanceCertificate均带interpretation_ref，指向服务器认可的解释版本清单；它不是允许客户端选择任意旧解释器的开关。语义升级对应INTERPRETATION守卫，纯同义索引重建不伪装成世界修改。固定plan/reader/literary清单与“当前采用清单”是不同选择：写命令需要后者时服务端加入ARTIFACT_SELECTION依赖，不能仅因ArtifactRef本身仍可读就沿用旧资格。

批量读在进入业务服务时一次解析世界和知识切面；后续每项/每页继续用这个pin。允许个别查询返回受限错误，但返回必须明确整体完整性。无权项的计数、标题、相邻实体及原因不泄漏；缓存命中照常验证当前grant。

统一工具名不扩大能力。[query_profiles](../runtime/query_profiles.json)与当前grant、实体/时间/用途限制取交集，并逐项核对操作和知识平面权限：NEIGHBORHOOD仍需GRAPH_READ，所有PLAN平面读取（包括TIMELINE与compiler辅助段）仍需PLAN_READ。不能只通过timeline_query的基本VIEW_READ就调用原本禁止的图查询或计划查询。

## 5. REST/SDK 映射与异步接口

建议版本前缀 `/api/v1`。初期固定少量资源，不把每个内部函数转成HTTP。

| HTTP | 用途 | 成功语义 |
|---|---|---|
| `POST /queries/world` | core2.0固定视图世界查询 | 200 AnswerEnvelope |
| `POST /queries/timeline` | 统一时态/状态查询 | 200 UnifiedQueryResult，返回实际固定版本 |
| `POST /queries/batch` | 同一切面的有界批量查询 | 200结果集，逐项状态及整体完整性 |
| `POST /contexts/compile` | 为已授权任务编译上下文 | 按编译用例返回固定结果或显式持久任务句柄 |
| `POST /tasks` | 已授权工作流创建任务 | 202 TaskRef；不是产物已完成 |
| `GET /tasks/{id}` | 查看任务/阻塞/结果引用 | 200当前状态；敏感字段裁剪 |
| `POST /tasks/{id}/results` | 上传一个generation的结果 | 200/201收件；重试返回同一receipt |
| `POST /proposals` | 世界/计划/修订候选 | 201候选引用，无世界效果 |
| `POST /world/accept` | core2.0 strict接受 | 200/201 CommitReceipt；精确幂等 |
| `POST /world/acceptance-certificates` | R1接受资格准备 | 201不可变证书引用；没有世界效果 |
| `POST /world/accept-guarded` | R1两模式正式接受 | 200/201 GuardedCommitReceipt；记录原base与实际parent |
| `POST /branches/{id}/promotions` | 候选切canon的特权操作 | 200经CAS切换的版本 |
| `POST /artifacts` | 存正文/证据 | 201不可变ArtifactRef |
| `POST /releases` | 接受本地发布manifest | 201 ReleaseRef；不表示网站发布 |

SDK返回相同 DTO，不将数据库连接暴露给外部 Agent。OpenAPI 在实现时从 Pydantic/路由生成，当前 Schema 是契约样本而非已经部署可调用的API。具体 endpoint 兼容变更保留版本，不用“服务还在开发”作为任意改字段理由。

## 6. 幂等、重试与错误

命令幂等键以 work/principal/command_type 作用域存储，绑定 canonical payload hash；同key同payload返回原receipt，同key不同payload返回 IDEMPOTENCY_MISMATCH。首次尝试与重试的正文、提案或批准变更必须使用新key。世界效果还需 occurrence/effect/representation 去重，不能仅靠请求去重。

| 错误码 | 默认HTTP | 可以如何继续 |
|---|---:|---|
| INVALID_ARGUMENT / SCHEMA_UNSUPPORTED | 400/422 | 修正输入或切兼容schema；不可盲重试 |
| VIEW_DENIED | 403（对存在性敏感资源采用统一不可用外观） | 使用已有授权任务；不建议换角色绕过 |
| HEAD_CONFLICT / DEPENDENCY_STALE | 409 | strict头变化需新资格；guarded只在相关守卫变化时重验证。确定性可局部重算，语义变化重裁决 |
| APPROVAL_STALE | 409 | 对准确内容与新依赖重新审核 |
| STALE_GENERATION | 409 | 保存审计后停止，旧worker不能争抢新任务 |
| ADVANCE_BLOCKED / TIME_AMBIGUOUS | 409 | 解决入站过程、缺失规则或故事顺序 |
| RESOURCE_CONFLICT / HARD_RULE_VIOLATION | 409/422 | 联合裁决或修改候选，不以技术重试重抽胜负 |
| IDEMPOTENCY_MISMATCH | 409 | 查原命令，明确新操作后用新key |
| BUDGET_EXCEEDED | 409 | 暂停派发、调整已授权预算 |
| CALL_UNKNOWN | 202 | 对账，保留预算；不宣称远端没执行 |
| RATE_LIMITED / ADMISSION_REJECTED | 429/503 | 在取DB连接前背压；按Retry-After和预算有界退避，不改变故事时刻 |
| TASK_REBIND_REQUIRED | 409 | 控制面建立新的固定WorldRef输入/代际后以FIXED编译；不修改旧TaskSpec输入 |
| CERTIFICATE_STALE / BRANCH_CHANGED | 409 | 重新准备或显式选择分支；不把旧批准和证书贴到新历史 |
| TEMPORARY_UNAVAILABLE | 503 | 受限指数退避；有明确幂等保证才重发 |

MCP业务失败用其规范的工具结果错误表示并附 ErrorEnvelope；协议格式/未知方法使用JSON-RPC错误，两层不能混淆。技术重试最大次数与退避是部署配置并有成本上限；创作不满意是新候选，不记录为网络重试。

## 7. 权限与部署

stdio进程只拿受限凭据或代理到本地服务，不继承数据库超级用户能力。全知模型会话不能轮流伪装成多个角色；角色拥有独立任务授权、上下文和缓存命名空间。传输连接可以复用，但每请求明确身份、能力和句柄，连接本身不缓存授权角色。Host能任意访问作者文件系统时，仅靠MCP过滤无法防止其读取本机其他秘密；P1须验证Host工具隔离，隔离不足改用本地编排器仅向模型发送ContextBundle。

HTTP部署使用已验证的TLS/认证/授权方案，遵循选定profile的OAuth/资源受众和来源检查要求。反向代理可信边界、origin/host校验、令牌轮换、请求大小、工具白名单和速率限制必须在上线前测试。只读权限不是“可跨作品搜索”；作品、branch、actor、用途和时间都属于授权范围。

MVP不把数据库暴露到互联网。运行时role到工具allowlist在服务端映射；Skills里的“需要工具”只是声明，不授予权限。修改role policy时递增policy_version并使旧上下文和相应批准失效。

### 7.1 无状态实例与部署预算

HTTP/MCP适配器实例不持有权威task状态或世界锁；所有实例共享同一应用语义和持久存储。进程级限流叠加作品/provider的共享预算，避免多实例各自认为仍有完整配额。短查询、编译、影响分析、正式接受和恢复使用有界资源份额，长模型调用不占数据库连接。连接池按实例总量核算，不给每个Agent常驻连接；每并发任务独立Session。

请求体、batch项数、图跳数、返回字节、工具调用时长、队列长度均设可配置上限。高负载时先保护既有回执查询、取消、恢复和短权威读，慢任务转持久队列。重试用原业务幂等键，不依赖请求落在同一实例。具体池/缓存/副本演进见 [14](14_高并发服务与一致性协议.md)。

## 8. 互操作验收

固定Host版本、SDK版本、MCP profile和权限配置，保存兼容矩阵。至少执行：读秘密的拒绝、角色ID替换、取消后回执、断连重试、相同key不同payload、长任务返回task_id、模型先后乱序、错误元数据不泄密、中文大文本分页和回传hash一致。

同一合法请求分别经内部服务、REST、MCP取得相同领域结果；传输可以不同，但版本/权限/幂等语义必须相同。MCP服务器重启不应丢失任务或推进世界。R1增加：一次batch跨多个SQL但WorldRef不漂移；同传输交错角色任务不串权限；RYW遇落后副本回主库；FIXED+未来未求值不自动推进；guarded无关HEAD变化通过、相关token变化拒绝；429/503背压下预算/队列有界。没有完成该组测试前，标记“设计支持，未完成Host验证”。

## 报告载荷与内部控制面补充

准备 Retcon 和推广使用 core.schema.json 中的 `PrepareRetconRequest/PromotionRequest`，由受权内部控制面调用，未向普通角色增加 MCP 权限。[report_templates.md](../contracts/report_templates.md) 规定修订、影响、重验证及正文对账报告的内部字段；报告 DTO 尚待 P1/P2 实现，不能用任意 JSON 或模型自报 PASS 替代。完整交接顺序见 [HANDOFF_EXERCISE](../validation/HANDOFF_EXERCISE.md)。
