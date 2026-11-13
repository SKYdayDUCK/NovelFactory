# P6 全文独立文学评阅协议

量表在三部作品开始前冻结为 `P6-rubric-v1`，其规范化 JSON 的 SHA-256 为 `fc2e01f90cf64c4e6d112b1a2fbc92305b82828d41206206c3e7922611528b2e`。每位评阅者按整数独立给分，合计满分 100。读者任务只收到评分项、允许的否决项、待阅原文或本轨阅读笔记；系统在阅卷结束后应用准入规则。[量表与引用校验实现](../../src/novel_factory/domain/book_review.py)

| 评分项 | 满分 | 评分项 | 满分 |
|---|---:|---|---:|
| 故事结构 | 9 | 情节因果 | 10 |
| 人物塑造 | 9 | 角色动机 | 8 |
| 叙述声音 | 8 | 语言表达 | 8 |
| 章节节奏 | 7 | 情绪与阅读吸引力 | 7 |
| 伏笔及兑现 | 7 | 类型文学适配 | 6 |
| 世界规则一致性 | 6 | 人物认知与信息披露 | 5 |
| AI 风格与重复模板 | 5 | 整体阅读体验 | 5 |

## 正文、阅读与举证

1. 冻结拟发布版本。核对分支和 WorldRef、当前已接受的草稿、对账、编辑批准，以及每场正文与实际原生写手结果的 SHA-256。对来自灵感入口的作品，验证原始篇幅约束、已接受的模型设计和独立事实审核。滚动作品按已冻结的章序重建全书，把前卷正式发布的正文与终卷完整接受集依次连接，场间插入两个换行。同一版的两条阅读轨共用同一全文哈希、边界和量表。
2. 本次双生产模型实验由当前开发平台的两个独立 GPT 子 Agent 担任终评，两条轨道均阅读全文，每轨每版一次。开发平台签名交换见下节。沿用原生模型评阅的其他项目，由管理员预先登记两条固定阅读轨和相应预算。登记时固定每轨路由的模型配置哈希，分包、汇总、评分及回执都复核同一配置。每轨由独立任务、独立权限和独立上下文驱动。只允许原生配置的无状态文本路由。若两条均使用同一模型家族，应披露判断相关性。评阅者看不到作者和编辑的工作记录、另一轨意见、门槛或目标。
3. 系统将全部原文按 Unicode 码点切成每包最多 6,000 个字符。每包中每段都须提交可定位引文和具体观察，校验场序、起点、引文及分段边界。缺段、错位、越界或凭空引用均阻断。覆盖回执证明原文已送达并得到可核对的回应，无法单独证明模型的主观理解程度。
4. 长稿按同一读者每四组笔记分层汇总，保留原始逐段记录，汇总引用逐一核验其确实来自本轨输入，以控制最后评分上下文的大小。两名读者分别基于自己的完整阅读笔记，对所有 14 项给出原文位置、引文和理由；扣分还须给出可执行的改进方向。否决项须指出作品不完整、主线因果失效、核心人物选择无依据、大面积重复填充、主要人物声线无法区分或关键设定矛盾破坏主线，并独立举证。每次调用绑定 Task、generation、Attempt、Context、模型及结果。需要核账时，保留原 Attempt。
5. 只计预先登记的两份结果，保存分项、总分、分差、平均分及任一严重否决。两份均为同一版，且无否决时，平均分达到 85 为合格，达到 95 为目标达成。低于 85 或有否决为不合格。各书分别判断。没有读完或没有两份结果时，分数一律留空。
6. 首轮不合格须定位问题并修订，再完成同一套验证。平均分在 85 至 95 之间可按影响决定是否定向优化。二轮登记要求新全文哈希、上轮对应评分回执、实际用于新版本的计划、文学材料或写手运行规范变更，以及预期效果与验证方式。保留两版证据；第二轮后停止。跨已冻结卷的修订尚需在真实生产中验证版本移交。

## 本次实验的开发平台评阅

协调人通过项目授权登记当前开发平台的两个全新 GPT 子 Agent。每部作品、每个拟评正式版本使用两个分开的空白上下文，协调人核对平台返回的会话身份，记录两者均未参与该书创作。不要向子 Agent 转发父会话、生产配置、评阅门槛或另一轨的任何材料。评阅会话各自持有一把新 ECDSA P-256 私钥（SHA-256 签名），只将公钥交给协调人。下列命令中的文件均为本次评阅的私有文件，导出文件须用新文件名。

1. 在两条独立会话中各执行一次 `uv run python scripts/p6_external_review.py keygen --key READER.key`。每个子 Agent 保存自己的私钥。协调人只收取公钥与平台会话标识，平台显示具体模型名时一并记录。
2. 协调人建立私有 `settings.json`，包含两项组成的 `external_judges` 数组，每项固定 `session_id`、`public_key`，可附平台显示的 `model`。另填 `deadline_seconds`（1–86400）及 `idempotency_key`。滚动作品的首个终版可由服务重建编辑批准的 ReleaseRequest，其他场合传入原始 `release_request`。二轮沿用原协议提供修订说明及证据。登记时核验正文生产凭证、原始篇幅、完整冻结章序和拟发布版本。外部模式登记后，这部作品的下一轮仍须使用同一评阅方式。
3. 协调人导出两份盲读材料，将每份单独交给对应的子 Agent。文件内有有序的原文、场序与码点边界、冻结量表、已登记题材和匿名作业绑定。长稿也可加 `--packet N` 按包导出，按序传齐该轨全部包。现有生产配置只单列题材，未另行收集读者画像。

```sh
uv run python scripts/p6_external_review.py enroll --binding "$NF_P6_BINDING" --settings settings.json
uv run python scripts/p6_external_review.py export --binding "$NF_P6_BINDING" --track 1 --output reader-a.json
uv run python scripts/p6_external_review.py export --binding "$NF_P6_BINDING" --track 2 --output reader-b.json
```

4. 每位子 Agent 在自己的上下文通读并记录。提交的 assessment JSON 只含 `observations` 和 `report`。前者是一包一数组，包内按原序每段一条，字段为 `scene/start/quote/observation`。后者由 `scores`、`vetoes` 组成，沿用上方分项、原句、位置、理由及扣分改进建议。读者写完后在自己的会话运行下列命令。若为分包导出，在 `--assignment` 后依序列出每一个包的文件名。签名工具会先复算全文与场序哈希并逐段核验。文件需为当前用户所有的私有文件（POSIX 0600）。

```sh
uv run python scripts/p6_external_review.py sign --key READER.key --assignment READER.json --assessment assessment.json --output signed.json
```

5. 协调人分别执行导入，两份都完成后查看结果。每份已认证返回都以原件归档；相同内容可重放，重签的另一份意见不能替代原件。错误会话签名、错版或篡改文件在导入前拒绝；当前版的真实签名若缺段或缺证，收据为 `REJECTED` 并阻断发布。只有两份有效的同版报告才能计算平均分。正式冻结仍须经 ReleaseService 全部原有检查。

```sh
uv run python scripts/p6_external_review.py import --binding "$NF_P6_BINDING" --audit signed-a.json
uv run python scripts/p6_external_review.py import --binding "$NF_P6_BINDING" --audit signed-b.json
uv run python scripts/p6_external_review.py status --binding "$NF_P6_BINDING" --reports
```

对外部评阅，分数引用可追溯至原始签名包、绑定公钥、会话标识及管理员导入身份。开发平台协调人核对会话和公钥归属；签名验证签署人控制登记的密钥，没有提供模型厂商的身份证明。两个同家族 GPT 的判断可能相关。此评阅路径不登记生产供应商 Attempt、Token 或金额。[外部评阅服务](../../src/novel_factory/application/external_book_review.py) · [PostgreSQL 17 工程夹具回执](evidence/development-platform-judge-pg.xml)

## 原有模型路由评阅入口

将已有 ReleaseRequest、两条已授权的 routes、已有 budget_id、每次金额与 Token 预留、有限有效期及幂等键置于私有登记文件。使用同一项目绑定执行：

```sh
uv run python scripts/run_supervisor.py --access-file "$NF_P6_ACCESS" --start-book-review "$NF_P6_REVIEW"
uv run python scripts/run_supervisor.py --access-file "$NF_P6_ACCESS"
uv run python scripts/run_supervisor.py --access-file "$NF_P6_ACCESS" --book-review-status
uv run python scripts/run_supervisor.py --access-file "$NF_P6_ACCESS" --book-review-reports
```

恢复登记的模型配置或排除调用前故障后，可使用 `--resume-book-review`。暂停任务在其全部代际均无发送或交付证据时，可以重新领取原任务；已结算的零用量失败还须有运行时未发送凭证。核清已送达的成功结果后，系统继续处理该结果。两条原轨、阅读包、授权和预算均保留。已送达的无效评分或错位引文、过期或被撤销的授权均不能以此命令重新评阅。两轨均通过后，正式发布继续走 `ReleaseService.freeze` 的完整条件检查；发布时将评阅读卷、两份分数及有序正文哈希绑定到确切 ReleaseRecord 和 Manifest。[工作流实现](../../src/novel_factory/application/book_review.py)

已生成内部 RELEASE 后，可用 `scripts/p6_export_release.py --reviewed-book --binding PRIVATE --release-ref REF --output NEW_FILE` 重建确切历史卷序，输出两份评分引用、有序章节/场景索引与全文 SHA-256。

[双盲、分层汇总、换路由阻断、版本绑定和回读的工程回执](evidence/book-review-postgres.xml) · [原生编辑、发行与统计回归](evidence/shared-release-harness.xml)

[PostgreSQL 恢复回执](evidence/blind-reader-resume.xml)记录断开路由调用凭据后的发送前暂停、同一任务重新领取与双轨发布校验；已交付的包无法重试。
