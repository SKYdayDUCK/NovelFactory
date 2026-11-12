# S3 精确引文、独立审稿与定向修订

编辑为 reviewer、literary_editor 或 chief_editor 安排适用任务，绑定有序的原生 EditorialBinding、稿件版本和阅读权限。审稿人在任务端调用 `nf_harness_review_read` 获取真实文本和覆盖收据。`nf_harness_review_submit` 按稿件顺序接收覆盖收据、独立性凭证、审核层次、观察意见、返工上限和额度。每次观察使用 Unicode 码点记录起止位置，并写明原因、反解释、叙事功能和最小改动。对比引文必须来自本次获准阅读的稿件。章节接缝需要相邻两稿，整卷审查需要完整的有序目录。

| 审核层次 | 可核对的依据 |
|---|---|
| 世界设定 | 设定说明在场景中的用途，或进入正文的序列化控制记录 |
| 命名 | 不同实体名称的相似形式及同一文化、家族或职务带来的合理解释 |
| 剧情 | 前置动机、事件和后果之间的具体文段 |
| 场景调度 | 同一场景内人物或物件的两个位置 |
| 人物与对白 | 被定位的说话者、各自台词和引用他人说法的可能性 |
| 句段文笔 | 重复意象、句式或总结及其在不同位置承担的叙事功能 |
| 章节接缝 | 前一稿结尾和下一稿开头 |
| 整卷结构 | 分散在不同稿件的承诺、回收或重复段落 |
| 世界事实与人物认知 | 经 NF 核对的稿件断言、世界切点、WORLD 或 BELIEF 证据 |

## 独立性与权限

在 `mode=creative_controller` 的私有 NF 端口使用 `nf_harness_review_independence`。传入稿件引用和 reviewer 任务及 generation。NF 从真实稿件派生作者和历次修订作者，从原生任务和 grant 派生 reviewer。多稿审核中每份稿件的完整作者链都要通过检查。角色端没有签发入口。

已委托给 Workshop 的任务使用受信的 `NF_REVIEW_WORKSHOP_BINDING` 和可选的 `NF_REVIEW_WORKSHOP_EXECUTABLE` 启动 Workshop MCP 连接。Workshop 按 W4 登记的审稿案件、主体组、员工雇佣版本、委托与远端收据查询授权。原生 NF 独立部署采用单案登记。管理员按具体稿件、作者链和 reviewer 建立档案，将私有目录配置为 `NF_REVIEW_NATIVE_ATTESTATIONS`，以 `NF_REVIEW_NATIVE_KEY` 指向私有签名密钥文件。签名档案由身份核验方持有，验证时重新读取，改稿、换代、撤权、过期和撤案均触发重新核验。

原生登记工具可通过 `python -m novel_factory.interfaces.review_independence --help` 查询参数。先使用 `describe` 核对稿件和主体，再对每个 principal 提交已查验的私有登记记录，通过 `enroll` 设置有效期。身份记录包括 `kind=VERIFIED_SUBJECT_GROUP`、作品、分支、principal、subject_group、来源记录编号和核验人。`withdraw` 撤回该案件；下一次受保护的审稿或编辑操作将检查当前档案。签名材料和实际主体资料应留在部署环境。

## 编辑及写作

| 操作 | 生效结果 |
|---|---|
| `DEFER` | 保存本次编辑意见，等待补充判断 |
| `RETURN` | 在返工次数与证据允许时，将指定问题交给获准的 targeted_revision 写作任务 |
| `nf_harness_revision_brief` | 作者按已退回的报告取得自己获准稿件中的问题编号、引文和修改指引 |
| `nf_harness_revise` | 按 finding_id 和 replacement 修改引用区间，通过原生 NarrativeService 保存后继稿件 |
| `RETAIN` | 在原生编辑会话内记录原样保留及对各问题的理由 |
| `ACCEPT_REVISION` | 关联独立复核、修订稿与原生 EditDecision |
| `WITHDRAW` | 审稿人从自己的任务端撤回，或由具备权限的编辑记录撤回 |

审核限额绑定 NF 预算、任务截止时间和原生模型 Attempt。确定性检查本身没有模型调用。工程记录保留稿件与文本的摘要、覆盖、问题位置、原因、上游角色和版本线索、前后稿、复核与编辑动作。文学判断需要独立读者验收，状态为 `NOT_EVALUATED`。

[审核服务](../../src/novel_factory/application/creative_review.py) · [审核规则](../../src/novel_factory/domain/creative_review.py) · [Workshop 核查](../../workshop/src/agent_workshop/review_independence.py) · [阶段证据](evidence/s3/receipt.json)
