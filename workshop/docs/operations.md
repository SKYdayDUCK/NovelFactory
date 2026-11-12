# 双服务部署、恢复与退出

## 准备隔离实例

NovelFactory 与 Workshop 使用不同数据库、登录用户和服务 OS 身份。部署标识包括 NF 实例、作品、分支、安装身份和两个源码 SHA。把 NF 对象存储与 NF 数据库作为同一恢复集。把 Workshop 数据库、安装映射、私有启动器和签发材料纳入单独加密备份。MCP 客户端只获得受限的 stdio 管道。数据库口令通过服务管理器、私有 libpq service 或环境文件注入。

数据库管理员在专用 PG17 实例上创建两个非超级用户及其私有数据库。以下为新建环境的示例，身份认证由管理员现有的 SCRAM 或证书策略管理。

```sql
CREATE ROLE aw_service LOGIN NOSUPERUSER NOCREATEROLE NOCREATEDB;
CREATE ROLE nf_service LOGIN NOSUPERUSER NOCREATEROLE NOCREATEDB;
CREATE DATABASE agent_workshop OWNER aw_service;
CREATE DATABASE novel_factory OWNER nf_service;
REVOKE CONNECT ON DATABASE agent_workshop, novel_factory FROM PUBLIC;
GRANT CONNECT ON DATABASE agent_workshop TO aw_service;
GRANT CONNECT ON DATABASE novel_factory TO nf_service;
```

1. 在 NF 目录执行 `uv sync --locked` 和 `uv run alembic upgrade head`。通过受权运维身份配置作品、分支、角色作用域与预算。用 `uv run python scripts/manage_workshop_binding.py --help` 创建安装并预置冻结任务槽位。按 NF 原有角色隔离方式运行 supervisor。
2. 在 Workshop 目录执行 `uv sync --locked --extra streams` 和 `uv run aw-migrate upgrade`。安装 wheel 时可执行 `aw-migrate heads`、`aw-migrate current` 诊断迁移头。部署前用两侧账号分别验证对方数据库拒绝 CONNECT。
3. 用 NF 返回的安装 ID 与实例 ID 执行 `aw-admin install INSTALL NF_INSTANCE`。执行 `aw-admin bootstrap ORG --installation INSTALL --preset Standard` 将组织与该安装绑定，然后 `aw-admin enroll INSTALL ORG author /private/aw-author.json --revision 1`。可选 Minimal、Standard、Long-form 或 Research-Audit。经理和任期员工各自使用独立绑定。
4. 用 `aw-provision budget ORG USD AMOUNT` 设置作者批准的组织上限。为每个授权槽执行 `aw-provision allow-slot ORG INSTALL SLOT EMPLOYEE policy.json`，保持作品/分支、角色、演员、作用域、模板、币种、额度、Token 与 NF 完全一致。
5. 配置两条独立的私有 NF launcher，分别连接项目 resume 身份和安装 delegation 身份。给作者配置 Workshop 服务和目录内 adapters/codex 指令。运行 `aw-mcp --binding /private/aw-author.json`。在受信 broker 安装 `aw-manager.service` 与 timer；把示例路径替换为实际路径后运行 `systemctl enable --now aw-manager.timer`。
6. 配置独立 Redis 8 测试或运行实例，执行 `python -m agent_workshop.notifications --help` 查看 publish/receive/replay。SQL 持久消息支持没有 Redis 时的手动送达。生产备份不得依赖 Redis。
7. 检查 `aw_capabilities`、`aw_state_get`、`aw_history_get`，验证角色看不到管理预算。作者可把已分配的 operation_id 交给 `aw_progress_get` 查看场景负责人、NF 状态、WorldRef 与费用保留。员工只可读本人任期内已发送或接手任务的进度。经理用授权 REPORT 返回冻结 PlanRef 与 ArtifactRef，NF 会在打开引用时复核读取身份。先对一个受权作品执行无模型 smoke，再启用写作授权。作者说“继续未完成的任务”以恢复，`aw_resume_project` 收据可用同一键查询。

### 从已部署的 Workshop 升级

先备份并在隔离的还原库运行 `aw-migrate upgrade`，再用 `aw-migrate current` 与 `aw-migrate heads` 核对同一版本。核对旧目录、安装和 NF 实例的运维登记后，为原来缺少归属的目录执行 `aw-admin claim-org ORG INSTALL`。按当前在职任期为需要组织工具的员工重新运行 `aw-admin enroll`。W0 遗留绑定可继续协商协议，后续组织操作需要新签的成员身份。

切换前按原操作标识核对在途委托、恢复请求、审核与费用。已发送且结算状态为 UNKNOWN 的请求保留原预留，用原操作标识向 NF 查询。历史消息缺少可核实的发送凭据时，数据库保留消息和收件序列，收件人看到 `UNAVAILABLE`。在停用旧服务前保存双侧账本、数据库迁移版本和冻结的 role/skill 哈希。

## 持续派工与诊断

在受信机的私有 `worker.env` 设置 `AW_DATABASE_URL`、`AW_ORG_ID`、`AW_INSTALLATION_ID`。Redis 中继另外需要 `AW_REDIS_URL`。把只调用当前安装的 NF stdio 程序放在 `/etc/agent-workshop/nf-delegation`，由管理员管理该路径的写权限。以下命令假定已把仓库内的 service/timer 样例安装到系统，并把 `/opt/agent-workshop` 配置为 Workshop 虚拟环境。

```sh
systemctl daemon-reload
systemctl enable --now aw-delegations.timer aw-manager.timer
# 已部署独立 Redis 中继时启用
systemctl enable --now aw-notifications.timer
systemctl list-timers 'aw-*'
journalctl -u aw-delegations.service -u aw-manager.service -u aw-notifications.service -n 50
```

委派 worker 仅领取 PostgreSQL 中到期的委派。作者离线时它仍能绑定和核对已有 NF 收据。manager 单独处理持久的作者恢复请求。关闭服务执行 `systemctl stop aw-delegations.timer aw-manager.timer aw-notifications.timer`，等待或停止对应的 oneshot service。手工重启前先核对在途的 NF 请求与保留费用。

## 停止和恢复

先停止派工及 manager timer，再停止运行中的 relay。已发模型调用留在 NF 原 attempt 和预算账上。对 NF 用 `scripts/backup.py --help` 与 `scripts/pitr_drill.py --help` 核对世界数据、WAL、对象校验与角色绑定；对 Workshop 采用独立 PostgreSQL 物理全备和连续 WAL 归档。恢复演练始终还原到新实例，在网络隔离中核对历史。CLI 的环境文件及备份中的令牌只对服务用户可读。

恢复后保持自动派工关闭。在 Workshop 运行 `python -m agent_workshop.recovery freeze --installation INSTALL`。使用 `aw-reconcile --lookup-only --org ORG --installation INSTALL -- NF_INSTALL_LAUNCHER` 核对两侧原 operation_id、task_id、grant epoch、generation、attempt 与币种。对照独立备份清单检查 NF 领先和 Workshop 领先两种情况。存在未知费用、缺失收据或前移失败的作用域时，保留预算和原始输入并升级给运维。恢复脚本需要完整、经核对的 NF 收据映射才能解除安装冻结。重建 Redis 时从 SQL outbox 重发，观察 inbox 去重。只有世界、对象、两侧收据和预算一致时才切换正式连接。

回滚应用二进制前先验证可读取当前扩展 schema；否则恢复配套版本与快照。生产实例上不得以表删除清除未知费用和已接受历史。更新 role/skill 后用原哈希核对已开工快照。更换离职员工或独立 reviewer 时由管理者签发新任期。

## 双库物理恢复和费用核对

先停止两侧入口、worker 和定时调度并等在途请求退出。在隔离的新 PG 实例还原数据库和对应对象快照，校验备份和 WAL 目标。恢复的 NF 执行其恢复围栏，Workshop 对每个安装执行 `python -m agent_workshop.recovery freeze --installation INSTALL`。持续运行普通 manager 或普通 reconciler 也不能绕过已落库的冻结。服务恢复时保留原安装、组织、角色授权、币种和 operation 标识。

1. 通过固定的 NF 安装启动命令运行 `aw-reconcile --lookup-only --org ORG --installation INSTALL -- NF_INSTALL_LAUNCHER`，处理所有分页。冻结 epoch 需要为每笔本地委托取得新证明，包括已经结算的任务。每次核对任务状态、generation、回执以及逐 attempt 费用、Token 和状态；同一回执号下的历史倒退也会进入人工处理。新 epoch 会使之前的证明过期。
2. 在 NF 本地管理员账户执行 `python scripts/manage_workshop_binding.py --access-file ADMIN snapshot --installation-id INSTALL`。保存完整的 operation→账本 JSON 到私有文件。Workshop 在所有已记录操作核对后可用 `python -m agent_workshop.recovery snapshot --installation INSTALL` 导出当前冻结 epoch 的结果。两份独立清单可以发现一侧多出的操作。发现丢失的已知回执、结算明细变化或检查未完成时保留冻结和原账本，请运维从完整备份、WAL 或供应商账单恢复证据。
3. 交叉使用导出的私有文件。NF 的 `reconcile --installation-id INSTALL --input AW_LEDGER` 校验 Workshop 文件，Workshop 的 `python -m agent_workshop.recovery permit --installation INSTALL --nf-receipts NF_LEDGER` 校验 NF 文件。排除已发送后恢复到发送前的错位，复核仍处于 UNKNOWN 的预算预留，再恢复调度。对没有回执且真实 GET 明确返回 NOT_FOUND 的请求，保持原 operation，恢复授权后方可重新提交。一次已确认的提交不会生成第二个 operation。
4. 可在普通非 root 测试账户执行 `python -m pytest -q integration/test_paired_restore.py`。设置 `PG_BIN` 为 PG17 二进制目录、`AW_NF_SOURCE` 为固定 NF 工作树，并让两个项目的 src 可导入。第一项测试使用同一物理归档中的两个受限数据库。设置 `AW_PAIRED_RESTORE_EVIDENCE` 保存回执。DB-01 测试分别为 NF 和 Workshop 初始化带页校验和的 PG17 集群，独立运行 `pg_basebackup`、`pg_verifybackup` 和 WAL 归档。两种错位组合都从两侧归档实际恢复后通过 MCP 按原 operation 查询。设置 `AW_INDEPENDENT_PITR_EVIDENCE` 保存系统标识、提升后的时间线、目标与重放 LSN、备份清单和 WAL 的 SHA-256、UNKNOWN 预留及两种对账结果。
