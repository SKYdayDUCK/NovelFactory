# 在 WSL2 中给不同作品、角色与任务指定模型

本节使用两个服务各自的管理凭据。Workshop 的管理者发布版本，NF 的项目管理员在指定作品及分支采用该版本。导出文件是两个授权管理员之间的本地交接。NF 管理员应在受信目录核对文件、组织和规则；本流程没有远程签名验证。采用一份现有版本不会发起推理。普通角色的 MCP 工具列表不包含读取或发布策略的入口。

在仓库根目录并使用已经安装 NF 和 Workshop 的 Python 环境运行。下文的 `ACCESS` 是本项目的 supervisor 私有访问文件，`MANAGER` 是已有的 Workshop manager 绑定，二者均位于仓库外。先配置 `NF_DATABASE_URL`、`NF_OBJECT_ROOT` 和 `NF_RUNTIME_PROFILES`。模板的 profile 名需与本机模型目录一一对应。

```bash
umask 077
WORK=$(python -c 'import json,sys; print(json.load(open(sys.argv[1]))["work_id"])' "$ACCESS")
BRANCH=$(python -c 'import json,sys; print(json.load(open(sys.argv[1]))["branch_id"])' "$ACCESS")
python -m agent_workshop.policy_cli --binding "$MANAGER" publish \
  --work "$WORK" --branch "$BRANCH" --expected-revision 0 \
  --rules reports/p6_readiness/templates/routing-rules.json > "$PRIVATE/policy-v1.json"
python scripts/runtime_policy.py --access-file "$ACCESS" activate \
  --policy "$PRIVATE/policy-v1.json" --expected-revision 0
python scripts/runtime_policy.py --access-file "$ACCESS" preview --role writer --kind scene_draft
```

预览回执含命中的规则、profile、配置摘要、策略版本和工件引用，仅供管理人员。将本作品场景配置的 `adapter_id` 或生产流水线 `routes` 内相应阶段的值设为 `@policy`。任务创建时冻结选择，后续从相同断面的 NF 工件重建角色上下文。显式指定静态 profile 的任务保持单一路线。用于角色实例匹配的 `instance_id` 是人物的授权 actor UUID，动态角色使用其不可变实例引用的 artifact_revision_id。

可用选择条件依次为单任务覆盖、task_id、instance_id 与 task_kind、instance_id、role_id 与 task_kind、role_id、task_kind、branch_id、work_id、org_id、installation_id、空对象默认。规则必须有唯一 id 和唯一选择条件。上下级管理者可以预先为某一作品发布含全部默认和覆盖规则的快照。当前快照可用相同 CLI 的 `get --work "$WORK" --branch "$BRANCH"` 重新导出。输入中只接受标识和匹配条件。

热切换时，用新规则重发上述 publish 和 activate，将两处 expected-revision 设为当前版本。每次创建后继任务都会解析采用中的版本。要立即更换一个已有、尚未发送的已登记任务，可运行以下命令，其中 GEN 是当前任务代际、KEY 是稳定的操作标识。

```bash
python scripts/runtime_policy.py --access-file "$ACCESS" rebind \
  --task "$TASK" --generation "$GEN" --key "$KEY"
```

该命令在一笔事务中检查发送状态、结清可证明未发送的预留、撤销旧执行代际并写入新快照。保留旧工件与待满足的前置边。涉及已发送/UNKNOWN 的请求进入原有对账流程。已返回、已接纳、已委派或绑定代际专用授权的任务需要新的显式任务或对应的授权手续。不会根据不同 provider 的对话 ID 恢复角色记忆。
