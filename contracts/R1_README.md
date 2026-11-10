# R1 契约、兼容与服务端核验

文档包版本为 **v2.0-R1**；新增机器契约使用 **schema_version=2.1**。原 [core.schema.json](core.schema.json) 与 [literary.schema.json](literary.schema.json) 保持原字节内容。新增类型集中于 [temporal_concurrency.schema.json](temporal_concurrency.schema.json)，复用原WorldRef、ArtifactRef、事件身份、查询状态及错误信封；没有新增另一套世界数据库。

[R1示例索引](examples/r1_index.json)给出28个正例和19个反例。示例是独立结构片段，UUID、产物hash及证书身份为合成引用，不能作为可直接导入的合法数据库种子。Schema只证明结构；实际引用、授权、历史、证明与原子性仍由服务端验证。

## 1. 契约总览

| 类型 | 输入/输出职责 | 实施阶段 |
|---|---|---|
| BranchSelector / RevisionSelector / ReadContract | 分支或canon解析；FIXED、LATEST_AT_START、READ_YOUR_WRITES；独立求值和索引策略 | P1 |
| SubjectTime / KnowledgeCut / ReaderCut | 所问故事时间、角色知情时间、计划/文学清单、读者编排位置 | P1/P2 |
| ResolvedReadCut | 一次解析所得WorldRef、视图、源清单、权限epoch与解释版本 | P1 |
| UnifiedQueryRequest / Result / QueryPayload | 同一语法访问状态、时间条目、计划依赖及有界邻域 | P1 |
| QueryBatchRequest / Result | 一次pin的只读批量，逐项返回成功或安全错误 | P1 |
| TemporalEntry / TemporalSource | 事件、认知、计划和正文来源的只读归一化条目 | P1/P2 |
| RelationValue / RelationOrigin / ValidityInterval / TemporalRelationRecord | 时态关系/标量状态的类型化只读形状和出处 | P1 |
| TypedStateAnswer / StateQueryItem | 每个主体/谓词分别表达确定性、认知、求值、覆盖与值，避免一组结果共用一个null | P1 |
| RelationTransition / AcceptedEventR1 / WorldChangeSetR1 | 新版多关系与标量状态效果，沿用统一发生身份、选择与接受协议 | P1 |
| ContextCompileRequest / CompiledContext / Result | 固定recipe和源切面，批量装配获准上下文 | P1/P2 |
| ScopeToken / ApprovalBinding / AcceptanceCertificate | 服务端依赖范围、批准适用性和重验证资格 | P1有限模板 |
| AcceptancePrepareRequest / GuardedAcceptRequest / GuardedCommitReceipt | 准备与正式接受分开；保存原base、validation、actual parent和新修订 | P1有限模板 |

## 2. 统一时间查询

`SubjectTime`是需要查询的点或有界半开区间。`KnowledgeCut.known_at`表示角色当时已经获知什么，不等于客观事件发生时间。PLAN固定`plan_manifest_ref`；READER固定叙事修订、编排版本与ordinal；LITERARY固定文学清单。角色、manifest和时间字段只是请求，真实范围来自当前grant。

`TemporalEntry.source_story_time`表示条目在故事中发生/形成的时间，`target_time`表示意图、预测、承诺或计划指向的时间，`valid_time`表示状态效力区间。源记录的系统`recorded_at`另由审计读取。例：D29形成D30夺剑意图，只证明意图存在，不能填一条D30成功事实。作者计划通常没有故事内形成时间，source_story_time可为空。

`time_axis=SOURCE/TARGET/VALID/RELEVANT`选择时间过滤轴；RELEVANT根据条目类型采用规范轴并保留完整时间字段。尚未解析的相对/模糊约束不得被悄悄排序成精确先后。统一时间线是服务视图，不是把所有来源写进同一张可变event表。

| ReadContract组合 | 合法含义 | 失败/降级 |
|---|---|---|
| FIXED＋REQUIRE_EVALUATED | 查询确切旧修订的已求值范围 | 该修订未推进则拒绝确定性用途；不换成当前HEAD |
| FIXED＋ALLOW_PARTIAL | 保留未求值、未知和确定部分 | 每项状态明确；空值不伪装不存在 |
| LATEST_AT_START | 在短一致快照解析一次HEAD/清单，后续使用固定cut | 中途HEAD变化不把同一响应拼成混合版本 |
| READ_YOUR_WRITES | 读取回执所在分支该修订或后代 | canon推广至不同历史时返回CANON_MOVED等明确错误；不静默遗失自己的写入 |
| 动态选择＋WAIT_EXISTING_ADVANCE | 有界等待已经授权的推进任务形成所需新修订 | 不启动模拟；超时返回顶层PENDING及现有任务引用 |
| ALLOW_STALE_SEARCH | 普通时间线/邻域候选召回允许显式陈旧 | 不适用于精确STATE、正式裁决、资源/否定前提或提交资格 |

顶层`PENDING`表示请求正等待已有推进；`READY`中的某个`answer.evaluation_status=PENDING`则表示已返回固定cut下的未求值知识，两者不等价。固定修订允许等它自己的物理投影，但不能等待未来事件加入该修订。

STATE的payload为`{kind: STATE, state_items: [...]}`，每项含subject_id、predicate和TypedStateAnswer。数据值限定为类型化实体/标量、关系记录数组、资源余额或null；状态组合沿用core规则。图结果带coverage/truncated，top-k或分页的空集不能证明世界中不存在路径或对象。

一次普通`query_batch`只有一个KnowledgeCut，最多32项，并另有总扫描/边数/响应字节和成本限制。每项同时检查操作能力与平面能力；TIMELINE(PLAN)也要求PLAN_READ，compiler辅助PLAN同理，不能只检查PLAN_DEPENDENTS。外部Agent不能把NEIGHBORHOOD塞进batch绕过GRAPH_READ。多平面组合只由获准ContextCompiler recipe执行，主/辅助cut分别授权且共享世界切面。

## 3. 动态关系与旧契约兼容

旧2.0 `RELATION_SET`及其他效果继续可读。2.1 `RelationTransition`支持注册谓词、实体/字符串/小数/布尔对象和OPEN/CLOSE：OPEN使用新relation_id，CLOSE核对当前选择的关系版本，关闭后重新成立使用新的关系发生身份。人物vital_state、宗门member_of、地区controlled_by等通过注册领域规则解释，不把所有状态塞进物品ACTIVE/DESTROYED枚举。

`at`仅表示已经确定的效力边界；可能与发生时间不同，但必须有规则/证据支持。不能从BOUNDED/UNSPECIFIED随便选一个端点。初始正式关系可以来源于已选择的设置记录，运行期变化必须有合法事件效果。TemporalRelationRecord.origin允许EVENT或RECORD，不为满足字段而伪造创世事件；OPEN/CLOSE及认知来源链在证据和依赖中保留。

旧RELATION_SET与新transition归一到同一投影和谓词约束，不对同一变化重复施加两次。`record_operations ASSERTION`保持原有正式设定/证据用途，不能直接改写运行中的权属、库存或生命状态。候选载荷出现不等于成为分支有效历史。

旧`commit_request`仍为strict；R1 `commit_guarded_request`支持STRICT_HEAD和DEPENDENCY_GUARDED，并最终调用同一正式写入器。旧writer也必须执行相同scope bump、任务fencing和失效协议，不能留下未登记的旁路。

## 4. 服务端必须核验的绑定矩阵

以下条件跨对象或依赖可信执行记录，JSON Schema不能替代。

| 核验项 | 必须满足 |
|---|---|
| 查询结果 | 每个StateQueryItem的subject/predicate与请求对应且无遗漏/重复；answer.world_ref/plane及故事点/区间与read_cut和该子查询一致；图边/时间条目也不得跨cut混装 |
| 原始输入 | request.base_world_ref = certificate.base_world_ref = proposal.input_world_ref = 当代TaskSpec/ContextBundle.world_ref |
| 身份与内容 | request/certificate/可信签发记录的task、generation、context、proposal_ref/hash、mode、审批集合和原依赖指纹相同 |
| 祖先链 | base ≤ validated ≤ actual parent均为同一分支、同一保留历史epoch中的真实祖先关系；不是UUID排序或跨分支branch_seq比较 |
| 原清单 | certificate.dependency_manifest_ref/fingerprint始终指原输入清单；其world_ref与scope版本不能被覆盖为新值 |
| 当前采样 | validated_world_ref、解释/活动选择/权限和scope token在同一有界一致只读快照采样；不能验证旧状态后再刷新成新token |
| 重验证 | 新token存于certificate读写集合；可信验证记录证明原查询结果和前提仍相容。实际输入变化需要新上下文/提案/审核，不能只签新token |
| 完整性 | 注册模板必须覆盖正向读、负向范围、写集合、跨实体约束、过程和冲突组；COMPLETE_FOR_TEMPLATE不是Agent自报资格 |
| 解释版本 | interpretation_ref绑定reducer、projector、谓词和历法映射版本；INTERPRETATION守卫变化阻断旧证书，旧历史不得被静默换解释 |
| 活动产物 | 固定manifest允许查询旧版本；要求“仍当前采用”的命令另绑定ARTIFACT_SELECTION并在选择变化事务中bump |
| 批准 | 精确内容hash与依赖匹配；旧批准默认STRICT_BASE，显式DEPENDENCY_STABLE政策才允许跨无关HEAD。提交时复验当前批准资格/撤销状态 |
| 调度 | 封闭意图集合、时间/通道覆盖、随机/语义裁决结果均保持适用；抢数据库锁不能决定角色胜负 |
| 最终接受 | 在统一锁序内重新检查当前grant、代际、history/interpretation、scope和约束；scope创建/删除有父注册守卫及tombstone防ABA |
| 幂等 | 同key不同payload拒绝；原成功回执可在当前读取授权下重取；换certificate需新key，提案/目标接受唯一性仍阻止重复效果 |

原批准条件若要求所读计划/编排仍被采用，而只固定ArtifactRef却漏掉活动选择scope，依然属于依赖不完整。反之，纯历史查询不能因为当前计划改变就把旧manifest视为不存在。

证书文件可以由对象存储保存，但正式使用必须回到服务端签发记录，核对certificate_id及内容hash、实际验证输入和执行身份。上传一个同形JSON、模型写“PASS”或读到ArtifactRef，均不构成证书。受限角色只能取得不透明依赖句柄，不读内部scope_key来推测隐藏人物或区域。

## 5. ContextCompiler交接与授权

TaskSpec同代world_ref固定。编译动态选择到不同修订时，返回TASK_REBIND_REQUIRED，控制面显式创建新输入代际，再以FIXED编译。`context_bundle_id=null`仅表示尚未构建上下文，不允许偷改输入。

CompiledContext包装原core ContextBundle并记录解释、recipe、源manifest、主/辅助KnowledgeCut和已授权section；不会把旧核心对象直接增加字段。ACTOR结构上禁止PLAN/WORLD段，服务端还核查所有正文、证据、错误、计数及缓存的来源与时点。其他recipe同样受[query_profiles](../runtime/query_profiles.json)与当前grant求交，角色名不是权限。

临时快照只用于一致采样和有限结构查询，长文本读取/装配及外部模型在事务外执行。硬材料超预算必须拆任务、提高获准预算或返回受阻状态；不能静默删掉约束。解释版本、known_at、计划/叙事清单和权限epoch参与缓存键；返回缓存前重新授权。

## 6. 错误与验收

新增安全错误码可通过原ErrorEnvelope表达：TASK_REBIND_REQUIRED、CANON_MOVED、PROCESS_NOT_EVALUATED、CONSISTENCY_UNAVAILABLE、UNPROVEN_FOOTPRINT、DEPENDENCY_CONFLICT、STALE_CERTIFICATE、CAPACITY_LIMIT、DEADLINE_EXCEEDED。`retryable`和recovery按实际失败确定，不能看到冲突就重新跑一次角色模型。

范围版本变化但候选仍可能相容：由AcceptanceService定向验证；真实世界前提变化：重新裁决/规划；纯DB可重试错误：回滚整个短事务并限次重试同业务命令；外部回执UNKNOWN：先对账；越权或取消：停止当前资格。跨分支采用、Retcon与无法证明完整的语义命令保持strict。

真实服务验收见[14](../docs/14_高并发服务与一致性协议.md)、[15](../docs/15_并发压测与端到端验收.md)和[A–H矩阵](../validation/r1_case_matrix.json)。本包正反例、有限模型与算术负载计算不能替代真实PostgreSQL并发、Host或小说创作验收。
