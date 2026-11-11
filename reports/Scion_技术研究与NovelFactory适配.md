# Scion 技术研究与 NovelFactory 适配

> 核查日期：2026-11-11（Asia/Shanghai）。源码基线：`23a61398278c9a13e6450d444d4a6d25a88ef5e0`。本文的源码判断来自实际阅读；性能收益只有在本轮对照实验中测得后才成立。本文不是 Scion 复现，也不改变 NovelFactory 的权威世界协议。

## 1. 结论与证据边界

NovelFactory 已具备服务端候选对象、内容哈希、固定 WorldRef、事件溯源、Scope 守卫及 PostgreSQL 原子接受。最适合借鉴的方向是让**已有协议的计算和读取尽量与实际变化对应**。当前最明显的结构性缺口是：小提案仍读取并重放完整选中历史；接受事务仍复制完整 Revision 投影和事件选择；通用 Repository 取整行，使只需少量字段的调用也可能支付 JSONB 解码成本。这些判断由第 4 节的调用路径支持，不依赖 Scion 的知名度。

官方文章真实存在，发布于 2026-10-13，2026-10-15 增补了先行技术归属。其适用背景及设计原则、数据结构、缓存、测试和审查部分均已阅读。本文不引用该系统的吞吐、缓存命中、资源缩减或开发速度数字作为 NovelFactory 的性能依据。[SC01]

本文使用四类标签：

| 标签 | 含义 | 可以据此做什么 |
|---|---|---|
| `primary_checked` | 已读取的官方说明、原论文或作者源码 | 确认该来源公开了相应机制；不能证明本项目已实现 |
| `code_checked` | 已检查指定 SHA 的实际实现 | 描述当前调用与复杂度；不能替代测量 |
| `proposed` | 本项目根据约束推导的方案 | 进入隔离原型和反例测试 |
| `project_measured` | 有本轮原始输出及环境记录的结果 | 只在对应实验范围内评价收益与正确性 |

本报告的研究结论为前两类。新路径的推广条件将在后续的《新旧世界内核_等价性验证.md》《Scion思路_并发优化实验.md》和《并发优化_集成与PR审核.md》中评估。

## 2. 十二项官方设计与项目映射

“官方事实”列为对 [SC01] 的简要事实索引；后两列是依据本项目源码提出的独立分析，不是 Anthropic 对 NovelFactory 的建议。

| 主题 | 官方事实索引（`primary_checked`） | NovelFactory 对应位置（`code_checked`） | 本项目适配及边界（`proposed`） |
|---|---|---|---|
| Always-online clients | Online clients fetch on demand. | `interfaces/http.py`；`application/agent_gateway.py` | Agent 经 API 读取获准切面，无需复制整个世界。连接中断后按幂等键查回执，不能假定未执行。 |
| Client-server asymmetry | Servers perform repository computations. | `Kernel.propose/prepare/accept`；`WorldQueryService` | 规则与验证继续集中在服务器。客户端可生成行动意图，但不能自造证书、状态投影或可信依赖。 |
| Server-side Changeset | Changesets record parent-relative file changes. | `Kernel.propose`；`WorldChangeSet`；`proposals` | 当前已经有事件候选与操作列表。先优化其生成成本，而非再建一套提交格式。候选的输入版本不能静默改成新的 HEAD。 |
| Incremental, lock-free creation | Creation cost follows the change; refs are separate. | `propose` 最后的对象登记事务；`prepare` 的证书登记事务 | 将候选对象准备与权威接受分开分析。移除这些事务中的分支锁前，要证明幂等、任务 fencing、权限撤销与 GC 协调都有替代保护；本轮不因标题“无锁”直接删锁。 |
| Content-addressed immutable objects | Stored objects are immutable and content-addressed. | `_blob/_register_artifact/artifact`；`domain/canonical.py` | 原始内容哈希可用于复用；权限不是哈希的属性。每次读取仍验证当前 grant、任务代际及知识切面。 |
| Incremental derived data | Derived values recursively use parent values. | `DerivedWorldWorker`；`revisions.snapshot` | 对有证明的追加事件增量更新投影。Retcon、解释版本变更和无法证明的顺序必须回退完整重建。 |
| Sparse / virtualized reads | Clients request only required trees and blobs. | `_sample`、`_state`、`_graph`、`context_compile` | 首先只投影需要的 Revision 列；未来再按实体/关系块读取。STATE 已避免加载事件正文，不能将既有修复重新计作本轮成果。 |
| Tiered caching | Immutable objects pass through bounded cache tiers. | 查询结果 LRU；genesis 初始投影 memo；对象存储 | 优先使用有界进程内缓存；本轮不新增 Redis 或缓存服务。监测命中率、占用及回源峰值，缓存命中不得跳过授权。 |
| Minimal mutable refs | Development avoids unnecessary shared refs. | `works.canon_branch_id`；`branches.head_revision_id` | 一个 Agent 的任务与提案不需要一个可变世界分支。正式、模拟、修复分支仍按业务语义存在，不能合并成不受审计的私有真相。 |
| Commit / Ref separation | Ref movement uses a separate Spanner-row CAS. | 候选 `proposals` 与正式 `revisions/commits/branches` | 对应的是“候选可并行，正式接受仍线性化”。继续在 PostgreSQL 事务内同时更新事件、投影、Scope、HEAD、回执与 Outbox。 |
| Server-side APIs | Repository operations are exposed through APIs. | `query_batch`；固定 `ResolvedReadCut`；Gateway | 优先复用现有同切面批读。多个 Agent 的批请求必须逐项授权，不可合并秘密上下文或共享可变 Session。 |
| Testing / review / consistency | Simulation, fuzzing and human review accompany scaling. | 反例、PostgreSQL 集成、故障钩子、CI | 用旧路径作语义参照，覆盖并发与失败。世界因果约束必须在接受前证明；不引入合并后发现冲突再由 Agent 修补世界的机制。 |

这里最关键的区分是**候选身份和正式位置**。一个候选对象可以在固定父版本上独立形成，但其是否成为世界真相，取决于当前授权、任务代际、证书、依赖、时间覆盖和原子事务。两个候选都合法，不意味着二者能够同时接受。例如两个角色各自拿走同一物品的提案，不能靠内容哈希不同而绕过冲突裁决。

## 3. 补充技术：支持哪些推导，不能支持哪些推导

### 3.1 Piper 与 Sapling / Mononoke / Bonsai

Google 的资料描述了集中式 Piper 与按需访问的开发环境；这支持将大型数据集的访问协议和数据存放方式分别设计，而不是要求所有参与者持有全量副本。[SC02] NovelFactory 的具体选择仍是现有单一 PostgreSQL 权威库及对象存储，未从该资料推出迁移到 Google 基础设施的必要性。

Sapling 的官方说明强调其在企业在线、单一主干环境中的设计取向。Mononoke 是其服务端，公开仓库同时列明开源构建与内部部署存在差异。[SC03][SC04] 因而本轮借鉴数据模型和协议思路，不把读取公开源码等同于获得完整可部署的内部系统。

已通过 GitHub 连接器读取 Meta 的固定提交 `79071c2a3ea20c36ed71332608535e5801101bba`：

| 原始源码 | 已核查结构 | 对本项目的独立推导 |
|---|---|---|
| `mononoke_types/serialization/bonsai.thrift`，blob `5a532da3c3a3778ff78ad92d8cf06fa93e7c7628` | 有序 `parents`、`file_changes`、`content_id`；有序映射保证确定序列化；字段序列变化会影响哈希。[SC05] | WorldChangeSet 的身份必须绑定规范化内容与解释版本；改变序列化方式要做版本兼容，不能让“优化”改变既有事件身份。 |
| `derived_data/src/lib.rs`，blob `12946e1189dd986d746f42f767f7ab100b6df950` | `BonsaiDerivable` 从 changeset 与直接父派生值求新值；提供批派生及按 changeset ID 取值接口。[SC06] | 可以为世界投影建立 `derive(parent_projection, changeset)` 接口，但必须另外证明事件时间、知识权限、效果唯一性和 Retcon 语义。 |

以上为独立的技术源流，不将 Mononoke/Bonsai 的设计归为 Scion 首创。Anthropic 官方更新也明确指出该关联。[SC01]

### 3.2 MVCC、OCC 与依赖守卫

PostgreSQL 17 的 `READ COMMITTED` 提供语句快照；`REPEATABLE READ` 提供事务快照，但并不排除全部序列化异常。`SERIALIZABLE` 增加依赖检测并可能要求重试；其谓词锁用于识别危险依赖。[E01][SC07] 这些数据库机制不能替代跨分钟 Agent 工作期间的业务版本检查。

NovelFactory 的 OCC 应继续分成两个时间尺度：准备阶段取得固定世界与完整依赖；接受阶段在短事务内复核依赖、授权和任务 fencing。`DEPENDENCY_GUARDED` 的正确性依赖“读集完整”，而非仅仅“碰过的实体 ID 相同”。查询结果为空、某区间没有事件、某关系只允许一个持有人，也都产生必须登记的负向或谓词依赖。直接缩小 Scope 以减少拒绝，会改变被证明的命题。

### 3.3 Event Sourcing、CQRS 与增量视图

事件溯源将不可变事件与可重建读模型分开；CQRS 可以在同一数据库中使用不同的读写模型。[E24][SC08] 这与当前项目兼容，无需为“读模型”引入第二权威库。

DBToaster 原论文研究通过变化量维护查询及辅助视图。[SC09] 本项目借鉴的是增量维护的代数思路，不采用其原实验结果，也不假定任意 reducer 自动满足可增量化条件。PostgreSQL 普通物化视图文档说明其结果可能落后于底表，刷新语义也不等于本项目的增量世界投影。[SC10]

对 NovelFactory，只有满足

`FullReplay(G, H ⊕ Δ) == Incremental(Project(G, H), Δ, proof)`

且权限、时间、效果及错误行为一致的变化，才可进入增量路径。这里 `H ⊕ Δ` 表示保持原有事件选择和有效时间语义的组合，不等同于按数据库插入顺序在列表末尾追加。跨地区晚到事件可能在故事时间上位于已有事件之前；必须重排受影响计算或回退，不能直接把当前状态当作正确前置状态。

## 4. 当前真实调用链与问题证据

本节位置以基线 SHA 为准。后续改动可能移动行号，函数名和链接中的 SHA 是稳定定位依据。

### 4.1 读取与 Context Compiler

`HTTP/MCP -> WorldQueryService.query/batch/context_compile -> _sample -> Kernel.revision -> KernelRepository.one/many -> PostgreSQL -> _execute -> _delivery_check`

[`queries.py`](https://github.com/SKYdayDUCK/NovelFactory/blob/23a61398278c9a13e6450d444d4a6d25a88ef5e0/src/novel_factory/application/queries.py) 的 `_sample` 在重复读事务中固定任务、权限与 WorldRef；随后调用完整 `revision`。`Kernel.revision` 从数据库取得整行，重新对 `snapshot` 计算哈希并验证调度投影。`Repository.many` 使用 `select(table)`，不支持按字段投影。

基线已经有三项有效的局部优化，不能重复宣称：STATE 查询和 Context Compiler 不再无条件读取全部事件正文；`prepare` 中 `parent == base` 时复用已验证 Revision；`Repository.ancestor` 已改为只递归版本身份与父关系，避免载入全部 JSONB。现有问题是 STATE 虽不取 Event payload，仍加载整个 Revision 的 `selection/snapshot/frontiers/scope_versions` 等字段；结果缓存查找也发生在 `_sample` 之后，命中不能省去该采样成本。

`context_compile` 先生成权限切面内事实、来源引用和依赖，再持久化 ContextBundle 并锁定任务代际。该路径里的大部分结果包含任务身份、grant epoch、story time、recipe、输入 manifest；它们不能仅凭相同 WorldRef 跨任务直接复用。可以复用的应是受控的中间纯计算，而非已绑定的整份 ContextBundle。

### 4.2 提案

[`Kernel.propose`](https://github.com/SKYdayDUCK/NovelFactory/blob/23a61398278c9a13e6450d444d4a6d25a88ef5e0/src/novel_factory/application/kernel.py) 的实际顺序为：

1. 校验任务、grant、输入上下文和 base Revision。
2. 读取 genesis，按 `base.selection.values()` 批量取全部已选事件正文；恢复 selection 顺序。
3. `templates.build` 从 base snapshot 建立 footprint；校验调度命令和任务时间切面。
4. 生成事件候选及 WorldChangeSet；调用完整 `replay(genesis, selected + candidate)` 验证。
5. 对象先持久化；在数据库登记时重新授权、锁 branch、校验任务、登记来源/提案/审批/回执/依赖。

一次只有一个局部效果的普通提案仍处理 N 个旧事件。对象数量可能只与 Δ 相关，但提案验证不是 O(Δ)。这里“候选已经分离”与“候选已经足够便宜”是两回事。

### 4.3 证书与接受

`prepare` 在重复读事务中加载当前 Revision、验证 base 祖先关系、重新构造 footprint，并调用 `_tokens`。后者先 `scope_map` 读取分支全部 Scope，再从中挑选读写 token。证书登记事务也会锁 branch。

`accept -> _accept_transaction` 依次复核 grant、审批、branch、幂等回执、任务、证书及祖先关系。获得 branch 行锁后，它还会加载完整当前 Revision、重做模板前置条件、复制 selection/frontiers、取完整事件集合、执行完整 replay、计算快照哈希、写入新 Revision 和所有 `revision_events`，最后 CAS 更新 HEAD、登记回执/Outbox 并接受任务。

因此，分支串行区间不仅有必须串行的权威提交，也包含完整历史读取、Python reducer、序列化和投影复制。减少这些工作可能缩短锁持有，但是否已是主要瓶颈要以 SQL、锁等待、CPU 和 HTTP 业务完成数据证明。

### 4.4 重放算法与增长成本

[`domain/world.py::replay`](https://github.com/SKYdayDUCK/NovelFactory/blob/23a61398278c9a13e6450d444d4a6d25a88ef5e0/src/novel_factory/domain/world.py) 不只累加资源：它重新验证每个事件 Schema、效果身份、因果拓扑、有效边界、同时事件写槽，再按时间应用效果，最后在所有关系边界检查基数与图约束。`_apply_group` 也检查约束。

设 N 为已选事件数、R 为时态关系记录数、B 为关系边界数、S 为 Scope 数、D 为版本深度：

| 操作 | 基线需处理的数据 | 复杂度判断的边界 |
|---|---|---|
| 普通 propose | N 个旧 Event payload 与候选 | 至少有随 N 增长的读取、校验和分配；实际 reducer 可能更高 |
| prepare | 当前完整 Revision；必要时原始 Revision；S 个 Scope | 不重新 replay；仍有投影 JSONB 与哈希成本 |
| accept | N 个选择/事件/关系；完整 snapshot；S 个 Scope；N 行 `revision_events` | 单次有线性部分；持续追加时完整选择映射的累计存储至少呈二次增长趋势 |
| 历史约束 | 多个时点扫描关系并检查图 | 部分循环具有 B×R 或事件×关系的增长形态；不未经剖析声称严格整体 O(N²) |
| ancestor | 父版本元数据链 | 读取列已收缩，但仍需遍历可达链；不能仅以 branch_seq 比较替代真实血缘 |

接受代码还有 `len(selection) <= 2000` 的明确同步重放上限。当前代码不能被报告为已支持百万事件；本轮不得因为加了增量实验就擅自删除此边界或放宽原容量目标。

### 4.5 派生 Worker、Retcon 与跨卷

[`DerivedWorldWorker._material/process_claimed`](https://github.com/SKYdayDUCK/NovelFactory/blob/23a61398278c9a13e6450d444d4a6d25a88ef5e0/src/novel_factory/application/derived_worker.py) 依固定版本提取 timeline、relations、cognitions、resource/lifecycle history 与依赖影响，再更新派生状态和水位。该 Worker 不应成为 world accept 的替代写入入口。把它进一步增量化时，水位必须同时包含版本身份、解释器/投影代和成功完成状态，不能只存一个递增整数。

Retcon 走修复分支和 `REPAIR_EVENT`；接受时重新验证受影响历史，提升 history epoch，跨卷与文学记忆通过已有失效/血缘机制衔接。一个修复后的版本即使显示相同余额，也可能具有不同因果、事实来源和知识传播；只比较最终值会漏检语义差异。

### 4.6 对第一阶段十个问题的直接回答

| 问题 | 源码可以确认的回答 | 还需要实测的部分 |
|---|---|---|
| 单次世界提交处理多少历史？ | propose 和 accept 都读/验证选中历史；accept 还写整份选择及投影。 | 每类数据的精确字节数、SQL 次数与时间。 |
| 成本随历史怎样增长？ | 存在线性复制及可能的多重关系扫描；不能视为固定成本。 | 多历史规模曲线及 profiler 中各函数贡献。 |
| 哪些全 JSONB 读取过量？ | 通用整行 Repository；Revision 在读、证书、接受路径全量解码。 | 哪些列在各调用点未使用、TOAST/网络/解码分摊。 |
| 哪些依赖可增量验证？ | 模板证明的精确读写 Scope；授权/解释/canon/history token 仍需检查。 | 稀疏 Scope 查询是否减少数据库和 Python 成本。 |
| 哪些操作锁整个分支 HEAD？ | propose 登记、prepare 登记、accept，以及分支/提升管理路径。 | 各类事务锁等待和持锁时间。 |
| 哪些候选可以独立创建？ | 固定 base 的 WorldChangeSet、closure、event 候选及部分纯计算产物。 | 移除 branch 登记锁是否有完整替代约束；本轮不预设可删。 |
| 哪些投影可异步？ | 检索、文学记忆、关系展示及派生分析可带水位落后；接受所需权威约束不可滞后。 | Worker 追赶与恢复成本。 |
| 哪些缓存可安全复用？ | 不可变对象字节、带解释版本的确定计算；当前权限必须另行复核。 | 命中率、内存上界、热点回源。 |
| 事务内有哪些重复 CPU？ | accept 内 replay/hash/完整投影复制；prepare/propose 有多轮校验但其中部分属于必要竞态复核。 | 可移出/可省略的精确部分及故障反例。 |
| 客户端会不会限制负载？ | 历史基准已有进程隔离代码；从 agent 数不能推出真实到达 RPS。 | 客户端实际发出率、连接池等待、服务端到达率及各进程 CPU。 |

## 5. 建议的有限原型与证明条件

### 5.1 按字段读取及稀疏 Scope token

先提供 Repository 的显式字段选择，审计每个调用点的实际用途。若只读取 Revision 元数据，返回类型不应伪装成已经完整验证的世界状态。涉及 `snapshot` 的查询继续执行原有完整性检查；不能为了省 CPU 把旧的 `PROJECTION_CORRUPT` 检测静默绕过。

Scope 校验可按 `reads ∪ writes` 获取版本，但该集合来自服务器注册模板而非客户端自报。所有授权、审批、CANON_POINTER、INTERPRETATION、history epoch 仍沿用当前复核。对于不存在的 Scope，必须保持原版本 0、创建登记和 tombstone/代际语义，覆盖“空读后被插入”的 phantom 反例。

这一类优化不改变权威模型，是适合优先 A/B 的候选；实际收益由读取字节、decode/CPU 与业务吞吐分别判断。

### 5.2 可证明的增量重放

最小原型可仅接受满足充分条件的追加变化：可信父投影；父选择未被替换；解释器相同；新事件效果身份未出现；因果父均存在且时间合法；新有效边界不会插入需要重算的旧效果之前；同一时点冲突与全局关系约束仍被检查。条件不足时调用原完整 reducer。

“跨地区互不相关”不能仅靠地区标签判断。资源账户、物品唯一持有、跨地区消息、位置层次关系、共享规则与计划依赖都可能跨地区。若为了推广增量路径必须建立一套复杂新闭包证明，先保留回退并把覆盖率作为指标，不将所有事件强行归入 fast path。

本轮选定的计算 memo 仍读取和解码完整旧 Event JSONB，并规范化全部输入以证明检查点匹配；它只跳过可复用前缀的部分 Schema/拓扑验证及效果重放。深拷贝、最终哈希、整份落库及 `revision_events` 仍随历史增长。另一个已验证 Revision memo 候选通过每次窄元数据核对复用精确行内容，其证明条件将在《增量世界版本与状态投影_技术设计.md》中记录。性能报告必须拆开这些工作，不能把未来分块模型的收益写成本轮成果。

### 5.3 分块投影是后续设计，不是本轮预先宣布的成果

可以研究按 `(WorldRef, projection_generation, block_kind, block_key)` 寻址实体、时态关系、资源和认知块，以不可变块哈希复用未变部分；Revision 保留根清单、父身份、变化和验证证明。块必须保存时间历史及来源，不能只留最新值。

但这引入回填、根清单兼容、跨块约束、Retcon 重建、备份恢复、索引水位等新维护面。只有有限原型证明确实需要后，才做扩展式 Alembic 迁移和可恢复回填。旧数据继续由现有 Revision 全量路径读取；新代投影经过等价核验才切换。当前研究不授权生产迁移或删除旧数据。

## 6. 派生数据与缓存的一致性设计表

以下是本项目建议，不表示已全部实现。

| 对象 | 权威来源与版本身份 | 计算与失效 | 异步、水位及恢复 |
|---|---|---|---|
| 局部状态块 | 已接受事件选择、genesis、解释版本、精确 WorldRef | 父块加已证明变化；Retcon 生成新身份 | 正式验证所需块必须与事务版本一致；缺失回退全量 |
| 关系图/时态子图 | 对应 Revision 的关系及来源记录 | 按边变化维护；版本不可用时不跨版本拼接 | 可异步；返回源 WorldRef 与 generation，落后则显式说明或回退 |
| 事件血缘与祖先索引 | 不可变 Event/Revision 父关系 | 按新增边维护；分支边界保留 | 可以重建；不能用未经核验的“最大序号”证明跨枝血缘 |
| 文学记忆/未完成剧情依赖 | 接受的世界/文本/计划版本及声明依赖 | 根据失效记录重算；计划不升级为世界事实 | 可异步；交付绑定源水位，Retcon 后重审而非静默沿用 |
| Context 纯计算片段 | WorldRef、story time、knowledge cut、manifest、recipe、解释版本 | 任一语义输入不同则独立键；身份/授权绑定在外层完成 | 可复用内部值；当前 grant 与任务 fencing 不缓存替代 |
| 对象字节缓存 | 内容哈希与编码格式 | 内容不可变，按容量淘汰而非按 HEAD 清空 | 丢缓存可回源；引用前哈希验证，返回前授权复核 |

异步 Worker 必须在产物全部持久化后推进水位，失败时保留可重试任务。Outbox 至少一次交付不等于效果可重复施加；索引写入需以源版本/任务代际幂等。对象存在而数据库提交失败时，保留可回收孤儿，绝不让对象可见性替代正式接受。

## 7. 不采用的迁移与仍未证明的假设

1. 不迁移源代码托管或用 Scion 取代本仓库 GitHub；Scion 在这里是架构研究对象。
2. 不使用软件提交后自动修复冲突的思路接受世界冲突。资源、时间和知识泄露无法靠后续代码修复恢复原语义。
3. 不引入 Spanner、Redis、Kafka、图数据库，也不增加第二套 Canonical World。
4. 不按每个 Agent 建一个持续更新的 HEAD；任务、候选与正式世界版本继续保持现有边界。
5. 不把内容寻址等同于授权，或把缓存命中等同于最新世界事实。
6. 不假定局部函数加速必然提升 200/500/1000 Agent 完成率；HTTP 排队、CPU、连接池、持锁、重试和真实因果热点必须分别测量。
7. 不把默认关闭的原型、仅单元测试通过的路径或一次成功运行标成正式容量验收通过。

原型每次 Shadow Evaluation 都应比较状态、事件/效果身份、来源、Scope、WorldRef/父版本、证书判断、历史切片、Retcon、知识与错误行为。由随机 ID 或记录时间造成的差异必须用预先定义的身份映射或固定输入解释，不能粗暴删除所有“难比较字段”。任何未经解释的差异都阻断推广。

## 8. 来源与实际阅读范围

全部访问日期为 2026-11-11。沿用项目已有 E01/E24 编号，其余 SC 编号为本轮报告局部编号。本文未复制来源全文或外部源码实现。

| 编号 | 一手来源 | 版本与已读范围 |
|---|---|---|
| SC01 | [Anthropic: Redesigning source control at Anthropic](https://www.anthropic.com/research/redesigning-source-control) | 2026-10-13；页面注明 10-15 更新；完整正文及脚注，重点 Principles、Data structures、Scaling、What we learned |
| SC02 | [Google: Why Google Stores Billions of Lines of Code in a Single Repository](https://research.google/pubs/why-google-stores-billions-of-lines-of-code-in-a-single-repository/)；[Software Engineering at Google, Chapter 16](https://abseil.io/resources/swe-book/html/ch16) | 2016 论文官方元信息/摘要；Google 工程书 Piper 与在线工作环境说明；未复现其内部系统 |
| SC03 | [Sapling SCM 官方介绍](https://sapling-scm.com/docs/introduction/) | Introduction 与 Caveats；该页面本身含历史产品可用性表述，不据此推断当前所有部署能力 |
| SC04 | [Meta Mononoke README](https://github.com/meta/sapling/blob/main/eden/mononoke/README.md) | 服务端定位、公开构建支持与缺失组件列表 |
| SC05 | [Meta Bonsai 数据定义](https://github.com/meta/sapling/blob/79071c2a3ea20c36ed71332608535e5801101bba/eden/mononoke/mononoke_types/serialization/bonsai.thrift) | 固定 SHA；已读完整文件，重点 changeset、确定序列化、FileChanges、ContentId |
| SC06 | [Meta Derived Data 接口](https://github.com/meta/sapling/blob/79071c2a3ea20c36ed71332608535e5801101bba/eden/mononoke/derived_data/src/lib.rs) | 固定 SHA；模块注释、BonsaiDerivable/BonsaiDerived、批处理与fetch说明 |
| E01 | [PostgreSQL 17 Transaction Isolation](https://www.postgresql.org/docs/17/transaction-iso.html) | Read Committed、Repeatable Read、Serializable 与谓词依赖 |
| SC07 | [PostgreSQL 17 MVCC Introduction](https://www.postgresql.org/docs/17/mvcc-intro.html) | MVCC 与读写冲突的一般保证；不外推为业务无锁 |
| E24 | [Microsoft Event Sourcing pattern](https://learn.microsoft.com/en-us/azure/architecture/patterns/event-sourcing) | 权威事件、重建、投影与适用边界 |
| SC08 | [Microsoft CQRS pattern](https://learn.microsoft.com/en-us/azure/architecture/patterns/cqrs) | Separate models in a single data store；读写职责与同步限制 |
| SC09 | [Ahmad et al., DBToaster, PVLDB 2012](https://www.vldb.org/pvldb/vol5/p968_yanifahmad_vldb2012.pdf) | 摘要、Introduction、viewlet transforms 与高阶变化量的机制说明；未复现性能 |
| SC10 | [PostgreSQL 17 Materialized Views](https://www.postgresql.org/docs/17/rules-materializedviews.html) | 结果持久化、刷新和可能陈旧的语义 |

性能数值、完整 A/B 环境和最终集成决定见本轮对应报告，不从外部系统的作者自述折算。
