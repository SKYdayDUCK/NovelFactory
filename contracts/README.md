> R1入口：[统一时间、批量读取、上下文编译与受控接受契约](R1_README.md)。原2.0契约保留；下文涉及原始示例的数量以相应索引为准，完整计数见validation/REPORT.md。

# 数据与接口契约入口

本目录是可机校验的**关键契约示例**，并非已实现的数据库、完整OpenAPI或所有题材规则的穷举。字段形状以JSON Schema为准；权限、事务、发生身份与时间语义以03–09为准。两者不一致必须在P0修正，不能在接口层悄悄忽略字段。

## 文件与入口

| 文件 | 内容 |
|---|---|
| [core.schema.json](core.schema.json) | WorldRef、时间、事件选择、WorldChangeSet、任务、提案、批准、修订/推广、发布、查询和工具请求 |
| [literary.schema.json](literary.schema.json) | 文学记忆、原文阅读覆盖、卷创作契约和滚动窗口计划 |
| [tool_catalog.json](tool_catalog.json) | 23个薄门面工具到共享服务、Schema及能力的映射 |
| [examples/index.json](examples/index.json) | 核心合法示例与目标定义 |
| [examples/literary_index.json](examples/literary_index.json) | 文学合法/非法示例以及真实文本夹具哈希 |
| [invalid_examples/core_index.json](invalid_examples/core_index.json) | 必须被结构校验拒绝的核心反例 |
| [canonicalization_vectors.json](canonicalization_vectors.json) | 语言无关内容指纹的规范化测试向量 |

`$id` 使用保留示例域名，只作Schema标识，不要求访问该网站；验证器将本地Schema注册到对应URI。使用Draft 2020-12并启用format检查。多数对象拒绝额外字段，新的字段必须显式升级而非依靠宽松解析。

## 权威责任和组合方式

1. `ActionProposal`只表达角色意图，不能指定成功或直接施加效果。
2. `AdjudicationProposal`保存裁决候选；服务校验并将其转成`WorldChangeSet`。
3. `WorldChangeSet.event_candidates`是可供选择的标准化事件payload。名为`AcceptedEvent`的Schema描述事件记录形状，**它出现在候选中不意味着已接受**。
4. `event_operations`显式ADD/REPLACE/DEACTIVATE某occurrence的版本；只有目标世界提交清单选择后才获得该版本中的地位。旧内容仍保留，替代/停用不能作为普通新增绕过Retcon。
5. `record_operations`处理已验证的实体身份、规则、断言、认知、过程等不可变payload；服务检查kind、payload、前一选择和权限，不允许任意JSON覆盖数据库表。
6. `process_advances`和`trigger_evaluations`是调度服务计算的推进/闭合记录，和事件原子接受；它们不是角色在世界内做出的新动作。
7. `AcceptWorldRequest`引用ChangeSet的ArtifactRef及批准。04中的组合伪代码包括已解析的依赖、task、对象和策略；不能把伪代码所有属性逐字当成传输JSON的字段。
8. `PrepareRetconRequest`引用含替代操作的ChangeSet并固定旧canon。`PromotionRequest`绑定候选WorldRef、影响/重验证报告及批准，只用于授权控制面；普通运行时没有promotion工具。
9. `ReleaseManifest`是本地发布的不可变内容清单，需依赖通过；对外送达另有delivery记录。发布不会调用世界效果应用器。

服务级不变量包括work/branch/revision归属、外键、选中版本存在、作用域和负向查询完整性、批准来源、任务四元组、跨记录资源与时间、最终release相容性。这些不能单靠Schema验证。

## 时间与空值

`StoryPoint.tick`为作品整数时间，限制在JSON安全整数范围；phase只表达已确认同刻因果阶段。`StoryInterval`为确定的半开持续区间`[start,end)`；`OccurrenceTime.BOUNDED`是一次发生的可能点范围，earliest/latest为含端点可能边界，两者含义不同。UNSPECIFIED保持未知，不能偷偷取earliest作为真实时刻。

在模糊发生足以改变查询结果时返回UNDETERMINED；只有状态在全部允许顺序中一致，或具有已经接受的联合裁决，才能给确定结论/推进相关覆盖。MVP可采用保守阻断与简单区间/偏序，不必实现通用时态逻辑求解器。

`evaluated_frontier`是排他前沿：严格早于该点的已登记相关过程完成求值，边界处影响仍需处理。计算等待不改变`world_stage`；world_stage和evaluation_status分开。

QueryAnswer必须含精确story_time或story_interval之一。UNKNOWN不泄漏隐藏确定性或证据；UNRESOLVED的data为null，VALUE不能为null；NONE只在声明完备且已求值、新鲜范围表示确定无值。NOT_APPLICABLE用于已确定不适用。PLAN即使其内容已明确，依然不是已发生历史。

## 任务、证据和角色字段

TaskSpec在READY/BLOCKED或RUNNING准备阶段可无context_bundle_id/dependency_fingerprint；首次外调之前服务必须保存绑定当前generation的完整上下文。RESULT_STORED及后续接受阶段必须有它们。TaskResult必须匹配持久化的task_id/generation/attempt_id/context_bundle_id并复核权限。task_get/task_cancel可在上下文尚未构建时使用已认证任务授权，不能要求不存在的bundle。

EvidenceRef.TextSpan使用原始UTF-8解码后的Unicode code point半开[start,end)，不规范化NFC/CRLF，不把UTF-16位置或grapheme数量当同一单位。quote_sha256是该切片重新UTF-8编码后的哈希。端点和原文哈希必须由服务核验，不能信任模型自报offset。

role_id/actor_id/plane字段只是对象声明，权限来自服务端Principal与TaskGrant。角色声明为editor或重写actor_id不能扩大视图。审稿category（主题）、issue_kind（处置性质）和severity（处理级别）是独立维度。

## 内容哈希约定 NF-CANON-1

原文ArtifactRef.sha256始终对确切原始字节计算。结构化业务指纹使用本项目的有限JSON profile：UTF-8严格编码、字符串保持原样、键按Unicode码点排序、无额外空白，数组顺序保留；仅允许null/bool/安全整数/字符串/数组/对象，实数用Decimal字符串。拒绝重复键、非有限数、浮点、非法Unicode和不安全大整数。该profile不是声称完整实现RFC8785。

幂等payload hash包括目标分支/WorldRef及全部业务参数，忽略传输request_id、连接信息和凭据；principal进入幂等作用域。批准对精确候选对象哈希和依赖指纹绑定。ReleaseManifest的manifest_sha256对删除自身该字段后的规范对象计算；world commit的manifest_hash则对选中记录及效果清单计算，不能拿CommitReceipt正文哈希冒充。

核心示例的UUID与部分ArtifactRef哈希是虚构夹具标识，未提供真实世界对象仓库，不能据此提交生产。文学文本夹具和规范化向量的字节/引用哈希是真实可核验数据；两类示例在索引中分别标明。格式通过不表示对象已持久化或受到批准。

## 兼容和使用

P0选定Pydantic模型作为实现后的生成源，并建立Schema与当前设计文件的差异门禁；当前以Schema文件为边界设计源。不要永久手工维护两份不一致的类型。新增题材effect、关系和record subtype先扩展注册表、Schema、领域规则与反例，不允许执行模型生成的任意程序。

从项目根目录执行 `python validation/validate_package.py` 可检查Schema、全部示例、引用、技能/角色工具目录和原文锚点；它不验证真实PostgreSQL、Host互操作、模型质量或所有领域前置条件。正式开发测试见[11](../docs/11_研发路线与测试计划.md)。
