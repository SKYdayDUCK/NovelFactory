# 创作材料、文学审核与评价边界

审查对象为首次 main 合并 1de471076b8fb4240f95ff8033c60db34c187404。独立审核者逐项对照九类规则的触发器、观察数据、原文、当前权限及修订入口。没有商用模型调用、作品独立盲读或第二部作品。文学质量状态 **NOT_EVALUATED**；S0 工程晋级契约中的 UNPROVEN 是另一字段。

## 九类判断各自能够支持什么

| 类别 | 已实现的取证路径 | 需要独立读者判断之处 |
|---|---|---|
| 世界设定 | 自动解析带类型标识和多个保留字段的序列化对象；对无用设定供读者对照两处原文 | 有无叙事功能，角色在终端上读记录是否有意 |
| 名称 | 显式引名结构、相近字形；第三个近似称谓连接较早两处 | 宗族、历史或职级是否解释相似 |
| 剧情 | 验证“动机缺口/缺后果”有在前的导因和在后的结果 | 识别未明说的动机、因果是否成立 |
| 调度 | 比较同一场景两个位置，拒绝只给一处的位移结论 | 省略、镜头切换、时间跳跃是否充分 |
| 角色 | 精确相同长对白、不同说话人、邻近称名；也接受读者提出情感规则化 | 引用、戏仿、接话与性格声音 |
| 文笔 | 同一句第三次出现产生待查项；读者也可提交意象重复、过量总结 | 副歌、仪式或叙述节奏是否有新功能 |
| 章缝 | 授权目录必须相邻；取前文结尾、后文开头；字面相同长行可提示 | 叠句承接、刻意断裂是否妥当 |
| 整卷 | 需完整分配目录；比较不同章节，自动检查首篇与目标篇章的末段重复 | 承诺有无兑现，结构回环是否有新意 |
| 事实/认知 | 从原生 reconcile 的执行凭证和失败断言读取精确平面、码点跨度和证据；只有对应佐证能给硬判断 | 归属人物的错信、叙述者断言、故事时间和留白需辨别 |

九类共同使用 `_source/_citation/_distinct_spans`；比较证据须在获授权的稿件版本中且互不重叠。剧情还要检查先后，章节接缝还要检查位置与相邻关系；引文的 Unicode 码点和 UTF-8 引文散列共同定位。声称“九种模型都能自动发现语义问题”超出了实现：PLOT 和 STAGING 没有自动建议分支。[creative_review.py L21](https://github.com/SKYdayDUCK/NovelFactory/blob/1de471076b8fb4240f95ff8033c60db34c187404/src/novel_factory/domain/creative_review.py#L21) · [creative_review.py L92](https://github.com/SKYdayDUCK/NovelFactory/blob/1de471076b8fb4240f95ff8033c60db34c187404/src/novel_factory/domain/creative_review.py#L92) · [creative_review.py L127](https://github.com/SKYdayDUCK/NovelFactory/blob/1de471076b8fb4240f95ff8033c60db34c187404/src/novel_factory/domain/creative_review.py#L127) · [creative_review.py L239](https://github.com/SKYdayDUCK/NovelFactory/blob/1de471076b8fb4240f95ff8033c60db34c187404/src/novel_factory/domain/creative_review.py#L239) · [creative_review.py L250](https://github.com/SKYdayDUCK/NovelFactory/blob/1de471076b8fb4240f95ff8033c60db34c187404/src/novel_factory/domain/creative_review.py#L250) · [reconciliation.py L18](https://github.com/SKYdayDUCK/NovelFactory/blob/1de471076b8fb4240f95ff8033c60db34c187404/src/novel_factory/domain/reconciliation.py#L18)

## 原文、视角、作者选择

实际网关 writer 收到场景目的、视点、允许虚构、出入口状态、风格原则、人物口吻和前文原句。actor 请求只接受自己的 BELIEF 与获准引文。系统消息和输出契约仍含运行身份，素材与响应定位字段分开，素材仍是外部输入。测试只检查分区和固定输出的控制词；它不能证明任意模型在遇到叙事内命令时会服从系统，或正常创作不会复述控制语言。[creative_context.py L100](https://github.com/SKYdayDUCK/NovelFactory/blob/1de471076b8fb4240f95ff8033c60db34c187404/src/novel_factory/application/creative_context.py#L100) · [creative_context.py L129](https://github.com/SKYdayDUCK/NovelFactory/blob/1de471076b8fb4240f95ff8033c60db34c187404/src/novel_factory/application/creative_context.py#L129) · [creative_context.py L255](https://github.com/SKYdayDUCK/NovelFactory/blob/1de471076b8fb4240f95ff8033c60db34c187404/src/novel_factory/application/creative_context.py#L255) · [agent_gateway.py L317](https://github.com/SKYdayDUCK/NovelFactory/blob/1de471076b8fb4240f95ff8033c60db34c187404/src/novel_factory/application/agent_gateway.py#L317) · [test_harness_creative_materials.py L65](https://github.com/SKYdayDUCK/NovelFactory/blob/1de471076b8fb4240f95ff8033c60db34c187404/tests/integration/test_harness_creative_materials.py#L65)

每条显式观察须填原因、反解释、叙事功能、建议或原样保留。PRESERVE 在生产评估中留下原始观察而不产生可执行的 finding。单纯出现“账”“名字”“印章”不会触发设定硬冲突。`UNCERTAIN` 也不能直接交给定向写作者修改。在未提供核验事实的情况下，“她以为……”可以作为人物信念保留。版本绑定和证据几何只验证位置、顺序、身份，与“反解释是否真与原文相称”存在明确的人工判断边界。上游角色对应表及版本线索支持追溯，无法单凭线索归责某位编剧。[creative_review.py L325](https://github.com/SKYdayDUCK/NovelFactory/blob/1de471076b8fb4240f95ff8033c60db34c187404/src/novel_factory/domain/creative_review.py#L325) · [creative_review.py L353](https://github.com/SKYdayDUCK/NovelFactory/blob/1de471076b8fb4240f95ff8033c60db34c187404/src/novel_factory/domain/creative_review.py#L353) · [creative_review.py L125](https://github.com/SKYdayDUCK/NovelFactory/blob/1de471076b8fb4240f95ff8033c60db34c187404/src/novel_factory/application/creative_review.py#L125) · [creative_review.py L336](https://github.com/SKYdayDUCK/NovelFactory/blob/1de471076b8fb4240f95ff8033c60db34c187404/src/novel_factory/application/creative_review.py#L336) · [creative_revision_brief.py L70](https://github.com/SKYdayDUCK/NovelFactory/blob/1de471076b8fb4240f95ff8033c60db34c187404/src/novel_factory/application/creative_revision_brief.py#L70) · [test_creative_review_rubrics.py L64](https://github.com/SKYdayDUCK/NovelFactory/blob/1de471076b8fb4240f95ff8033c60db34c187404/tests/unit/test_creative_review_rubrics.py#L64)

**P57-CR-02 · P2**　只提供阅读回执并以空 observations 提交 PLOT/STAGING，会走自动建议的空结果，然后写入原生 PASS 和 advisory ACCEPT_CANDIDATE。完整阅读回执证实取回过所有文字；调用者可能实际审完认为没有问题，也可能未形成独立判断。两种意图使用相同的请求和结果。九类单测均先提交一个人工构造的候选观察，未区别这两种情形。改进方向是在此类模式中让显式“已检阅、无问题”和缺少判断有不同结果，保留其他模式的有证据提示。正式接受仍由原生发布/世界服务把关。此为静态可核的协议语义，未在本次独立会话新跑服务。[creative_review.py L127](https://github.com/SKYdayDUCK/NovelFactory/blob/1de471076b8fb4240f95ff8033c60db34c187404/src/novel_factory/domain/creative_review.py#L127) · [creative_review.py L325](https://github.com/SKYdayDUCK/NovelFactory/blob/1de471076b8fb4240f95ff8033c60db34c187404/src/novel_factory/domain/creative_review.py#L325) · [creative_review.py L310](https://github.com/SKYdayDUCK/NovelFactory/blob/1de471076b8fb4240f95ff8033c60db34c187404/src/novel_factory/application/creative_review.py#L310) · [test_creative_review_rubrics.py L36](https://github.com/SKYdayDUCK/NovelFactory/blob/1de471076b8fb4240f95ff8033c60db34c187404/tests/unit/test_creative_review_rubrics.py#L36)

## 一次配对样本的逐字核对

数据取自仓库留存的三臂校准，freeze.source_commit 为 a7f41ae7d32ad322fe48895950b0245b3096fee6；首次正式合并同一测试再次通过。表中时间与合成计量取自该留存批次，不能替换为本次 main 的耗时。三个窗口均为 COMPLETE、reconciliation=CLEAR、9 次 NF Attempt 成功、0 次正式正文接受，0 次返工、USD 0。[S6_ENGINEERING_COMPARISON.md L14](https://github.com/SKYdayDUCK/NovelFactory/blob/1de471076b8fb4240f95ff8033c60db34c187404/reports/harness/S6_ENGINEERING_COMPARISON.md#L14) · [原始三臂结果](https://github.com/SKYdayDUCK/NovelFactory/blob/1de471076b8fb4240f95ff8033c60db34c187404/reports/harness/evidence/s6/three-arm-comparison.json) · [主线 comparison 作业](https://github.com/SKYdayDUCK/NovelFactory/actions/runs/43197896764/job/131113456646)

| 已公开字段 | A 静态 | B 固定 Harness | C 动态与已准量表 |
|---|---:|---:|---:|
| 真阳 / 漏报 / 多报 | 1 / 1 / 0 | 1 / 1 / 0 | 2 / 0 / 0 |
| 合成输入输出计量单位（UTF-8 字节口径） | 47,788 | 40,685 | 41,347 |
| 窗口耗时（秒，约） | 2.734 | 3.308 | 8.539 |
| 4 个已指定控制字段在正文出现数 | 0 | 0 | 0 |

A/B 原文摘要均为 `518ae2ddee13912c3a4c1d65b90a9b4f49c039880085299c32fe2b0af40fedd5`。B 的“桥边的灯还亮着。”在 C 中成为“守灯人抬灯后才放下门闩。”C 的正文摘要为 `fce57f33babd2ed975a2b1d51834a6cb17e3c35d5504341eba3d865a29078eb2`。后一句新增一个明确的行动顺序，能追溯到岗位简报。B、C 均命中第一段重复的第三次出现 [98,118)；C 多命中另一段重复的第三次出现 [190,209)。这是各自正文上的码点，不能把 C 的第二处坐标套到 B 的文本。两句重复都由夹具按模板写了三遍。[test_s6_engineering_comparison.py L65](https://github.com/SKYdayDUCK/NovelFactory/blob/1de471076b8fb4240f95ff8033c60db34c187404/tests/integration/test_s6_engineering_comparison.py#L65) · [test_s6_engineering_comparison.py L87](https://github.com/SKYdayDUCK/NovelFactory/blob/1de471076b8fb4240f95ff8033c60db34c187404/tests/integration/test_s6_engineering_comparison.py#L87) · [test_s6_engineering_comparison.py L442](https://github.com/SKYdayDUCK/NovelFactory/blob/1de471076b8fb4240f95ff8033c60db34c187404/tests/integration/test_s6_engineering_comparison.py#L442) · [test_s6_engineering_comparison.py L749](https://github.com/SKYdayDUCK/NovelFactory/blob/1de471076b8fb4240f95ff8033c60db34c187404/tests/integration/test_s6_engineering_comparison.py#L749)

配对单位为一个场景，校准批 1、确认批 0。C 同时更换证据专员指令和审阅量表，且其耗时包括评估与授权；一次配对足以说明字节交接与新增跨度工作。硬事实矛盾、自由文学创作、长篇角色声音、隐喻自然度及分组盲读效果均无相应独立结果。登记的 A 是本版本关闭路径。本次没有运行历史版本与真实商业提供商的跨版本盲测。[test_s6_engineering_comparison.py L60](https://github.com/SKYdayDUCK/NovelFactory/blob/1de471076b8fb4240f95ff8033c60db34c187404/tests/integration/test_s6_engineering_comparison.py#L60) · [test_s6_engineering_comparison.py L802](https://github.com/SKYdayDUCK/NovelFactory/blob/1de471076b8fb4240f95ff8033c60db34c187404/tests/integration/test_s6_engineering_comparison.py#L802) · [S6_ENGINEERING_COMPARISON.md L24](https://github.com/SKYdayDUCK/NovelFactory/blob/1de471076b8fb4240f95ff8033c60db34c187404/reports/harness/S6_ENGINEERING_COMPARISON.md#L24)
