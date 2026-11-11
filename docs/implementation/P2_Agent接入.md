# P2-03：受限外部 Agent 接入与调用证据

## 范围与状态

实现一个可配置的、无工具、无跨任务会话的 HTTP 模型路线，以及一个可插拔的受信 Host 适配端口。应用层复用 P1 PostgreSQL 任务、租约代际、预算、调用账与不可变对象；不引入另一套世界数据库，不给模型世界接受、正文接受或发布权限。

`tests/unit/test_agent_adapter_protocol.py` 的 HTTP 服务是本机协议夹具，`tests/integration/test_agent_gateway.py` 的模型是明确标记 `TEST_FIXTURE` 的确定性适配器。它们验证真实 HTTP/数据库边界，**不属于真实模型生产验收，也不提供文学质量证据**。本轮真实 Host 是否成功，以主开发报告及原始 Host 日志为准；安装了 CLI、存在登录状态、配置了适配器均不代表模型调用成功。

## 服务与持久化

| 组件 | 责任 |
|---|---|
| `RuntimeCatalog` | 从 runtime catalog 选择一个合法角色模式、一个主技能及受限模式；不载入开发 AGENTS/技能，不把 requested_tools 变成权限 |
| `AgentGateway.prepare_input` | 由 ADMIN 控制面冻结运行时原文、内容哈希、模型配置哈希、输出契约、任务目标及材料引用，返回可用于 TaskSpec 的输入 |
| `AgentGateway.execute` | 复查任务和认知切面、读取获准材料、预留预算、持久 CAS 派发、校验输出、存证及提交任务候选 |
| `OpenAICompatibleAdapter` | 一次 HTTP 请求，明确配置 endpoint/model/key，无工具列表、历史会话、自动重试或重定向 |
| `TrustedHostAdapter` | 调用受信 Host 回调，要求响应绑定请求哈希；默认 `UNVERIFIED_HOST`，不能用于角色认知隔离验收 |
| `BoundedAgentWorker` | 单进程有限 worker 和有限待处理批次；不是跨进程供应商全局并发配额 |

新增的 `AGENT_RUNTIME`、`AGENT_INPUT`、`AGENT_PROMPT`、`AGENT_RESPONSE`、`AGENT_CANDIDATE`、`AGENT_CALL_EVIDENCE` 和 `AGENT_BLOCKED` 产物均在现有 `nf_artifacts` / 对象存储保存。预算和调用仍写现有 `nf_budgets` / `nf_call_attempts`。任务结果写 `nf_task_results`；接受回执写既有 receipts 表。无需新建独立 Agent 状态库。

调用审计记录包含 task/generation、角色和 actor、WorldRef、context ID、输入/运行时/模型配置、请求哈希、提示和原始响应引用、候选引用、状态/错误、用量、成本确定性及耗时。失败和迟到的已返回字节保留为证据；它们不自动进入当前任务结果。

## 任务交接顺序

1. 控制面选择受限角色、模式、主技能和模型路由，调用 `prepare_input`。
2. 创建现有 `TaskSpec`，把返回的 `input_ref` 放入 `input_artifacts`，把 `runtime_contract_version` 设置为返回的精确快照哈希。世界/故事时间由 TaskSpec 固定。
3. 使用该角色自己的 grant 领取任务并通过 `context_compile` 编译当前代际；不能用总编或管理员的上下文替代角色上下文。
4. 创建/选择已有 work 预算，发送 `AgentDispatchRequest`。请求只能引用这代任务已登记的 input_ref，不能自报新的角色、actor、世界版本、输出 Schema 或 endpoint。
5. 模型得到角色/技能快照、固定身份、精确输出 Schema、当前 ContextBundle、read cut 和逐条获准的全文材料。没有来自旧任务的聊天记录、开发者文件或 shell 工具。
6. 模型返回 `AgentModelResponse`；外层身份逐字段匹配，payload 再按 AgentInput 指定的精确 Schema 验证。ActionProposal 等重复携带的 actor/世界/代际也再次核验。
7. 成功结果仅为 `VALIDATED_CANDIDATE`。角色行动交联合裁决；写稿交对账/审核；评审建议交编辑决定。该服务从不调用 `Kernel.accept`。

同一代任务已经保存 runtime 原文，之后部署的新技能不改变旧任务。模型配置变更后旧 input 不静默换模型，返回 `TASK_REBIND_REQUIRED`。材料清单不授予读取权限：即使管理员把某私有 artifact 列入 AgentInput，执行时仍由 `Kernel.get_artifact` 核验当前任务/代际/认知切面；未获准材料阻断派发。

计划同步门禁 `validate_task_plan_binding` 在派发准备、SENT 事务、结果保存和回执重放资格检查中运行。计划前提在世界中已被破坏时，不等待异步重规划 worker 才停止旧输入。UNKNOWN、撤权、取消、租约失效都不被“任务已获得一次授权”绕过。

## 网络、重试与计费

HTTP 配置来自服务端环境，不从模型文本或 dispatch JSON 取 URL/API key：

```text
NF_MODEL_ENDPOINT=https://<authorized-provider>/v1/chat/completions
NF_MODEL_NAME=<explicit-compatible-model>
NF_MODEL_API_KEY=<provided-through-authorized-secret-configuration>
NF_MODEL_MAX_OUTPUT_TOKENS=2048
NF_MODEL_TIMEOUT_SECONDS=60
```

不设置默认模型或默认价格。缺少 endpoint/model/key 返回 `MODEL_UNAVAILABLE`；模型请求未发出、预算未扣，不调用其它账户或寻找替代秘密。生产 endpoint 要求 HTTPS，拒绝带 userinfo、query 或 fragment 的凭据 URL；明示允许的 loopback HTTP 仅用于可控本机测试。HTTP 不跟随重定向，避免任务内容和凭据被转发到另一来源。

本适配器使用非流式 Chat Completions 形状，包括 messages、model、max_completion_tokens、JSON response_format；所选供应商和模型必须明确支持这个方言。字段依据：[OpenAI Chat API reference](https://developers.openai.com/api/reference/resources/chat)，核查日期 2026-11-10。该选择不宣称所有供应商/模型都兼容；没有在当前环境完成的真实供应商运行不能标通过。

`reserve_call` 先预留任务/work 的金额与 token，`mark_call_sent` 在短事务核验资格并执行 PREPARED→SENT CAS。仅获得首次转换资格的 worker 发网络请求；另一个相同 call_key 返回已派发/待核对。网关授权回调同时锁定该 task 行，检查全部代际的 PREPARED/SENT/UNKNOWN；两个不同 call_key 即使同时通过事务外预检查，也不能并发各自发一笔调用。事务结束后才进入网络等待。

| 实际情况 | 结果与账务 |
|---|---|
| 缺凭据，未发请求 | `BLOCKED` / `NOT_SENT`，有缺环境证据，无调用收费假设 |
| 明确响应、有效候选、可靠金额和 token 均齐全 | 保存候选与原始返回，实际金额/使用量结算，释放多余预留 |
| 明确响应但没有真实金额 | 候选可保存；`UNKNOWN_COST`，`actual_amount=null`；调用账保持 UNKNOWN，保留金额预留。实际 token 单独保存在调用证据 |
| 网络超时、断连、状态未知 | `BLOCKED`，保留 UNKNOWN 及预留，不自动重试；换 call_key 也不能绕过同任务未决调用 |
| 响应不符 Schema / 错 actor | 原始返回与原因存证，任务失败，不产生候选或正式世界效果；实际已知费用仍记账 |
| 模型执行期间撤权/取消 | 可保留审计字节和已发生费用；当前结果交付/采用被阻断 |
| 相同请求重试且原结果已持久化 | 复核当前权限，重取原回执/恢复保存结果，不再调用模型 |
| SENT 后崩溃且尚无持久响应 | 保持结果未知；不得假定未收费、不得重新生成原历史 |

UNKNOWN_COST 不表示零费用。HTTP token usage 也不是美元账单；模型订阅 Host 没有 API 账单时同样不能填入猜测的价格。新的真实供应商费用核对可使用原 TaskService.settle_call，必须保留外部账单依据。已知实际金额/token 超过预留时，不悄悄突破上限，仍保留未结算状态等待明确核对。

输入预算用 UTF-8 字节量加输出上限作保守准备检查，不宣称它是供应商 tokenizer；实际提供方用量另存。NF-CANON-1 不允许浮点或 tuple：持久提示使用 JSON 列表，耗时和配置小数使用十进制字符串，供应商原始 JSON 作为原文字符串存证，不修改其数值含义。

## 并发、恢复及已知边界

`BoundedAgentWorker(max_workers=1, max_pending=16)` 是保守的单进程部署入口。允许显式增大到有限配置，但本轮没有实现跨进程供应商并发租约；多个进程各有一个 semaphore/线程池不等于总量受控。总模型进程数必须作为部署预算明确限制，不能据此宣称支持 200 个真实模型同时在途。50/200 逻辑 Agent 服务负载与真实模型并发是不同验收项。

同一个 call_key 的派发唯一性依靠 PostgreSQL 调用记录 CAS，跨进程仍成立。未知调用的预算保留、同任务阻断和租约恢复器是持久状态；网关不因进程重启忘记已经发过请求。HTTP 请求可能在 SENT 记录完成后、真正送出前遇到崩溃，这种保守窗口也按 UNKNOWN 处理。

网关不会恢复一个失效代际的执行资格。当前实现的未知调用保持逻辑阻断，租约回收器随后按现有机制将该任务转入 PAUSED。作者选择继续时应先对账，再明确生成新的代际/尝试；不自动撤销已经接受的子事件。

Host 回调接口可以服务已授权的无工具 Host，但 `isolation_evidence` 必须引用真实工具清单/探针。它不是密码学隔离证明，配置字符串本身不能完成 V2-11 验收。开发子 Agent 继承的全仓库/全知上下文不适合作为隔离角色模型。无隔离证据的 Host 对角色任务直接拒绝，写作/编辑任务也只能报告其实际可见范围。

## 验证与复现

控制面准备、领取、编译好任务后，可通过薄 CLI 执行对应的外部调用：

```bash
uv run python scripts/run_agent.py \
  --access-file /private/agent-access.json \
  --request /private/agent-dispatch.json \
  --adapter-id external
```

access 文件只提供已签发的任务身份；dispatch 文件必须符合 AgentDispatchRequest，并匹配准备时的 adapter ID 和配置哈希。凭据文件保留在私有路径，不提交仓库。该入口只调用已有任务，不自动签发权限、推进世界或发布。

```bash
uv run python -m pytest tests/unit/test_agent_adapter_protocol.py -q
uv run python scripts/with_postgres.py --migrate -- \
  .venv/bin/python -m pytest tests/integration/test_agent_gateway.py -q
```

具体覆盖：运行时模式/技能限制、路径边界、Schema 与重复键、缺环境阻断、真实本机 HTTP 请求形状、Host 默认未隔离、批次队列上限；PostgreSQL 成功重放、未知费用、超时/换 key、嵌套错误 actor、取消/撤权期间返回、不持连接等待、私有材料旁路、当前代际、旧 runtime 保留、同 key 与不同 key 的实际并发仅派发一次、原始失败响应归档。正常世界状态和效果计数始终由独立查询确认。

本次测试原始记录放在 `evidence/p2/batch1/gateway/`。早期运行发现 canonical tuple/float 不合法以及 pytest 同名测试模块收集冲突；保留失败 session/collection 证据，修复实现和文件名，未弱化权限、失败断言或世界约束。2026-11-10 在 Python 3.12 / PostgreSQL 17.11 的局部合并运行中，8 项单元/协议检查及 14 项 PostgreSQL 集成/并发检查共 **22 项通过**。这不替代全库回归或 GitHub CI，也不是 22 次真实模型验收。
