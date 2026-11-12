# P6 真实创作运行记录

本次执行了仓库已有的无请求路由预检，现场回执见 [P6_ROUTE_PROBE.json](P6_ROUTE_PROBE.json)。项目没有提供经授权的作品和分支绑定，也没有为首次推理指定供应商、金额上限、Token 总额和每次输出限制。生产模型请求数为零，尚未取得小说正文、供应商账单或独立文学评分。

## 启动现场

1. 在隔离服务账户中读入 NF 服务配置，固定已验收的 Git SHA、PostgreSQL 与 Redis 版本、作品与分支、对象备份、初始 WorldRef、角色 KnowledgeCut 和审核人身份。
2. 执行 `uv run --no-sync python scripts/probe_model_environment.py --output p6-route.json`，检查回执中的 http_route。作者确认价卡、币种、总金额、每次预留、Token 总额和单次最大输出。
3. 按[真实场景运行手册](../../docs/P6_本地真实模型创作交接.md)登记已授权的 3–5 个角色、初始场景及后继窗口。事先标出密信被证伪影响的任务和无关的远方场景。
4. 首场与后继验证通过后按同一实验扩展至 10–20 场。逐场保存 NF Task/Attempt、KnowledgeCut、变更或冲突案件、费用回执、正式接受的正文和 WorldRef。UNKNOWN 保留原 attempt 与费用预留直到供应商对账。
5. 正文接受并冻结后，由具备该作品与分支权限的作者操作员导出，核对回执的正文 SHA-256，分别向两位独立读者发放同一份正文和空表。协调人分别收回原表，随后核对分歧。当前没有分配评阅人。[导出正文、日志与分发步骤](../../docs/P6_本地真实模型创作交接.md#导出冻结卷与独立审读材料) · [独立读者空表](P6_READER_FORM.md)

## 可复核的离线首场预检

模板 [P6_experiment.example.json](P6_experiment.example.json) 记录实验、已接受计划、S9 预标注证据和消费授权。模板 [P6_scene.example.json](P6_scene.example.json) 使用现有 `run_supervisor.py --register-scene` 的实际 JSON 格式。两个模板保留空值，防止误用合成作品或随意设定金额。作者在私有实验目录填好它们。服务账户从原有私有环境向适配器提供凭据，两个 JSON 均不接收凭据。

以下命令使用已安装 NF 锁定依赖的 Python。`P6_TOOLKIT` 指向包含本预检的工作树，`NF_CHECKOUT` 指向部署的 P5.5 或后继 NF 工作树。`P6_PRIVATE` 指向本次实验的私有目录。实验填写实际部署 SHA 和 Skill 目录 Git tree。后者覆盖目录内所有文件，可用同一源码逐字节复核。当前登记入口从首场时间和每个参与者生成自己的 BELIEF KnowledgeCut。填写角色与时间后，正式运行仍需保留实际切面回执。

```sh
umask 077
mkdir -p "$P6_PRIVATE"
cp -n "$P6_TOOLKIT/reports/handoff/P6_experiment.example.json" "$P6_PRIVATE/experiment.json"
cp -n "$P6_TOOLKIT/reports/handoff/P6_scene.example.json" "$P6_PRIVATE/first-scene.json"
git -C "$NF_CHECKOUT" rev-parse HEAD
git -C "$NF_CHECKOUT" rev-parse HEAD:runtime/skills
cd "$NF_CHECKOUT"
uv run --no-sync python "$P6_TOOLKIT/scripts/P6_preflight.py" \
  --nf-checkout "$NF_CHECKOUT" --experiment "$P6_PRIVATE/experiment.json" \
  --scene-file "$P6_PRIVATE/first-scene.json" --output "$P6_PRIVATE/preflight.json"
```

指定的实际供应商端点和模型必须与后端配置相同。金额使用非负十进制字符串。首场每次金额预留及 Token 预留须落在已授权的实验上限内，后端最大输出也须在每次输出上限内。当前场景登记使用 USD。首场 `following_scenes` 的节点与过程来自本地已采用卷计划；提交前还须在已授权作品中核实。两名读者以不同化名登记，对文本独立阅读、评分及分歧的证据在正文产生后收集。

退出码 0 表示文件与本机可执行源码、模型环境的离线检查匹配；退出码 2 表示需要补齐或修正。脚本阻止 Python socket 操作，仅读取文件和 Git 元数据，没有启动服务、登记角色任务或调用模型。输出区分源码、路由、配置材料、阅读人安排及消费账实。正式登记时由 Kernel 校验当前 HEAD、主管授权、真实预算和已采用计划。角色工具所需的原生 Host 隔离仍须单独实测。

## NF 全卷共享预算与回执

PR #6 的 `55d8979b` 在同一资金账户上限制总金额，并对各 Task 分别限制 Token。包含迁移 `0018_p6_shared_tokens` 的 NF 版本新增按 budget_id 共享的全卷 Token 硬上限。登记首场及后续场景时须使用同一实验预算。只有获得作品全域 ADMIN 的私有项目绑定可以查看或设置全卷账户。现有未设总额的预算返回 `total_tokens: null`。读命令访问部署的 NF 数据库，独立离线前检只读回执文件。

在隔离服务账户中，`BUDGET_ID` 为已授权首场的预算 ID，`P6_TOTAL_TOKENS` 为已获准的全卷限额，`P6_ADMIN_BINDING` 为模式 0600 的 NF 项目绑定（token、database_url、object_root 和含 work_id、branch_id 的 binding）。以下命令使用已包含共享上限的 NF 工作树。首次对现有实验预算设置上限前先查询；已有上限需要变更时，使用本次查得的旧值作为 `--expected-total-tokens`，并履行此次变更对应的消费授权。

```sh
cd "$NF_CHECKOUT"
uv run --no-sync python -m alembic current
uv run --no-sync python scripts/p6_budget.py --binding "$P6_ADMIN_BINDING" show --budget-id "$BUDGET_ID"
uv run --no-sync python scripts/p6_budget.py --binding "$P6_ADMIN_BINDING" set --budget-id "$BUDGET_ID" --total-tokens "$P6_TOTAL_TOKENS"
uv run --no-sync python scripts/p6_budget.py --binding "$P6_ADMIN_BINDING" show --budget-id "$BUDGET_ID" > "$P6_PRIVATE/budget.json"
uv run --no-sync python "$P6_TOOLKIT/scripts/P6_preflight.py" --nf-checkout "$NF_CHECKOUT" --experiment "$P6_PRIVATE/experiment.json" --scene-file "$P6_PRIVATE/first-scene.json" --budget-receipt "$P6_PRIVATE/budget.json" --output "$P6_PRIVATE/preflight.json"
```

预检核对回执中作品、账户、币种、总金额、全卷 Token 上限和可用余额。尚无可量化用量的历史 attempt 或不足以预留下一次请求的余额会报告 `shared_budget.receipt`。未传回执时输出 `NOT_SUPPLIED`；文件匹配后输出 `MATCHED_SAVED_NF_RECEIPT`。保存的回执有时间差。真正发送模型请求前，NF 在同一预算行锁下重新核对并预留。PREPARED、SENT 和 UNKNOWN 继续占用 Token，已结算调用计入实际用量。部署旧版时，应先升级到具备该共享配额的受验版本，或提供已实测等效的供应商隔离项目硬配额。

在独立 PG 17.11 上，已对这项迁移执行真实 CLI。新预算首次显示无上限，设置 5,000 个合成实验 Token，重复设置后余额一致。另一作品读取返回 VIEW_DENIED，携带错误旧值的更改返回 IDEMPOTENCY_CONFLICT，交给预检的真实余额回执通过。没有登记场景或发出供应商请求。[现场回执](P6_SHARED_BUDGET_DRILL.json) · [NF 并发、迁移和 UNKNOWN 回归](https://github.com/SKYdayDUCK/NovelFactory/blob/013ebcadf4f7d3a64838096361c6432e4e679b8d/evidence/p6/shared-tokens.xml)

本机在 NF [PR #17](https://github.com/SKYdayDUCK/NovelFactory/pull/17) 的 `013ebcadf4f7d3a64838096361c6432e4e679b8d` 上用空模板执行的[实际回执](P6_LOCAL_PREFLIGHT.json)为 `BLOCKED_ENVIRONMENT`。另用合成、不可路由的 `.invalid` 端点仅检查本地正负路径。完整合成封套通过，金额、Token、环境模型、单次输出不符，重复演员、错误 WorldRef、不同 Git 或 Skill 树均被拒绝；新增保存的共享预算回执匹配、历史用量未定与可用余额不足三项，共八次运行均未联网。可在已安装依赖且源码干净的工作树重现：

```sh
cd "$NF_CHECKOUT"
uv run --no-sync python "$P6_TOOLKIT/scripts/P6_preflight_smoke.py" --nf-checkout "$NF_CHECKOUT"
uv run --no-sync python scripts/run_supervisor.py --help
```

源码入口的 `--help` 已本机验证。实际消费授权、硬配额、私有作品及运行时前置条件齐备后，按本记录“启动现场”和本版本 `docs/P6_本地真实模型创作交接.md` 运行。验收保留首场与后继登记回执、原任务/attempt、独立文学评阅和可对账的成本。
