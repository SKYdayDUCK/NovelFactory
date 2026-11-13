# 原生模型、创作输入与首次规划

三部作品的生产模型调用、正文、文学盲评和内部发布回执均为零。三套输入实际经过同一 CLI 的预检，各自生成 32 个带启用或跳过决议的节点。默认 Codex 文本桥在配置检查时返回 `MODEL_UNAVAILABLE`。预检未创建虚构的任务或 Attempt。

| 输入 | 真实预检 | 范围 |
| --- | --- | --- |
| [公寓门外的鞋](../../examples/p6/short_supernatural/inspiration.json) | [短篇回执](../../examples/p6/short_supernatural/preflight.json) | 6,000–10,000 汉字 |
| [修补法宝的代价](../../examples/p6/medium_xuanhuan/inspiration.json) | [中篇回执](../../examples/p6/medium_xuanhuan/preflight.json) | 30,000–60,000 汉字 |
| [副本之间的联系](../../examples/p6/medium_long_infinite/inspiration.json) | [中长篇回执](../../examples/p6/medium_long_infinite/preflight.json) | 100–500 章 |

## 环境诊断

Codex CLI 0.167.0-alpha.4 检测到现有 ChatGPT 登录。仓库原生探测程序使用默认策略、只读沙箱与临时工作目录，发起一次不含作品材料的连接探测。进程以状态码 1 退出，诊断涉及只读文件系统和初始化失败。原签入目录的 app-server 在 `initialize` 回应前退出；单独指定临时 SQLite 目录也未能完成初始化。全新隔离目录中的同一程序成功完成 `initialize` 和 `account/read`，其登录状态为空。三次控制面探测均未提交模型回合。[脱敏环境记录](evidence/model-environment.json)

原生 Codex 订阅适配器从运营者配置的授权环境引用读取计划凭据，校验 OAuth 客户端和模型输出上限。当前环境没有配置此路由。OpenAI API、DeepSeek 及旧版可兼容 HTTP 适配器也未发现已配置可用的一组端点、模型和授权引用。未向付费替代服务发送请求，实际生产费用无账单证据。现有 CLI 登录不能替代 NovelFactory 的任务授权、隔离、单次发送和预算结算。

## 工程验证

独立 PostgreSQL 17 上的四项测试使用明确标记 `TEST_FIXTURE` 的 HTTP 响应。它们验证贯穿服务正常入口的恢复、授权与数据持久化。正例经两次独立身份领取的原生 Attempt，分别执行初始世界设计和事实审核，核对已完成且结算的调用后创建新的正式作品、带出处的初始事实、已采用计划、场景输入和首个滚动窗口。重试返回同一结果，不重新计费。两个反例分别验证事实评阅者指出问题、以及实际费用待核对时均停止在正式作品创建之前。[完整 JUnit](evidence/idea-intake-postgres.xml)

编译后的 DAG 描述当前 Profile 的任务及其依赖。持久化的实际领取、审核、正式接受和内部发布回执分别记录执行结果。另一次工程试验从灵感和三章目标开始，先生成两章的初始规划，在原生监督器调度下完成写作、独立断言提取、事实对账和正文接受。模型响应来自标记清楚的工程测试后端。在真实冻结的两章后，主管重建时自动领取新的卷导演任务。导演读取冻结检查点中的正文片段，再由独立评阅者核查续卷候选。系统正式采用新增卷和第三章规划，并在原游标追加第二个窗口。该实例验证了模型驱动的接续登记；没有生成或发布第三章正文。中长篇的 100 章创作、世界演绎与对账以及两次全文盲评仍需真实运行。

## 灵感模式中各角色的实际执行范围

| 环节 | 入口已执行的工作与证据 | 仍需接入的工作 |
|---|---|---|
| 初始设计及导演职责 | 世界设计师产出标题、一名主角、一个地点、一条故事线、1–24 条文字设定和 1–32 个章节目标。首窗另受预设上限和用户指定的总章数上限约束。独立事实评阅通过后，系统把这些内容落实到初始身份与计划。每个目标生成一张场景卡，限定可添加的内容为不改变世界事实的感官细节。 | 按分层预设另行派发世界扩展、规则审阅、过程语义、主编全书方向与首卷卷导演任务。首窗尚不能产生多人物、多地点、阵营、时间表、获知来源或有凭据的可执行过程。 |
| 人物、环境与裁决 | 原生角色执行服务会为人物固定所属 actor 的 BELIEF 读面和故事时点，密封意图的操作另需正式 Interaction 中恰好属于该角色的槽位。裁决入口要求已登记且到期的语义过程、规则证据、各角色的密封提案和允许的变更范围。 | 灵感入口尚未从评阅过的设定生成并登记过程、首批感知证据、参与角色、作用域与可允许的后果；没有自动派发人物或环境任务。人物名和文字设定不能代替这些登记。 |
| 正文及认知 | 写手只读实际绑定的场景与获准前文。独立 `reviewer/epistemic` 读取同版正文，报告人物获知与披露问题；获准后另一个身份执行全场断言提取。原生服务再次按事实、获知时点、正文交付与引用进行核验。报告出现问题会留下阻断回执。 | 仍需真实文学运行才能检查误判、角色区分、人物弧线与长程一致性。 |
| 接续及局部重规划 | 连续登记需要真实冻结的章末、同版采用计划、独立卷导演及审稿回执。局部重规划服务按预设开关，仅允许替换尚未打开且身份一致的已采纳窗口。 | 灵感入口未自动将已接受世界偏差接入局部重规划；所写续卷使用冻结前文片段与原始目标，尚未持续提前一至两卷登记。 |
| 预设在调度中的作用 | 实际执行窗口章数、写作与读稿并发、审核积压限制、前文交接、受限重试和目标章节边界。跨卷记忆开关控制已发布连续性快照能否交给下卷写手。 | 已编译的层次、场景粒度、各档读稿强度不能等同于逐节点实际执行。当前滚动入口各档均对每章登记文学读稿；窗口内每场均执行独立认知和事实核查。 |

最终内部 RELEASE 报告正文、编辑、阅读与版本条件；每个演绎或重规划环节需要各自的角色任务、调用和世界接受回执。当前这三条输入在预检后仍没有这些实跑记录。

## 运行与恢复

在仓库根目录用 `uv sync --locked --group dev` 安装依赖，在准备好的私有 PostgreSQL 17 数据库执行 `uv run alembic upgrade head`。配置同一部署的数据库连接和 `NF_OBJECT_ROOT`。所选模型由部署者按已有接入方式加载，例如将 `NF_RUNTIME_PROFILES` 指向已授权的私有配置。以下命令使用与该配置相符的 `NF_P6_ROUTE` 环境变量。金额与 Token 由运营者为准备工作及写作工作分别指定，账户和授权上限进入原生预算台账。

```sh
uv run python scripts/p6_idea.py preflight \
  --idea examples/p6/short_supernatural/inspiration.json --adapter "$NF_P6_ROUTE"
uv run python scripts/p6_idea.py enroll \
  --idea examples/p6/short_supernatural/inspiration.json --adapter "$NF_P6_ROUTE" \
  --private-dir "$NF_P6_PRIVATE" --total-amount "$NF_PLAN_USD" --reserve-amount "$NF_CALL_USD" \
  --total-tokens "$NF_PLAN_TOKENS" --reserve-tokens "$NF_CALL_TOKENS"
uv run python scripts/p6_idea.py advance --private-dir "$NF_P6_PRIVATE" --adapter "$NF_P6_ROUTE"
uv run python scripts/p6_idea.py status --private-dir "$NF_P6_PRIVATE"
```

`advance` 每次运行一次有界调度或移交。沿用同一私有目录直至返回 `INITIAL_PLAN_MATERIALIZED`；遇到 `DESIGN_REWORK_REQUIRED` 或待核账状态保留原始回执并处理。每个作品使用独立目录。准备工作只接收用户灵感、篇幅与题材。审核者取得原始委托、冻结的设计和候选引用。事实审核通过后，系统才创建目标作品以及两侧可追溯的来源回执，生成 `first-window.json` 和私有控制绑定。最新章末状态、世界事件与正式接受仍需在后续原生流程产生。

```sh
uv run python scripts/p6_idea.py register-window --private-dir "$NF_P6_PRIVATE" \
  --adapter "$NF_P6_ROUTE" --total-amount "$NF_WRITE_USD" --reserve-amount "$NF_CALL_USD" \
  --total-tokens "$NF_WRITE_TOKENS" --reserve-tokens "$NF_CALL_TOKENS"
uv run python scripts/run_supervisor.py --access-file "$NF_P6_PRIVATE/supervisor.json"
```

私有的 `preparation-control.json` 与 `control.json` 分别绑定准备作品与正式作品。审计导出命令可分别读取两个工程的角色和调用记录。

若用户指定章节范围，系统默认选择范围的下界作为本次授权目标；首次规划已经覆盖更多章时保存该实际数量，仍受用户上限约束。前窗完成后，Supervisor 从冻结章末和当前正式 WorldRef 分配卷导演与独立审核任务，并在原游标追加获准章节。进入下一卷继续要求上一卷的正式内部发布与连续性回执。
