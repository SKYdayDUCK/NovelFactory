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

主机应只看到 `nf_project_status` 与 `nf_resume_project`，两个工具的输入都为 `{}`。前者返回当前任务计数。后者重验就绪与等待条件，返回恢复及唤醒计数。传输断开后再次调用会从数据库恢复。

## 在隔离测试库直连

配置中的 `command` 可以使用项目虚拟环境的 Python 绝对路径，`args` 为 `["-m","novel_factory.interfaces.mcp_stdio","--binding","/private/resume.json"]`。同一 OS 用户能直接读取绑定内的令牌与数据库 DSN，直连用于受信测试进程。它不能作为有 shell 的角色 Host 的权限隔离证据。

在已签发绑定的环境运行 `uv run --no-sync python scripts/probe_codex_mcp.py --binding /private/resume.json`。探针创建临时的 Codex 配置与空项目，通过本机 app-server 获取实际工具列表并调用恢复。JSON 收据只包含 Host 版本、线程、工具名和计数。探针不发起模型轮次。VS Code 扩展验收另须在扩展界面检查可用工具并发出恢复调用。

## 任务身份

任务绑定文件采用同一个顶层结构，`binding` 使用控制器提供的 `task_id`、`generation`、`worker_id`，其 `token` 是该任务独立的服务端 grant。普通模型只能经受限通道调用工具，或由无 shell Gateway 传递固定上下文。绑定文件、数据库连接以及任何其他任务的密钥须留在受保护的服务端，不进入模型环境。

- `nf_task_get({})` 和 `nf_context_get({})` 读取当前租约与上下文。
- `nf_query_batch({"lookups":[{"subject_id":"<uuid>","predicate":"owner"}]})` 在已编译清单中查询状态，认知平面、角色、时间与世界版本都从当前任务绑定派生。
- `nf_artifact_put({"content":"我决定不赴宴。","idempotency_key":"scene-2-intent"})` 保存候选文本并返回对象引用。同一任务代际重放同一键和内容会取得同一收据，改动内容会触发幂等冲突。联合裁决、审批和世界接受继续经各自应用服务执行。

## 在作者自己的 VS Code 补验扩展

本仓库的 SDK 与 `probe_codex_mcp.py` 已分别取得真实 stdio、Codex app-server 的恢复回执。隔离环境中的 VS Code 已激活 OpenAI 扩展，并收到了它启动的 app-server 的初始化响应。该环境中的 Codex 侧栏渲染超时，仍缺一份由扩展发起的 `tools/list` 和恢复工具回执。可以在作者日常可用的桌面 VS Code 完成以下步骤。

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
