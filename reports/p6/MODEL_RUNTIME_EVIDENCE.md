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

编译后的 DAG 描述当前 Profile 的任务及其依赖。持久化的实际领取、审核、正式接受和内部发布回执分别记录执行结果。另一次工程试验从灵感和三章目标开始，先生成两章的初始规划，原生写作并由工程测试读者按既有服务提交事实对账、接受回执。在真实冻结的两章后，主管重建时自动领取新的卷导演任务。导演读取冻结检查点中的正文片段，再由独立评阅者核查续卷候选。系统正式采用新增卷和第三章规划，并在原游标追加第二个窗口。该实例验证了模型驱动的接续登记；没有生成或发布第三章正文。中长篇的 100 章创作、世界演绎与对账以及两次全文盲评仍需真实运行。

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

若用户指定章节范围，系统默认选择范围的下界作为本次授权目标；首次规划已经覆盖更多章时保存该实际数量，仍受用户上限约束。前窗完成后，Supervisor 从冻结章末和当前正式 WorldRef 分配卷导演与独立审核任务，并在原游标追加获准章节。进入下一卷继续要求上一卷的正式内部发布与连续性回执。
