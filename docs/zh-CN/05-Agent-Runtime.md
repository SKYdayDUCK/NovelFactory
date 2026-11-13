# Agent Runtime 与模型路由

[English](../en/05-agent-runtime.md) · [配置与部署](08-配置与部署.md)

同一部小说可以让一个模型写场景，另一个模型读稿。操作员给每条执行路径命名，Workshop 管理者发布匹配规则，NF 作品管理员采用策略。任务输入会记录选中的配置、匹配规则和策略摘要，事后可以查清某次调用用了哪个模型。

## 配置模型

`NF_RUNTIME_PROFILES` 指向含 `profiles` 对象的 JSON 文件。下面是可以离线校验的单配置示例。实际开通调用前，将模型名改为账号获准使用的模型，并登记相应凭据。

## 管理 API Key

在运行 Supervisor 的同一操作系统用户下执行：

```bash
python scripts/nf.py credentials set NF_DEEPSEEK_API_KEY
python scripts/nf.py credentials status NF_DEEPSEEK_API_KEY
python scripts/nf.py credentials remove NF_DEEPSEEK_API_KEY
```

`set` 使用不回显的输入框；自动部署可用 `--stdin` 从标准输入读取。命令行参数不接收 Key 原文。`status` 只报告引用、是否配置和来源，`remove` 删除本机保存项。已有同名环境变量优先，删除本机项不会删除环境变量。

Windows 使用当前用户的 Credential Manager；Linux/macOS 保存到 `$XDG_DATA_HOME/novel-factory/credentials`，未设置时使用 `~/.local/share/novel-factory/credentials`。POSIX 目录为 0700、文件为 0600，读取拒绝符号链接和其他用户可访问的文件。不同用户、Windows 与 WSL 各有自己的凭据存储。Supervisor 每次发送前解析引用，轮换 Key 后无需把密钥写入任务或重新发布模型目录。真实调用仍需要独立的任务授权、预算和 `NF_RUNTIME_LIVE_APPROVED=1`。

Workshop 的模型路由继续引用 Provider 配置名，配置中的 `secret_ref` 指向本机项。数据库、Git、模型目录和对外报告均不保存 Key 原文。岗位默认由 Codex 负责大纲、规划、审核，DeepSeek 负责正文和角色扮演；通过下文的角色路由规则实现，已开始的任务保留原绑定。

```json
{
  "profiles": {
    "draft-model": {
      "kind": "deepseek",
      "model": "example-chat-model",
      "secret_ref": "NF_DEEPSEEK_API_KEY",
      "max_output_tokens": 2048,
      "timeout_seconds": 90
    }
  }
}
```

将上例保存为 `providers.json`。在仓库根目录执行下列命令可验证配置并列出名称。校验过程不读取供应商凭据，也不发送模型请求。

```bash
export NF_RUNTIME_PROFILES="$PWD/providers.json"
.venv/bin/python -c 'import os; from agent_runtime.providers import read_profiles; print(sorted(read_profiles(os.environ["NF_RUNTIME_PROFILES"])))'
```

| `kind` | 模型宿主 | 运行条件 |
| --- | --- | --- |
| `deepseek` | 使用 Chat Completions，输出上限传入 `max_tokens` | 默认请求 `https://api.deepseek.com/chat/completions`，凭据由 `secret_ref` 指定。 |
| `openai-compatible` | 使用 Chat Completions，输出上限传入 `max_completion_tokens` | 显式设置 HTTPS `endpoint`。端点须支持模型请求的 JSON 输出及用量格式。 |
| `codex-cli` | 本机 Codex CLI 的 app-server 运行隔离文本回合 | 宿主须安装 `codex`。可选 `api-key` 或 `chatgpt-plan` 鉴权。后者需 `oauth_client_id`，且 `model_output_ceiling` 与 `max_output_tokens` 一致。 |

已有 `codex login` 的 ChatGPT 用户可使用 `auth_mode: "host-login"`。CLI 管理登录与续期，不向 NF 导出访问令牌。该模式创建新的临时只读线程，关闭本机 MCP、插件、Shell 和浏览器工具，再检查服务状态；出现工具请求或动作即拒收结果。`secret_ref` 在此模式中仅作为账号配额命名空间，不需要另存 OpenAI API Key。需将当前模型的完整输出上界同时填入 `model_output_ceiling` 和 `max_output_tokens` 并预留相应 Token；任务内较小的上限不能作为 CLI 的硬截断。调用中断保留原 Attempt，不自动再次发起回合；CLI 内部传输及订阅金额不能冒充逐请求账单。

生产交付会核验该 Attempt 的本机 Host journal 已达 `COMPLETED`，线程/回合、实际模型、任务代际、提示/响应/输入哈希及候选契约完全相符，才允许采用返回结果。采用不结算订阅费用，原 `UNKNOWN` 和预算预留保留。未完成、丢回执、换模型或换回合都不能仅凭声明为成功而交付。调度预算须覆盖输入与整个 CLI 输出上界，且满足派发契约的 500000 单次 Token 上限；当前 Codex 迁移阶段使用 500000，发送前仍会核对实际输入估算，不能沿用小于输出上界的旧实验预算。

可从 [双模型目录](../../configs/codex-deepseek.example.json) 和 [岗位路由](../../configs/codex-deepseek-routes.example.json) 配置后续作品：写作与角色行动使用 DeepSeek，其余规划、设定、审读和编辑使用 Codex。示例的保守输出预留须结合当前账户调整。原三书快照继续使用其冻结的双 DeepSeek 配置，换模型须经过任务重绑，不能改写已开始的调用。

新作品的 P6 双模型 intake 也采用此分工：`creative` 配置供 writer/character_actor 及 evidence_maintainer 的 summary 使用，复杂事实抽取、其余大纲、设定、规划及审核使用 `review` 配置。旧 intake 与已物化作品继续采用其已保存的策略 Artifact，源码默认值变更不重写历史路由。

已返回但因格式反复失败而停止的事实读取，可用 `python scripts/nf.py recover-reading --help` 预览显式策略迁移。管理员提供旧任务代际及当前生产板哈希；`--apply` 在事务中核验原回执、封存旧返工诊断、采用新版路由，为同一份正文登记独立读取任务和新阶段预算。旧 Attempt、原预留及未知账务不改写。此操作不会自动处理仍在发送或丢失结果的请求。

### 输入缓存与上下文

WORLD 切面的事实抽取任务另取得 `FACT_READING_SCOPE`：固定版本、限定主体的实体名称映射与完整状态查询结果，来源绑定到不可变编译产物。UNKNOWN/PENDING 明确保留，不因没有已知事实而只收到 UUID。名称登记不等于事实成立；该视图不投递给 BELIEF 角色或其他岗位。合法 `NEEDS_INPUT` 会保存结果并在生产板显示 `MODEL_INPUT_REQUIRED`，不继续显示空闲，也不自动重发原请求。修补授权材料后，管理员可为同一正文登记独立读取，原结果和账务保留。

网关先完成固定切面、授权裁剪及不可变上下文快照，再组装三条消息：角色/技能/Schema 指令、已授权材料、任务身份与外层响应模板。任务 UUID 不插在固定指令和正文前。材料 JSON 先放正文与结构化证据，最后放具体任务和诊断；值、数组顺序及校验规则不变。Actor 查询后的最终请求保留同一身份尾部。既有 Attempt 的请求哈希与旧回执保留，不能把旧调用按新布局重发。

默认字典型创作材料内部先序列化稳定的场景卡、风格、连续性和事实，再放每版变化的旧稿正文。显式上下文配方用数组声明的次序仍原样保留。A 的两次真实 Flash 返工请求离线比较中，材料公共前缀从 2,539 增至 3,324 个 UTF-8 字节，解析后字段和值完全相同；这是可复用前缀证据，不能当成供应商实测命中 Token。

DeepSeek 自动复用匹配的前缀，命中是供应商的尽力服务，并非本机 Redis 开关。见[官方缓存规则](https://api-docs.deepseek.com/guides/kv_cache/)。Redis/编译缓存减少本地查询，不能直接减少供应商输入账单。前缀调整只提高复用条件；节省以响应的 `prompt_cache_hit_tokens` 为准。不同角色的秘密上下文不拼接共享，不用摘要替代必须完整核查的正文与事实证据。

`scripts/p6_deepseek_costs.py` 按作品和阶段/角色/模型输出 `input_cache_hit_ratio`，计算方式为已测输入命中 Token 总和除以同批输入 Token 总和。未知和无效用量另计 `cache_unknown_calls`，不作为零命中混入平均值；输出 Token 不放在命中率分母。价格估算需固定日期的价目表，不能等同供应商实付账单。高命中率仍不能消除重复审核、全篇改写和失败输出的开销。

`max_output_tokens` 范围为 1–1,000,000，`timeout_seconds` 默认为 60，范围为 1–600。实际模型还受供应商和账户限制。模型标识和纯文本执行能力来自本地配置。加载器会验证整份目录，一项配置格式有误即可阻止启动。加载后，供应商请求失败记入该次 Attempt。宿主在发送前校验凭据、执行能力与授权。真实调用另需 `NF_RUNTIME_LIVE_APPROVED=1`。仅在已获费用授权的运行环境启用。

## 发布、预览和切换

路由规则包含 `id`、选择条件 `when` 和 `profile`。一个策略包含 1–256 条规则，必须有一条 `when: {}` 默认规则。规则 ID 和相同条件的组合均须唯一。以下内容可保存为 `rules.json`。

```json
[
  {"id": "writer", "when": {"role_id": "writer"}, "profile": "draft-model"},
  {"id": "default", "when": {}, "profile": "draft-model"}
]
```

在已初始化的双服务环境中，设定 Workshop 管理者的 `AW_BINDING`、NF 管理者的 `NF_ACCESS` 及其 `WORK_ID`、`BRANCH_ID`。以下命令发布并采用第一个策略，最后一步只查询匹配结果。更新现有策略时使用两端各自的当前预期版本。

```bash
workshop/.venv/bin/python -m agent_workshop.policy_cli --binding "$AW_BINDING" publish --work "$WORK_ID" --branch "$BRANCH_ID" --expected-revision 0 --rules rules.json > policy.json
.venv/bin/python scripts/runtime_policy.py --access-file "$NF_ACCESS" activate --expected-revision 0 --policy policy.json
.venv/bin/python scripts/runtime_policy.py --access-file "$NF_ACCESS" preview --role writer --kind scene_draft
```

获准的显式覆盖优先于规则。其余条件的优先级从高到低依次为任务 ID、实例加任务类型、实例、角色加任务类型、角色、任务类型、分支、作品、组织、安装、默认。策略采用时要求所有引用的配置均已安装。已有任务继续使用冻结的输入。

如需为已登记任务切换模型，管理员可执行 `.venv/bin/python scripts/runtime_policy.py --access-file "$NF_ACCESS" rebind --task "$TASK_ID" --generation "$GENERATION" --key "$OPERATION_KEY"`。操作核验 generation、截止时间、依赖和可重绑状态。它释放已确认未发送的预留，递增 generation，并在下次领取时重建上下文。已发送、终态或结果未知的调用需要先处理原 Attempt。委托、计划以及动态 Agent 或自适应指令绑定需要按新的代际重新授权。

## 并发、费用与未知结果

Supervisor 的进程数控制本机并行度。运行时配额在 PostgreSQL 中跨 Worker 生效，可按供应商、账户、组织、作品或角色限制并发数、每分钟请求数和每分钟预留 Token 数。作品之间的调度还使用项目权重。共享供应商或账户的作品会共用相应配额，另一个供应商走自己的配额。排查延迟时，区分本地 `RUNTIME_CAPACITY_LIMIT`、供应商返回的 HTTP 状态和数据库等待。

每次外部请求记录 Attempt，关联任务、generation、WorldRef、模型配置、请求摘要和预算。下图展示账本状态。预算预留时写入 `PREPARED`，跨越发送边界时写入 `SENT`。

```mermaid
stateDiagram-v2
    [*] --> PREPARED
    PREPARED --> SENT
    PREPARED --> FAILED : 确认未发送
    SENT --> SUCCEEDED : 候选与账单均已核验
    SENT --> FAILED : 已知失败并结算
    SENT --> UNKNOWN : 结算依据不足
    UNKNOWN --> SUCCEEDED : 对账确认成功
    UNKNOWN --> FAILED : 对账确认失败
```

Token 用量、供应商回执和实付金额分别记录。Token 已知但金额缺失、币种或金额越界，以及发送后失去可核对的结果，都可能留下 `UNKNOWN`。原预留继续占用相应预算，同一任务不会自动重发这次调用。其他任务有独立授权且有可用额度时可以继续。运维人员以账单标识和该次请求的证据对账，可用 `.venv/bin/python scripts/p6_reconcile_invoice.py --help` 查看入口。合成适配器验收记录、真实网络调用记录和供应商账单可分别通过测试结果、调用审计和账本核查。

[配置与宿主协议](../../agent_runtime/providers.py) · [路由优先级](../../agent_runtime/routing.py) · [模型切换](../../src/novel_factory/application/model_routing.py) · [费用登记](../../src/novel_factory/application/agent_gateway.py)
