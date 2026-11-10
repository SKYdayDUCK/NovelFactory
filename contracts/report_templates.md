# 修订、影响、重验证与正文对账报告契约

> 状态：字段和校验责任已确定，供 P1/P2 转成 Pydantic/JSON Schema；当前尚未提供这些报告的完整机器 Schema 或业务实现。
> 已有类型复用 [core.schema.json](core.schema.json)。本文新增字段只属于本报告契约，不能直接塞入 `additionalProperties: false` 的其他请求中。

报告通过 `ArtifactRef` 固定内容；引用存在和哈希正确只能证明“这份报告已保存”，不能证明报告结论成立。模型可以提交候选证据和解释；正式接受服务必须完成以下来源绑定、确定性核验和授权检查。检查失败或未执行时保持阻塞，不把模型自报的 PASS 当执行证据。

## R1 适用边界

本页保留报告 `contract_version=2.0` 和原字段，不将 2.1 接受证书字段塞入旧 DTO。Retcon、历史替换和跨分支推广仍采用严格基线/CAS；这些报告不能自行授予 DEPENDENCY_GUARDED 资格。新增查询/接受契约见 [R1_README](R1_README.md)。

R1 实现还须由可信验证记录绑定报告的精确 ArtifactRef、对应 `interpretation_ref`、当前授权/批准资格，以及依赖“当前采用”的计划或叙事选择。相同 WorldRef 下解释或活动选择变化也可能使报告不适用；不能只比较报告中的 projector_version 或对象存在。旧 2.0 报告缺少这些可信记录时须重新验证，不默认兼容。原不可变报告和批准不改写；若以后将这些绑定纳入报告载荷，应显式升级报告版本并增加机器契约及兼容测试。

## 1. 共用头部和引用

四种报告均有以下必需字段。数组可以为空，但空数组不自动证明“没有影响”；是否完整由服务器依据输入及要求检查。

| 字段 | 类型与约束 | 校验责任 |
|---|---|---|
| `contract_version` | 固定 `2.0` | 报告解析器；后续变化保持兼容或升级版本 |
| `report_id` | UUID | 应用服务分配；不可冒充审批 ID |
| `task_id`、`generation`、`context_bundle_id` | 复用任务字段类型 | TaskService 核对来源任务和输入绑定 |
| `dependency_fingerprint` | Sha256 | 解析服务器保存的 DependencyManifest；禁止 Agent 自报兼容 |
| `source_artifact_refs` | ArtifactRef 数组 | 回读并核对哈希、当前可读权限、输入版本 |
| `evidence_refs` | EvidenceRef 数组 | 原文跨度、事件版本和规则引用实际可解析 |
| `blocking_issues` | ReviewIssue 数组；这里只放未解决的阻塞问题 | ReviewService/负责用例核验处置；不能只改 disposition 掩盖问题 |
| `unresolved_scopes` | 非空字符串组成的数组 | 服务端内部作用域标识；无权角色不能看到秘密范围名称 |

报告自身的 ArtifactRef 在保存后由对象服务返回，不嵌入其自身正文，以免形成自指哈希。作者、执行器及实际验证时间由持久任务/检查记录提供，不能从模型写的署名获得可信身份。

### 1.1 WorldStateBinding

报告中的 `old_binding`、`candidate_binding` 或 `world_binding` 均采用下列结构，所有字段必需。

| 字段 | 类型 | 含义 |
|---|---|---|
| `world_ref` | WorldRef | 作品、分支和不可变版本的完整绑定 |
| `record_selection_manifest_sha256` | Sha256 | 该 WorldRef 的有效不可变记录选择清单哈希 |
| `effect_selection_manifest_sha256` | Sha256 | 该 WorldRef 的有效 occurrence/version/group/representation/effect 选择清单哈希 |
| `projector_version` | 非空字符串 | 本次核验实际使用的投影器版本 |
| `ruleset_refs` | ArtifactRef 数组 | 本次受影响范围采用的不可变规则集 |
| `registry_version` | 非空字符串 | 过程/通道注册表版本，与已有 ProcessAdvance 的语义一致 |
| `policy_version` | 非空字符串 | 本次核验采用的权限/作品策略版本 |
| `scope_coverage` | ScopeCoverageBinding 数组 | 下列受检查范围与实际证明 |

`ScopeCoverageBinding` 的必需字段是 `scope_key: string`、`story_interval: StoryInterval`、`proof_ref: ArtifactRef`。证明必须能回到相应过程、通道、前沿及未解析范围；采用半开区间，不因 D30 之前已检查就声明 D30 的并发交汇已结束。`proof_ref` 的存在也需由调度服务核对，不能把模型文字当成 AdvanceCoverage。

这两个选择清单哈希由服务器从 WorldRef 解析出的同一权威清单计算，使用本项目统一的规范化序列化；不能让报告作者提供另一套自由选择的记录。规则和旧投影版本按该绑定解释，candidate 则必须使用本次接受策略认可的投影器/规则；不通过切换到新规则重新计算旧世界。若这些输入在接受前变化，报告停止适用，转重建或重新验证。

### 1.2 DependencyTarget

影响项目采用带判别字段 `kind` 的封闭联合，禁止无类型字典：

| kind | 其余必需字段 |
|---|---|
| `ARTIFACT` | `artifact_ref: ArtifactRef` |
| `EVENT` | `world_ref: WorldRef`、`event_ref: EventRef` |
| `RECORD` | `world_ref: WorldRef`、`record_id: UUID`、`record_version_id: UUID` |
| `TASK` | `task_id: UUID`、`generation: nonnegative integer` |
| `SCOPE` | `world_ref: WorldRef`、`scope_key: string` |

这些字段是报告中的定位方式，不是任意读取句柄。所有引用仍校验作品、分支、版本归属及当前权限。

## 2. RetconPlan

由 `VersionService.prepare_retcon` 持久化，绑定已有 `PrepareRetconRequest`。VersionService 负责创建或复用该命令对应的一条 REPAIR 分支，不让调用者因重试创建多条无关联修复分支。

| 额外必需字段 | 类型/含义 |
|---|---|
| `old_binding` | WorldStateBinding；必须对应请求的 expected_canon_world_ref |
| `candidate_binding` | WorldStateBinding；计划形成时的 REPAIR 基线，不宣称最终重建已通过 |
| `requested_changeset_ref` | ArtifactRef；作者请求固定的原变更包 |
| `prepared_changeset_refs` | ArtifactRef 数组；按目标 REPAIR WorldRef 生成的不可变 WorldChangeSet |
| `event_operations` | EventSelectionOperation 数组，明确 ADD/REPLACE/DEACTIVATE 和前后版本 |
| `record_operations` | RecordSelectionProposal 数组，明确非事件记录的选择关系 |
| `earliest_impact_point` | StoryPoint 或 null；null 表示从初始基线重建或先证明保守边界 |
| `rebuild_mode` | `FROM_GENESIS` 或 `COMPATIBLE_SNAPSHOT` |
| `rebuild_base_ref` | ArtifactRef；初始基线或相容快照/来源清单 |
| `required_scopes` | 数组；每项包含 scope_key 与 StoryInterval |
| `protected_release_ids` | UUID 数组；与请求及服务器已知保护范围一致 |
| `reason` | 非空说明及作者意图，不提供权限 |

准备阶段的 candidate_binding 可以是修复分支初始版本；完成重建后，ImpactReport/RevalidationReport 绑定实际最终候选版本，并以该计划引用及修复提交来源链证明延续关系。不得简单要求初始与最终 revision 相同，也不得只因 branch_id 相同就接受任意新 head。

若原变更包固定在旧 canon，VersionService 生成新的目标分支包和上下文，记录来源关系；不原地改 requested_changeset_ref 的内容或让 WorldCommitService 暗中替换 WorldRef。D12 借改赠选择同一 occurrence 的新 event_version；D30 发言是否保留或修订须独立说明，不能由产权差异自动删掉。

**服务核验：** 旧 canon、目标分支用途、替代的 prior 版本、最早影响边界、快照相容性和保护范围。REPAIR 重建尚未完成不影响旧 canon 正常查询，但该计划不提供推广资格。

## 3. ImpactReport

由 ImpactService 汇总确定性依赖传播和待语义复核项目；作用是组织重建和审核，不直接裁定所有受影响正文都必须删除。

| 额外必需字段 | 类型/含义 |
|---|---|
| `old_binding`、`candidate_binding` | WorldStateBinding；固定比较两端 |
| `retcon_plan_ref` | ArtifactRef；本次修订计划 |
| `changeset_refs` | 实际用于本次比较的 WorldChangeSet ArtifactRef 数组 |
| `affected_items` | 下述 ImpactItem 数组 |
| `conservative_scope_barriers` | 作用域字符串数组；细依赖展开未完成时的最小保守屏障 |
| `unsupported_checks` | 非空字符串数组；当前机制无法可靠判断的检查项，不能省略来伪装完整 |

ImpactItem 的必需字段：`target: DependencyTarget`；`dependency_kind` 为 `HARD_PRECONDITION/COMPUTED/SEMANTIC_REVIEW/REVEAL/STYLE`；`required_action` 为 `REBUILD/REVALIDATE/REVIEW/PRESERVE`；`cause_evidence_refs: EvidenceRef[]`；`explanation: string`。PRESERVE 需证据支持，不能以模型认为“应该无关”替代范围比较。

**服务核验：** 图展开起点与实际改变一致；覆盖正向依赖和相关负向谓词；硬前提、计算重建、语义风险和文学选择不混淆。尚不能证明依赖完整时采用保守屏障，阻断相关新接受，保留旧版本合法查询。

## 4. RevalidationReport

由受控验证用例汇集实际检查结果，绑定最终候选。它不是让语义 Agent 盖一个 PASS 章。

| 额外必需字段 | 类型/含义 |
|---|---|
| `old_binding`、`candidate_binding` | WorldStateBinding；candidate 对应拟推广的确切 head |
| `retcon_plan_ref`、`impact_report_ref` | ArtifactRef；两者内容和比较范围相容 |
| `checks` | 下述 VerificationCheck 数组 |
| `semantic_decision_refs` | ArtifactRef 数组；D30 台词等不可纯算法决定问题的具体审议记录 |
| `unsupported_checks` | 非空字符串数组；未支持检查显式保留 |

VerificationCheck 的必需字段：`check_id: string`；`check_kind` 为 `SELECTION/RESOURCE/TIME/COGNITION/PROCESS_COVERAGE/TEXT_ALIGNMENT/PROTECTED_RELEASE/SEMANTIC_DECISION`；`scope_keys: string[]`；`outcome` 为 `PASS/FAIL/NOT_RUN/UNSUPPORTED`；`verifier_version: string`；`evidence_refs: EvidenceRef[]`。

**服务核验：** 每条检查必须对应可信检查执行记录或具体授权的语义决议，并匹配当前输入；模型生成同形 JSON 不能成为检查记录。必需检查集合由 RetconPlan、规则和当前策略推导，不能由报告作者删掉失败项后只提交 PASS 子集。缺项、FAIL、NOT_RUN、UNSUPPORTED 或未决高影响范围阻止推广，除非策略对确切语义事项已有可追溯决议；硬约束不能被审美意见豁免。

PromotionRequest 的 `revalidation_report_ref` 必须指向此报告；`candidate_world_ref`、相关清单哈希、投影完成状态及过程覆盖均须匹配。短事务检查输入绑定和持久验证资格，不在事务内再次执行模型或全卷重建；失效屏障和 head CAS 防止长时检查后的过期使用。

### 4.1 修订批准的内容哈希

`Approval.subject_type=RETCON` 时，`subject_sha256` 绑定 PromotionRequest 中以下固定业务内容的规范化摘要：`expected_canon_world_ref`、`candidate_world_ref`、`retcon_plan_ref`、`impact_report_ref`、`revalidation_report_ref`、`policy_version`、`dependency_fingerprint`。

不把 `approval_refs` 放入它自己的审批对象哈希，避免循环；任务代际、当前 grant 和命令幂等仍独立校验。原批准不能通过更换报告引用或 candidate head 自动适用于新内容。

## 5. AlignmentReport

由 NarrativeService 的对账组件保存；事实抽取 Agent 只提供候选。它在 DraftSubmitRequest 中作为 `alignment_ref` 被引用。

| 额外必需字段 | 类型/含义 |
|---|---|
| `draft_ref`、`scene_contract_ref` | ArtifactRef；分别与提交请求中的精确内容一致 |
| `world_binding` | WorldStateBinding；草稿所采用的确切世界与投影 |
| `narrative_revision_id`、`arrangement_version` | 与 SceneContract 相同的类型和含义 |
| `reading_coverage_refs` | ArtifactRef 数组；实际阅读记录，摘要覆盖不等于通读 |
| `segments` | 下述重要跨度对账项数组，不要求每个普通词都入图 |
| `critical_commitment_checks` | VerificationCheck 数组；入/出状态、资源、时间及认知边界 |
| `unmatched_critical_spans` | TextSpan 数组；尚未可靠归类的重要原文范围 |
| `open_change_refs` | ArtifactRef 数组；尚未完成的世界增量或历史修订 |

每个 segment 必有 `text_evidence: EvidenceRef`（仅 ARTIFACT，带 TextSpan，指向本 draft_ref）、`classification`、`event_refs: EventRef[]`、`proposed_change_refs: ArtifactRef[]`、`issues: ReviewIssue[]`。classification 只取 docs07 的 `EXISTING_EVENT_PRESENTATION/SURFACE_DETAIL/SPEECH_OR_BELIEF/NEW_WORLD_EFFECT/HISTORICAL_CONFLICT/READER_DISCLOSURE_CHANGE/UNCERTAIN`。

**服务核验：** 文本哈希和跨度、WorldRef 中有效 event_version、重要效果覆盖、场景承诺与合法认知。既有事件呈现只建立映射，不重放效果；新发言即使命题为假，也可能需要发言/观察事件。无法解释的关键跨度、新效果未接受、历史冲突或不完整覆盖会阻断新的正文接受。抽取遗漏通过终态直接核验和独立回读补防，不能声称零遗漏。

最终正文接受仍由 NarrativeService 校验当前 generation、依赖、批准和失效屏障。此处固定报告载荷，不新增普通角色的管理员接受工具；专用内部正文接受 DTO 在 P2 依这些已定输入及职责形成。

## 6. 最小实现与验证边界

P1 实现前三类报告的封闭类型、真实引用校验、可信验证记录及推广门禁；P2 实现 AlignmentReport 与正文接受门禁。Schema 负责字段和结构，应用服务负责引用内容、权限、版本、完整性和真正在何处执行过检查。

最少加入四个反例：伪造 PASS 报告无执行记录；改变 candidate head 后复用旧报告；仅存在 ArtifactRef 但清单/规则/registry 不匹配；旧正文对账报告直接换 WorldRef。四种情况都不能通过接受。报告保留候选与失败证据，不因此修改旧正式历史。
