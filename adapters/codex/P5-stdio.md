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
