# 通过 stdio 接入 Codex

安装仓库锁定的 Python 3.12 依赖，升级到当前迁移。数据库与对象目录都在服务端。模板以 POSIX/Linux（含 WSL）为运行环境，使用 Python MCP SDK 1.29.0。每条 MCP 连接固定一个服务端身份；每次请求都会重验数据库中的授权。

## 给作者配置「继续未完成的任务」

1. 在独立运行账户下，通过服务端私有配置取得 `NF_DATABASE_URL`。该账户应能读取对应对象目录。VS Code 所在账户仅持有下述受限连接凭据。配置数据库认证和文件系统权限，使编辑器账户无法直接打开对象、绑定文件或登录 PostgreSQL。回环 trust 是隔离测试数据库的专用配置。
2. 管理员在服务端运行以下命令，按提示交互输入该项目的管理员令牌。命令创建独立的恢复授权，将 grant ID 输出给管理员，并写入仅服务端账户可读的绑定文件。授权限定一个作品、一个分支、签发时的策略版本及到期时间。

   ```bash
   uv sync --locked
   uv run --no-sync python -m alembic upgrade head
   uv run --no-sync python scripts/provision_resume.py \
     --work-id "$WORK_ID" --branch-id "$BRANCH_ID" --hours 24 \
     --object-root /var/lib/novelfactory/objects \
     --output /var/lib/novelfactory/private/resume.json
   ```

3. 将专用公钥登记在该服务账户的 SSH 授权配置，启用 OpenSSH 的强制命令与 `restrict`。下面是替换绝对路径与公钥后的格式。将服务代码和强制命令配置置于编辑器账户的写权限之外。服务账户只授予必要数据库、对象和程序权限。

   ```
   restrict,command="/srv/novelfactory/.venv/bin/python -m novel_factory.interfaces.mcp_stdio --binding /var/lib/novelfactory/private/resume.json" ssh-ed25519 <public-key>
   ```

4. 在作者账户的 SSH 配置中将 `nf-resume-service` 绑定到已核实的服务主机、上述账户及专用私钥，设置 `IdentitiesOnly yes`。将同目录 `p5-mcp.toml` 中的条目加入 Codex 配置，重新加载 VS Code 扩展。在该作品的受信项目内输入“继续未完成的任务”。恢复工具负责一次数据库扫描。实际模型工作由独立启动的 Supervisor 领取已登记的工作，详见总运行指南。
5. 恢复授权到期、撤销或策略版本改变后，服务会拒绝调用。管理员通过现有 `kernel.revoke_grant(admin, grant_id)` 撤销旧授权，按需重新签发独立绑定。

模板给 `nf_resume_project` 配置预授权。此前在扩展里使用相同的项目权限但省略此工具的 `approval_mode` 时，作者界面要求单独确认。服务端仍逐次验证本作品、分支、策略、权限与到期时间。

主机应只看到 `nf_project_status` 与 `nf_resume_project`，两个工具的输入都为 `{}`。前者返回当前任务计数。后者重验就绪与等待条件，返回恢复及唤醒计数。传输断开后再次调用会从数据库恢复。

## 在隔离测试库直连

配置中的 `command` 可以使用项目虚拟环境的 Python 绝对路径，`args` 为 `["-m","novel_factory.interfaces.mcp_stdio","--binding","/private/resume.json"]`。同一 OS 用户能直接读取绑定内的令牌与数据库 DSN，直连用于受信测试进程。它不能作为有 shell 的角色 Host 的权限隔离证据。

在已签发绑定的环境运行 `uv run --no-sync python scripts/probe_codex_mcp.py --binding /private/resume.json`。探针创建临时的 Codex 配置与空项目，通过本机 app-server 获取实际工具列表并调用恢复。JSON 收据只包含 Host 版本、线程、工具名和计数。探针不发起模型轮次。VS Code 扩展验收另须在扩展界面检查可用工具并发出恢复调用。

## 任务身份

任务绑定文件采用同一个顶层结构，`binding` 使用控制器提供的 `task_id`、`generation`、`worker_id`，其 `token` 是该任务独立的服务端 grant。普通模型只能经受限通道调用工具，或由无 shell Gateway 传递固定上下文。绑定文件、数据库连接以及任何其他任务的密钥须留在受保护的服务端，不进入模型环境。

- `nf_task_get({})` 和 `nf_context_get({})` 读取当前租约与上下文。
- `nf_query_batch({"lookups":[{"subject_id":"<uuid>","predicate":"owner"}]})` 在已编译清单中查询状态，认知平面、角色、时间与世界版本都从当前任务绑定派生。
- `nf_artifact_put({"content":"我决定不赴宴。","idempotency_key":"scene-2-intent"})` 保存候选文本并返回对象引用。同一任务代际重放同一键和内容会取得同一收据，改动内容会触发幂等冲突。联合裁决、审批和世界接受继续经各自应用服务执行。

## 在作者自己的 VS Code 复验扩展

VS Code 1.145.3 的独立 web 工作台实际加载了 OpenAI Codex 扩展 26.51106.51428。在最初的离线试验中，侧栏因为不能加载远程 webview 静态文件而超时。复验将同一 VS Code 发行包内的三个静态文件按原 URL 映射到受隔离的本机回环 HTTPS，使用专用测试凭据与 PostgreSQL 17.11。扩展、app-server 和 MCP 服务均执行真实代码。测试模型通过本地 Responses 端点产生工具请求。作者在扩展中输入“继续未完成的任务”，扩展进程所启动的 app-server 随即列出两个工具并调用 `nf_resume_project`。被动的双向 stdio 记录和数据库返回保存在[扩展验收证据](../../evidence/p5/mcp_vscode_extension.json)。下列步骤可在普通联网工作台复验。

1. 在服务端选择独立测试作品，按 [P5-03 运行说明](../../reports/P5-03.md) 创建两任务样例与私有配置。首次验证可先只初始化样例。Supervisor 停止时，恢复调用仍能独立验证数据库就绪与等待计数。
2. 在作者自己的桌面 VS Code 使用隔离测试配置，安装或启用官方 `openai.chatgpt` 扩展。记录「帮助 → 关于」中的 VS Code 版本和扩展详情中的版本号。按本文的 SSH 配置接入服务，打开该项目并只信任自己创建的测试目录。在命令面板执行 `Open Codex Sidebar`，确认侧栏能够加载。运行命令前关闭同一测试服务器上的其他 MCP 客户端，便于将服务侧记录与本次扩展操作对应。
3. 在扩展新对话中输入 **继续未完成的任务**。展开扩展界面中的工具调用卡片，核对服务器 `novelfactory`、`nf_resume_project`、空参数和返回计数。再次输入相同请求，并记录第二份回执。任务可能在两次扫描之间自然推进；检查已接受世界效果与模型调用账本能够区分重复调用和正常进展。只保存测试作品的信息。
4. 若需逐帧证明扩展进行了工具发现，可在此次测试的服务端强制命令使用下方薄包装器。对比私有记录与扩展中同一时间段的工具卡片。运行前确认端点已签发专用 `resume_operator` grant。服务端按两个方向过滤，只记录协议名、工具名和恢复计数。每次验收使用新私有目录，结束后还原原有强制命令。

   ```bash
   #!/usr/bin/env bash
   set -euo pipefail
   umask 077
   audit=$(mktemp -d /var/lib/novelfactory/private/ide-proof.XXXXXX)
   tee >(jq -c --unbuffered '
     select(.method == "initialize" or .method == "tools/list"
            or (.method == "tools/call" and .params.name == "nf_resume_project")) |
     {method, tool: (.params.name // null)}' > "$audit/requests.jsonl") |
     /srv/novelfactory/.venv/bin/python -m novel_factory.interfaces.mcp_stdio \
       --binding /var/lib/novelfactory/private/resume.json |
     tee >(jq -c --unbuffered '
       if .result.serverInfo.name? == "NovelFactory" then
         {server: .result.serverInfo.name, protocol: .result.protocolVersion}
       elif (.result.tools? | type) == "array" then {tools: [.result.tools[].name]}
       elif (.result.structuredContent.counts? | type) == "object" then
         {counts:.result.structuredContent.counts, awakened:.result.structuredContent.awakened,
          recovered:.result.structuredContent.recovered, isError:.result.isError}
       else empty end' > "$audit/responses.jsonl")
   ```

5. 启动独立 Supervisor，检查服务端两任务先后出现不同的 thread/turn ID，并在两次执行间出现持久的等待与唤醒。关闭侧栏只结束作者界面。Supervisor、PostgreSQL 和模型端点是否可用分别决定后续工作能否推进。

角色 Host 的权限还需要独立验证。可以向测试作品植入只在角色 B 认知和作者计划中出现的两段合成文本，用角色 A 的派发记录核对上游实际收到的消息中没有这两段内容。同一 OS 账户的编辑器如果能读取私有绑定、对象目录或 Supervisor 的凭据，MCP 返回拒绝仍不足以证明该编辑器具备角色隔离能力。该部署应让角色继续使用无 shell 的单次文本 Gateway。切换为有 shell 的角色 Host 前，要在真实的角色账户与运行环境中验证上述私有资源的文件系统、数据库和工具访问限制。

### 使用固定版本的离线工具请求复验

验证端采用 Python 3.12、PostgreSQL 17.11、VS Code Server 1.145.3、发布版 Linux x64 `openai.chatgpt` 26.51106.51428 和 Codex CLI 0.167.0-alpha.4。为试验单独建立 HOME、CODEX_HOME、扩展目录、浏览器目录、测试 PostgreSQL 和作品。将这些进程放在同一仅含回环的 Linux 网络命名空间内。可以用 `unshare --user --map-current-user --net --keep-caps` 启动子环境，在里面启用 lo，检查 `/proc/net/dev` 只有 lo。启用 VS Code 的 `--telemetry-level off`、`telemetry.telemetryLevel=off` 和 Codex 的 `[analytics] enabled=false`。将测试工作区设为可信并将 `chatgpt.cliExecutable` 设为测试 CLI 的绝对路径。

1. 从该 VS Code Server 安装目录取 `out/vs/workbench/contrib/webview/browser/pre`。为本次测试生成只用于回环的临时 TLS 证书，保留私钥在测试账户内。在命名空间内运行下面的服务。它只向浏览器供应 `index.html`、`service-worker.js`、`fake.html`，在另一回环端口模拟工具调用模型。MCP 使用独立的真实 stdio 服务。

   ```bash
   umask 077
   openssl req -x509 -newkey rsa:2048 -nodes -days 1 \
     -keyout loopback.key -out loopback.pem -subj '/CN=offline-vscode-webview' \
     -addext 'subjectAltName=DNS:*.vscode-cdn.net'
   .venv/bin/python scripts/probe_vscode_offline_transport.py serve \
     --static-dir "$VSCODE_SERVER/out/vs/workbench/contrib/webview/browser/pre" \
     --cert loopback.pem --key loopback.key
   ```

2. 为这一次的浏览器配置 `--host-resolver-rules=MAP *.vscode-cdn.net 127.0.0.1:31185`、`--no-proxy-server` 及 `--ignore-certificate-errors-spki-list=<测试证书公钥的 SHA256 Base64>`。证书指纹可由 `openssl x509 -pubkey -noout -in loopback.pem | openssl pkey -pubin -outform DER | openssl dgst -sha256 -binary | openssl base64 -A` 取得。访问回环地址上的正式工作台，打开相应测试目录和已安装的扩展。Chromium 使用 webview 的原有隔离源和 service worker。
3. 在这一次的测试 CODEX_HOME 中选用下面的模型路由。在同一文件中配置实际项目绑定，把 MCP 的 `command` 设为项目虚拟环境 Python，`args` 设为 `["/repo/scripts/probe_vscode_offline_transport.py","capture","--record-dir","/private/new-trace-dir","--binding","/private/resume.json"]`。先创建仅当前账户可读的记录目录。保留模板中对 `nf_resume_project` 的单工具预授权。捕获器对每条连接逐行原样转发，在不同文件中保留方法名、工具名及数字计数。

   ```toml
   model = "nf-ui-test"
   model_provider = "nf_ui"
   [model_providers.nf_ui]
   name = "Offline test"
   base_url = "http://127.0.0.1:31186/v1"
   wire_api = "responses"
   supports_websockets = false
   requires_openai_auth = false
   request_max_retries = 0
   stream_max_retries = 0
   ```

4. 在真实侧栏里连续两次输入“继续未完成的任务”。如需复现唤醒，预先建立等待 ACCEPTED 前置任务的 BLOCKED 工单，正式接受前置结果，在操作侧栏前停止其他恢复扫描。检查服务侧两份 `receipt.awakened` 先为 1、再为 0，且计数与 PostgreSQL 一致。试验用定序模型只选择回环工作台实际公开的项目恢复工具。外部模型推理质量、正式 SSH 跨账户部署和角色 Host 隔离需另行验证。

### 恢复回执与诊断

`nf_project_status({})` 只读，`nf_resume_project({})` 在同一授权分支执行有界对账。两者返回 `counts`、`workflow_state`、`ready_count`、`blockers`、`pending_review_count`、`pending_author_approval_count` 和恢复授权的 `resume_grant_expires_at`。恢复另返回本次 `awakened`、`recovered`、`closed_waits`。READY 是数据库任务数，可能包含未登记给模型的控制任务；RUNNING 是有效工作状态，统计本身不能证明服务进程存活或供应商正在计费。blockers 只含分支内的状态和数量。

人工数只计当前代际有明确 `AUTHOR_DECISION_REQUIRED` 交付回执且仍待审的任务。旧版待审记录有些没有路由标记，会计入待审总数。凭据过期或撤权会使原会话失去工具；由管理端重新签发并替换服务侧绑定。用户端继续使用空参数的恢复命令。

登记了屏障的任务遇到自己的截止时间、AND 中一个永久关闭的前置或 OR 中最后一个关闭的前置时，进入 FAILED。工作流以 `workflow.wait.closed` 记录本代的 WAIT_DEADLINE 或 PREREQUISITE_CLOSED，保留原意图和世界历史。尚有未对账调用的任务保留既有围栏。授权管理员可据此延期、重新规划或采用新任务；连续重试恢复不会替角色行动。

## Linux 隔离角色 Host

需要从受信控制器通过 Codex app-server 调用某个执行中角色的 MCP 工具时，可以使用单任务入口。服务账户准备已经领取、已编译上下文的角色私有绑定，安装可用的 util-linux、bubblewrap、静态链接的原生 Codex CLI，以及本项目的静态转发程序。转发程序和服务代码由管理员部署。

```bash
cc -O2 -Wall -Wextra -Werror -static adapters/codex/role_relay.c -o /srv/novelfactory/bin/role-mcp
.venv/bin/python scripts/run_role_host.py \
  --binding /var/lib/novelfactory/private/actor-task.json \
  --relay /srv/novelfactory/bin/role-mcp --codex /srv/novelfactory/bin/codex --lifetime 90
```

控制器使用标准 app-server stdio RPC 连接此进程，在 `/work` 创建临时 Thread，用 `mcpServerStatus/list` 和 `mcpServer/tool/call` 访问 `novelfactory`。stdout 包含角色获准的正文，应交给该角色的控制器。一次启动服务一个任务和一个客户端。TaskBinding、DSN 和对象库只在容器外的 MCP 服务中使用。容器内可见两个静态程序和专用的双向 FIFO，工作目录为临时文件系统，网络与服务账户的本机回环隔离。服务端逐次检查当前 grant、代际和租约。角色传输最多接收 128 条消息，每条至多 1 MiB，进程有显式截止时间。命名空间、租约或程序配置不可用时启动失败。

该入口已经用于角色工具发现、读取、候选提交和撤权验证。生产 Supervisor 的模型路由另见 [P5-03](../../reports/P5-03.md)。主动模型工具循环仍需要将每次供应商请求接入预算账本；本入口不配置外部模型路由。详细验收与复验命令见[角色隔离验收](../../reports/P5_MCP与Codex角色隔离验收.md)。

### 在 Supervisor 的角色派发中读取 MCP 上下文

服务启动前设置 `NF_ROLE_MCP_RELAY` 为上面编译的转发程序绝对路径。登记新的任务或场景时，选择 `adapter_id: codex-role-mcp`。该路由在角色仍持有租约时由隔离 Codex Thread 发起一次 `nf_context_get`。只有角色授权的工具回执与冻结的 ContextBundle 完全相符，才预留并发出原先的一次模型请求。工具回执没有单独追加到模型输入。生成候选后原始响应审计保留 MCP Thread 标识、上下文哈希及模型 Thread/Turn 标识。工具阶段的决策由服务固定，模型的单次文本返回仍然只提交候选。

### 允许角色自己选择一次查询

设置同一服务端 `NF_ROLE_MCP_RELAY`，在新任务或新场景的登记中显式选择 `adapter_id: codex-role-tools`。角色首先在独立、可审计的模型轮次中阅读自己的 BELIEF 和任务要求，返回不查询或一次 `nf_query_batch`（一条 subject/predicate）的选择。控制器验证选择，经真实隔离 Codex MCP Thread 发出请求。合法读取结果出现在第二个模型轮次的行动提案输入中。其他岗位仍按其固定单次文本请求执行。

每个角色的两次请求各有独立幂等键、预留、调用回执和 Thread/Turn。两步总额受原登记额度限制，货币额均分，第一步向下截到百万分之一，第二步使用余量。第一步按输入字节上界和最多 256 输出 Token 预留，余额给最终行动。一次读取的观察至多 32 KiB；超出最终预算会阻断发送。请求结果未知、无可核验费用、权限变动或无效选择时停止。同代有效租约恢复时复用已持久的成功选择。

本路由接收具备供应商回执、货币、实际费用及 Token 数的适配器。对官方 OpenRouter 的精确 HTTPS Chat Completions 端点，服务读取响应 `usage.cost` 及 `gen-…` 生成 ID，原始 JSON 金额可由六位小数账本精确表达时才自动入账。供应商单请求回执可与账号活动另行核对。本次没有对真实账单进行验证。普通兼容端点仅返回 Token、没有确定费用，或费用精度超过账本表示能力时，保持 UNKNOWN 并等待对账。有关费用字段的语义见[供应商使用核算](https://openrouter.ai/docs/cookbook/administration/usage-accounting)和[官方 USD 说明](https://openrouter.ai/blog/insights/cost-vs-quality-tradeoff-framework-for-agent-models/)。

首次设置作者工作区时，在已配置并验证服务端强制命令的 SSH 别名之后，运行 `python scripts/install_p5_project.py --project /小说项目路径 --ssh-alias nf-resume-service`。命令把本仓库的双工具配置写入该项目的 `.codex/config.toml`，保留其他服务器和设置；重复执行显示 ALREADY_CONFIGURED。若同名工具已连接其他服务，它会报告冲突。该步骤可在独立的作者账户执行，无需数据库连接。重新加载受信任项目后，使用 Codex 的 MCP 清单核对服务器和恢复工具。

## P5.5 服务端传输

作者的恢复工具继续使用已有项目绑定，角色任务仍沿用冻结的 grant、知识切面和 Host 配置。Redis 由服务端的 Relay、接收进程和项目执行进程使用；作者和角色的 MCP 配置只需要各自的受限连接。后台进程的安装、开启与回退见[安装与运维](../../docs/implementation/P5.5_安装与运维.md)。从 PostgreSQL 模式迁移的项目先安装所有新版进程，再对选定作品和分支启用 Redis 调度。
