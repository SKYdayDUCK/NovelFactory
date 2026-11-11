# 通过受限 MCP 委托一个 NovelFactory 任务

NovelFactory 管理员在本机授权一次安装和有限数量的任务。每个范围引用对应一个未使用的任务定义。源 `WorldRef`、角色、人物、时间、输入、预算、模型路由均由 NF 锁定。发行安装凭据与预备运行时输入都不会创建可运行任务。安装凭据只能交给 NF 侧 stdio 服务，Workshop 客户端只访问标准 MCP 输入输出。部署时以不同的系统账户管理两侧服务和数据库。

## 安装与授权

在 NF 环境执行 `uv sync --locked` 和 `uv run alembic upgrade head`。在 NF 服务账户的私有目录中准备一个属主可读写的 `admin.json`，包含 `database_url`、绝对 `object_root`、`token`、`work_id` 和 `branch_id`。`token` 是 NF 已存在的管理员授权。以下 UUID 与截止时间取自实际的组织安装申请和授权。

```sh
uv run python scripts/manage_workshop_binding.py --access-file /srv/nf/private/admin.json install \
  --org-id "$ORG_ID" --nf-instance-id "$NF_INSTANCE_ID" --expires-at "$EXPIRES_AT" \
  --binding-output /srv/nf/private/workshop.json
```

标准输出仅包含 `installation_id` 和当前 `grant_epoch`。新建文件使用 `0600` 权限，启动 NF MCP 时直接引用该文件。注册 slot 的命令读取另一份属主专用的 JSON。`definition` 接受 `RuntimeTaskService.define` 的参数，包括 `world_ref`、`story_time`、`role_id`、`task_kind`、`subject_ids`、`predicates`、`assignment`、`adapter_id`、`max_amount`、`token_budget`。人物角色另需 `actor_id`。该文件的顶层还须含 `scope_ref`、`task_template_id`、已有 NF `budget_id`、`reserve_amount` 和 `token_reservation`。金额和 Token 上限须分别与定义相等。`accept_summary` 默认关闭，只能对已授权的证据摘要开启。CLI 使用 NF 运行时的模型路由配置，注册过程不发送推理请求。

```sh
uv run python scripts/manage_workshop_binding.py --access-file /srv/nf/private/admin.json slot \
  --installation-id "$INSTALLATION_ID" --input /srv/nf/private/slot.json
uv run python -m novel_factory.interfaces.delegation_stdio --binding /srv/nf/private/workshop.json
```

## 提交、确认与费用

通过 `tools/list` 枚举 `nf_delegation_submit`、`nf_delegation_get` 及其 JSON Schema。版本为 `aw-nf.v1`。安装固定作品、分支、组织和实例。`operation_id`、`budget_reservation_ref` 为稳定 UUID；后者每个安装只能消费一次。首次提交对同一 PostgreSQL 事务中的授权、任务、调度许可、业务回执和 Outbox 生效。调度沿用现有 Supervisor。相同 operation 重放时可读到最新代际和状态；同键异内容或用其他操作重复消费同一 slot 会被拒绝。

NF 已提交但回应丢失时，客户端保持组织侧的费用预留，用原 `operation_id` 查询。`get` 返回 `receipt_ref`、`task_id`、`world_ref`、`generation`、`task_state`、`accounting_state`、币种及每个 attempt 的状态、金额和 Token，适于按 attempt 标识去重。`actual_amount` 与 `actual_tokens` 汇总的是已入账部分。某条明细字段为 null 表示该数值尚未结算。任务可以已经 `ACCEPTED`，而外部费用仍 `UNKNOWN`。两侧不得据此释放预留或重新发起模型调用。

## 恢复和撤权

物理或逻辑恢复后在启动服务前照常执行 NF 恢复围栏。此步骤还会冻结所有安装的新委托。既有回执可查。由授权管理员在核对 Workshop 的所有待处理与已绑定操作后，提供完整的 `{operation_id: receipt_ref}` JSON。两侧回执集合不同会阻止解冻。缺失的对端操作必须先对账，再恢复外发。撤销安装同时撤销所有所属 slot 的角色授权，历史回执和已发生费用保存在 NF。

```sh
uv run python scripts/manage_workshop_binding.py --access-file /srv/nf/private/admin.json reconcile \
  --installation-id "$INSTALLATION_ID" --input /srv/nf/private/verified-receipts.json
uv run python scripts/manage_workshop_binding.py --access-file /srv/nf/private/admin.json revoke \
  --installation-id "$INSTALLATION_ID"
```

新安装无 `WORLD ADMIN`。服务所能返回的内容在专门的 stdio profile 中固定，凭据、内部提示、角色私有 `ContextBundle` 和小说事实均不会进入组织回执。在预配额度之外新增场景或分支，须由 NF 的正常领域服务形成相应任务与授权。
