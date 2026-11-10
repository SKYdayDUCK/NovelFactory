# R1 并发、查询与接受资格独立复核

> 日期：2026-11-10。范围：设计文档、机器契约及跨模块责任检查。本记录不是 PostgreSQL 运行报告，也不证明任何并发 Agent 数、吞吐或延迟已经达到。

## 1. 复核范围与结论边界

本轮逐项对照了[04 版本与接受](../docs/04_事件溯源与版本体系.md)、[06 任务与编排](../docs/06_Agent编排与管理.md)、[08 接口](../docs/08_MCP与工具接口.md)、[13 统一时间与知识](../docs/13_统一时间线与动态知识演化.md)、[14 并发协议](../docs/14_高并发服务与一致性协议.md)，并检查[05 调度](../docs/05_世界模拟与调度引擎.md)的跨分支采用规则、[核心 Schema](../contracts/core.schema.json)、[R1 Schema](../contracts/temporal_concurrency.schema.json)及[查询权限配置](../runtime/query_profiles.json)。服务级绑定应以[R1契约说明](../contracts/R1_README.md)和本文共同作为开发检查表。

修订后的主路径是：**保留短分支提交序列，通过服务器可证明完整的依赖守卫减少无关 HEAD 更新造成的返工。** 旧 2.0 接口仍严格校验 HEAD；2.1 新入口只对已登记模板提供 DEPENDENCY_GUARDED。没有把跨分支合并、Retcon 或自由语义判断一并放宽。

在下述约束全部实现的条件下，未发现该路径仍需推翻的跨模块矛盾。当前结论是“设计可以进入实现与反例验证”，不是“数据库并发实现已经正确”。本轮没有启动真实 PostgreSQL，没有运行多进程提交/崩溃/Host 互操作或 10–1000 Agent 压测。包级 Schema 检查和有限机制实验的实际运行结果由各自报告说明，不能借用本文冒充生产验证。

## 2. 发现并修正的关键缺口

| 编号 | 原风险 | 已采取的修正 | 证据位置 |
|---|---|---|---|
| R1-C01 | 原 WorldRef 和实体 token 未变，但投影器、谓词或日历解释变更；旧上下文/证书可能按新意义被继续接受 | ResolvedReadCut 与 AcceptanceCertificate 必须绑定 interpretation_ref；增加 INTERPRETATION 守卫。旧版本恢复采用原认可解释或明确兼容证明 | 04 §3.3、06 §4.1、08、14 §5.2；R1 Schema 两处 required 字段及 scope enum |
| R1-C02 | 世界版本未变，当前采用的卷纲或叙事编排已变；只检查不可变 ArtifactRef 存在会误用旧资格 | 增加 ARTIFACT_SELECTION 守卫。固定旧 manifest 的历史读取保持有效；依赖“仍被采用”的命令须守卫活动选择，并在采用事务原子 bump | 04 §3.3、06、08、14 §5.2 |
| R1-C03 | 先验证旧状态，最后采新 token，形成“旧证明＋新令牌”的 TOCTOU 漏洞 | prepare 在同一有界只读一致快照内固定 validated_world_ref、权威材料及所有守卫；长验证移到事务外；证书不刷新采样 token | 04 §3.3、14 §6.1 |
| R1-C04 | 原 DependencyManifest 指纹含原 WorldRef 与 scope_versions，却在重验证后被重新计算，破坏原 Context/Approval 绑定 | 原清单与指纹保持不变；当前核验 token 单独放证书。重签需不可变验证记录证明实际依赖仍等价，不能修改旧批准或旧输入 | 04 §3.3、14 §6.1 |
| R1-C05 | 不可变批准曾为 ACCEPTED，被误认为永久可用；或旧 strict 写入口未参加新 scope 协议 | 提交锁内复核当前批准资格；细粒度授权守卫与撤销事务互斥。strict 和 guarded 共用写入器，均原子 bump 实际影响范围 | 04 §3.2、§4.2，14 §6.2–6.3 |
| R1-C06 | ContextBundle 尚未生成时动态读取偷偷修改当代 TaskSpec.world_ref | 任务输入按代际固定；动态解析得到不同版本必须 TASK_REBIND_REQUIRED，由控制面新建输入代际再 FIXED 编译 | 06、08、14 §3；ContextCompileRequest 描述 |
| R1-C07 | 候选交互采用被误扩展为一般 guarded 合并，或普通批量响应暗中混合知识平面 | 跨分支采用继续严格目标基线/CAS；普通 query_batch 单一 KnowledgeCut；复合上下文只由授权 recipe 显式声明辅助切面 | 05 §交互采用、08、13 §4；CompiledContext 与 QueryBatchRequest |

这些修正同时进入原有模块说明和新增协议文档，不仅列在本审查文件中。它们没有改变角色自主、文学表达自由或世界/叙事分离原则；变化集中在接受资格、数据来源与查询切面的可验证性。

## 3. guarded 成立的条件

令 B 为原输入世界、V 为证书核验世界、P 为提交时实际父世界、N 为新世界。合法 guarded 接受要求同一作品、分支和历史 epoch 下满足 `B ≤ V ≤ P` 的祖先关系，再创建 `P → N`。这个次序是编辑修订的关系，不是故事中的发生先后。

允许 P 不等于 V，需要同时成立：

1. 命令属于已认可的有限模板；模板登记读、写、否定查询、约束集合、过程覆盖、调度封闭及相关控制状态。
2. 服务器从实际载荷、工具回执与规则推导完整依赖；模型声明“没有别的影响”不构成证明。
3. V 与全部守卫取自同一一致采样；证书只使用这些原令牌。不能将旧查询结果和后来采到的令牌拼接。
4. 所有合法 writer，包括旧 strict 入口、过程登记、计划采用和授权撤销，在同一原子变化中更新相应守卫。新成员出现会更新父注册范围；版本单调并保留删除痕迹。
5. 在固定锁序下读取当前守卫，且它们与证书一致；对应更新不能穿过所持锁。共享资源及工作级不变量使用额外 guard，不能仅以当前 branch 锁代替。
6. 原提案字节、输入、任务代际、证书来源、批准内容和当前批准资格全部匹配。当前解释与规则版本合法，证书未过期。
7. 调度器已经封闭真正同时交互的输入集合并选定结果；数据库提交只应用这个结果，不决定角色胜负。

在这些前提下，“P 中存在新 revision”本身不构成逻辑冲突；任何会改变该模板接受结论的相关变化则应使至少一个守卫变化，从而阻止旧证书通过。这个论证依赖模板覆盖和所有 writer 参加协议，不能由 SERIALIZABLE、JSON Schema 或一张 HEAD 表替代。

若依赖范围不完整、需要开放语义推理、发生跨分支采纳、历史替换/停用、效果表示替换或 Retcon，采用 strict 或先建立新语义结果。保守范围导致误冲突时可以重新证明实际结果未变并重签；仅把 token 改成当前值不合法。

## 4. 不能省略的服务绑定

下表是服务测试责任，JSON Schema 不能单独保证跨对象等式和数据库来源。

| 绑定对象 | 必须核验 |
|---|---|
| 原世界 | request.base_world_ref = certificate.base_world_ref = proposal.input_world_ref = 当代 TaskSpec.world_ref = ContextBundle.world_ref |
| 原依赖 | request/certificate/proposal/ContextBundle及批准使用原依赖指纹；它对应原不可变 DependencyManifest。新采样 scope token 不覆盖它 |
| 任务 | task_id、generation、context_bundle_id一致且属于当前 Principal/grant；TaskResult另匹配其 attempt_id，不能拿其他任务结果冒用 |
| 提案 | 精确 ArtifactRef、原始字节哈希、已登记类型及schema版本一致；证书不能移用于相似或同名提案 |
| 证书 | 可信服务持久化的签发/验证来源、完整命令模板、采样切面、解释清单及有效期；客户端上传同形JSON不产生资格 |
| 批准 | approval_refs精确对应服务器认可的binding集合；subject_sha256匹配提案；批准政策允许所选模式；锁内资格未撤销且条件满足 |
| 当前世界 | V是P的同分支祖先，B是V祖先；history epoch相容；canon别名使用者还需守卫当前canon指针。strict额外要求P=B |
| 所有守卫 | 所属作品/分支、registry版本、history epoch、scope_key及单调版本都匹配；重复或矛盾token不能被字典覆盖后静默接受 |
| 查询结果 | 每个TypedStateAnswer的world_ref、plane和目标时间匹配固定read_cut与原查询；subject/predicate组合无静默遗漏或重复；失败项明确返回，不能缩掉后声称完整 |
| 幂等 | 作用域含work、principal、command_type与业务键；同键同内容返回原回执，同键异内容拒绝；换键也受目标分支已接受提案的唯一约束 |
| 回执 | 分别保存original_base、validation、actual parent及new world；哈希始终绑定原请求，不因实际parent前移而改写 |

数据库故障的有限重试只重跑短事务及当前核验；不重新抽取 LLM 结果。提交响应丢失时先以原键对账。迟到/已取消任务若从未接受则拒绝新写；若早已成功，仍可在当前读取授权范围内获取原回执，不把历史成功改为失败。

## 5. 跨模块检查结果

| 检查项 | 核对结果与边界 |
|---|---|
| 一个世界权威源 | 时态关系、图谱和上下文均由所选世界记录/事件派生；查询外观不是另一写源；ASSERTION不成为运行状态后门 |
| 故事时间与版本 | Story/Valid Time控制小说状态；revision与recorded_at控制记录历史；数据库排序不创造故事因果 |
| 全局假冲突 | 同分支短序列保留，受守卫模式可接纳无关变化；不宣称移除了单分支物理提交上限 |
| write skew | 除写实体外登记共享约束/否定范围，如库存总量、至少一人留守；不能只比较被改行 |
| 幻读/新域 | 无命中行的查询仍有范围token；新增实体/通道/过程更新父registry；不是依赖行数为零就跳过守卫 |
| ABA | scope删除后不归零；tombstone、单调版本和registry/history边界阻止旧令牌复活 |
| 并发抢资源 | 冲突组在接受前联合裁决并sealed；抢锁先后不决定胜负；真实前提改变需要重裁决 |
| 角色与读者 | BELIEF固定主体和known_at；READER固定叙事版本、排列和切点。ACTOR recipe不能混入WORLD/PLAN/READER辅助材料 |
| 查询与上下文 | 普通批量单一知识切面；ContextCompiler的主/辅助切面由服务端recipe授权，不能靠客户端字段提升权限。操作能力与知识平面权限同时取交集；TIMELINE(PLAN)仍需PLAN_READ，不因TIMELINE本身只需VIEW_READ就放行 |
| 固定查询等待 | FIXED世界的未求值范围不因等待新revision而变更；可等待相同世界的投影构建。动态WAIT不自动触发模拟 |
| 自身写可见 | RYW验证同分支祖先关系和回执授权；canon切换不以较大branch_seq冒充包含旧提交 |
| 宏微重复效果 | occurrence、effect_group及活动表示选择保持唯一；正文展开不再次应用资源/伤势/权属效果 |
| 原子性 | 世界选择、关键投影、coverage、token、最小失效屏障、outbox和回执同事务；派生全文/语义索引可异步 |
| 多子任务失败 | bounded ChangeSet可原子接受；独立子任务整体失败不会自动补偿撤销已经接受的世界事件 |
| 传输与任务 | MCP连接不是模型角色会话；断流不等于持久取消。每请求验证显式身份/任务/授权 |
| 池与背压 | 模型等待不占数据库连接；每并发Session独立、各进程自建池；共享配额/UNKNOWN调用持久记账 |

## 6. 实现后必须执行的反例

以下是验收输入，不是本次已通过的运行结果；完整负载档位见[15](../docs/15_并发压测与端到端验收.md)。

| 反例 | 预期判定 |
|---|---|
| 同一旧B的两个无关地区模板依次接受，第二份证书的V落后于P | 依赖完整且token不变则第二份可通过，保留不同original/validation/parent，不要求无意义的模型重推理 |
| 一个writer改留守人员A，另一writer改B，行不重叠但共同违反最低留守人数 | 约束范围发生冲突；不能两份都沿用旧证明通过 |
| 证书证明“桥上无守卫”后新建守卫；原读集没有命中行 | 父/否定范围token变化，旧证书拒绝 |
| 删除scope并创建同名scope，或者角色状态A→B→A | 单调版本/registry阻止旧资格恢复 |
| prepare读取Wv后变化提交，再试图用变化后的token包装旧查询结果 | 一致采样证明不成立；服务拒绝拼接；只能重新采样和验证 |
| interpretation改变但WorldRef不变；或活动卷纲改变但世界没变 | 对应守卫失效；固定历史读仍按原清单/解释处理，不静默变义 |
| 审批后撤销与正式接受并发 | guard确定明确线性化先后；撤销先完成则拒绝新接受，接受先完成则保留既有历史 |
| 同键成功响应丢失后任务取消，Agent重试；另一请求同键换证书 | 前者返回有权读取的原receipt且效果一次；后者幂等内容不符而拒绝 |
| dynamic compile得到不同WorldRef，而原TaskSpec尚无bundle | 返回TASK_REBIND_REQUIRED；原TaskSpec不修改，控制面另建输入代际 |
| actor请求WORLD辅助段或借batch混合PLAN；错误/依赖句柄包含秘密scope名称 | 拒绝越权；错误和句柄裁剪，不泄漏隐藏实体是否存在 |
| canon推广后用旧branch receipt执行RYW | 明确分支变化或选择旧分支；不能仅比较局部序号 |
| world事务成功、outbox消费者失败并重复消费 | 权威事实及回执可查，异步派生可重试；关键读回源，不重复施加世界效果 |
| 长事务外模型任务、lease接管与迟到结果交错 | 不长期占数据库锁/连接；旧generation无新写资格，已接受的回执仍可审计 |

## 7. 尚须验证的工程假设

最关键的剩余风险是模板依赖的**完整性实现**，其次是调度封闭证明和所有writer是否确实参加守卫协议。开发时应先以少数明确资源转移/局部变化模板完成真实数据库反例，再开放新的模板，不能以增加scope枚举数量代替证明。

PostgreSQL锁模式、取消/撤权与接受交错、连接池配置、进程崩溃、响应丢失及副本滞后必须在实际数据库和Host上验证。固定锁序降低死锁风险，但不能据此声称绝无死锁；出现死锁/序列化失败应有限重试整个短事务，并重新检查守卫。[PostgreSQL锁文档](https://www.postgresql.org/docs/17/explicit-locking.html) [隔离语义](https://www.postgresql.org/docs/17/transaction-iso.html)

解释清单必须能够定位实际可恢复的reducer/projector/规则/历法版本，兼容证明也需有证据；只在JSON中写一个版本号不足以恢复历史。宏微语义等价、开放文本依赖、复杂历法与长篇文学效果保留原型/人工评估边界。本轮未为它们声明普遍有效的自动解法。

并发Agent数、工具RPS、数据库读取、世界接受及外部模型在途数仍分开报告。正式性能结论必须附硬件、数据库版本、数据规模、负载行为、冷热缓存及错误/重试统计；本文没有以设计推断替代压测。
