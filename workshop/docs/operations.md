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
3. 用 NF 返回的安装 ID 与实例 ID 执行 `aw-admin install INSTALL NF_INSTANCE`。执行 `aw-admin bootstrap ORG --preset Standard`，然后 `aw-admin enroll INSTALL ORG author /private/aw-author.json --revision 1`。可选 Minimal、Standard、Long-form 或 Research-Audit。经理和任期员工各自使用独立绑定。
4. 用 `aw-provision budget ORG USD AMOUNT` 设置作者批准的组织上限。为每个授权槽执行 `aw-provision allow-slot ORG INSTALL SLOT EMPLOYEE policy.json`，保持作品/分支、角色、演员、作用域、模板、币种、额度、Token 与 NF 完全一致。
5. 配置两条独立的私有 NF launcher，分别连接项目 resume 身份和安装 delegation 身份。给作者配置 Workshop 服务和目录内 adapters/codex 指令。运行 `aw-mcp --binding /private/aw-author.json`。在受信 broker 安装 `aw-manager.service` 与 timer；把示例路径替换为实际路径后运行 `systemctl enable --now aw-manager.timer`。
6. 配置独立 Redis 8 测试或运行实例，执行 `python -m agent_workshop.notifications --help` 查看 publish/receive/replay。SQL 持久消息支持没有 Redis 时的手动送达。生产备份不得依赖 Redis。
7. 检查 `aw_capabilities`、`aw_state_get`、`aw_history_get`，验证角色看不到管理预算。先对一个受权作品执行无模型 smoke，再启用写作授权。作者说“继续未完成的任务”以恢复，`aw_resume_project` 收据可用同一键查询。

## 停止和恢复

先停止派工及 manager timer，再停止运行中的 relay。已发模型调用留在 NF 原 attempt 和预算账上。对 NF 用 `scripts/backup.py --help` 与 `scripts/pitr_drill.py --help` 核对世界数据、WAL、对象校验与角色绑定；对 Workshop 采用独立 PostgreSQL 物理全备和连续 WAL 归档。恢复演练始终还原到新实例，在网络隔离中核对历史。CLI 的环境文件及备份中的令牌只对服务用户可读。

恢复后保持自动派工关闭。在 Workshop 运行 `python -m agent_workshop.recovery freeze --installation INSTALL`。使用 `aw-reconcile --lookup-only --org ORG --installation INSTALL -- NF_INSTALL_LAUNCHER` 核对两侧原 operation_id、task_id、grant epoch、generation、attempt 与币种。对照独立备份清单检查 NF 领先和 Workshop 领先两种情况。存在未知费用、缺失收据或前移失败的作用域时，保留预算和原始输入并升级给运维。恢复脚本需要完整、经核对的 NF 收据映射才能解除安装冻结。重建 Redis 时从 SQL outbox 重发，观察 inbox 去重。只有世界、对象、两侧收据和预算一致时才切换正式连接。

回滚应用二进制前先验证可读取当前扩展 schema；否则恢复配套版本与快照。生产实例上不得以表删除清除未知费用和已接受历史。更新 role/skill 后用原哈希核对已开工快照。更换离职员工或独立 reviewer 时由管理者签发新任期。
