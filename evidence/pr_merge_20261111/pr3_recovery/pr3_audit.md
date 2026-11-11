# PR #3 合并前独立审计工作记录

审计基线：`24b203f6fcda5f36105dfd9a37cd833671d7215c`，相对 PR #2 `e0b0f2b080f3225d8be8e202e13258bc480f5b84` 增量 90 文件。两树由主 Agent 已与 GitHub 全部 blob 校验一致。本记录只涉及派生工作者、恢复与升级、CI 证据和阶段说明；不是所有 P3/P4 功能的重新开发或全量安全保证。

## 1. 已确认并修复的问题

### 恢复阶段未验证安装目录的完整文件成员集合

旧 `BackupService.verify(require_runtime=True)` 只检查备份 manifest 已列出的 Python 与 Schema 文件是否仍存在且内容相等，没有拒绝额外安装成员。新增 `alpha/__init__.py` 可以优先于同目录 `alpha.py` 被 Python 导入，而原清单中 `alpha.py` 的哈希保持不变；旧恢复校验仍宣称解释器一致。新增 Schema 成员也没有被拒绝。在线备份创建阶段已经比较完整库存，恢复阶段缺失同等约束。

修改：`src/novel_factory/infrastructure/recovery.py` 的 `BackupService.verify`，在安装兼容性模式下先比对 `_runtime_files()` 与 Schema 目录成员集合，再执行现有逐文件哈希校验。`require_runtime=False` 仍只校验归档内容；旧 `runtime_asset_version=1` 的 profile-only 资产兼容语义保留。未改变数据库格式、权限、任务隔离和世界效果。

反例：`tests/unit/test_online_backup_runtime.py::test_restore_rejects_installed_members_not_in_the_saved_interpretation`，覆盖新 Python 包/新 Schema 与两代资产清单。

- 修复前原有 12 项通过，新增 2 项反例失败（真实旧实现未抛异常）：`control/pr3-runtime-before.xml`。
- 初次修复后目标测试与备份连接目标测试 38 项通过：`control/pr3-runtime-after.xml`。
- 追加 version-1 兼容覆盖后最终 40 项通过、0 失败/错误/跳过：`control/pr3-runtime-after-v1.xml`。

### 规模工作流未被后端源码修复触发

旧 `.github/workflows/p3-scale-experiments.yml` 只对 P3 分支 push 的若干 benchmark/工作流路径触发，没有 `pull_request`，也没有 `src/**`。这导致后端性能修复后的新 HEAD 可能只有普通工程 CI，500/1000/600 秒容量证据仍停在旧提交。

修改：保留三种原场景、全部参数、失败门槛和 always 上传；增加源码、测试、benchmarks、契约、迁移、运行资产及依赖锁路径，增加 PR 和最终 main push 触发。`.github/workflows/p4-recovery-drill.yml` 另加 main push，保证正式集成提交可再次执行原隔离物理 PITR。没有改名逃避、禁用、continue-on-error 或放松容量阈值。

## 2. 已有可靠性机制核对

`DerivedWorldWorker` 复用现有 outbox、inbox、artifacts 与 index_watermarks；物化固定不可变 WorldRef，不更新权威世界。工作者在消费事务检查租约 owner/generation/expiry，按 work SHARE → branch UPDATE 的顺序锁定；产物登记、两个水位和 inbox 同事务。ack 在消费提交后执行。重复和乱序处理保留同一派生产物标识、水位不倒退。现有真实进程退出测试覆盖对象后、产物后、水位后、consume 后四故障点；额外覆盖过期租约、并发重复、候选分支、角色与时间权限、运行升级锁顺序。

新 worker 的大历史物化耗时和 500/1000 负载期间队列积压尚未由规模工作流测量。`benchmarks/process_server.py` 明确声明 `worker_processes=0`；不能从这些 API 负载结果推定派生 worker 与 API 共驻的资源容量。CLI 一次串行运行 `max-messages` 有界，但没有生产部署级的统一配置读取或跨进程预算协调测量。`Database()` 配置池上限 5，默认 `ConnectionBudget.worker_pool_size` 为 4；当前串行工作者实际同时用一个连接，这不是本轮确认的连接超限事故，但部署预算必须按实际工作者数量、实际池配置重核，不能直接依赖默认假设。

## 3. 迁移与恢复边界

PR3 不增加迁移文件；沿用 P2 的增量 `0002→0003`。0003 校验冻结 DDL SHA，只加模拟交互相关表和约束，不原地改写 P1 数据。已有 `test_p1_accepted_world_is_preserved_by_p2_additive_migration` 真实创建 0002、接受世界事务、升级到 head，并逐表比较旧摘要、同回执和 HEAD。

在线逻辑备份使用一个导出 PostgreSQL MVCC 快照；对象共享锁排除 GC，未阻断普通世界写。恢复只接受显式新数据库/空对象目录，校验数据库逐表摘要、对象哈希、解释器/Schema/运行资产，再隔离旧任务代际及未知外部调用。PITR 脚本创建独立测试集群，物理 basebackup、WAL 归档、SIGKILL 后恢复指定命名目标，显式验证后续一次世界提交被排除，不以小夹具秒级耗时承诺生产 RTO。

## 4. 文档应更新，历史证据应保留

- 根 `AGENTS.md` 首段仍声称 P3 其他模块与 P4 未授权，不符合本轮明确授权和已实现范围。应说明当前分支阶段、允许的收尾范围及容量未通过，保留核心约束正文。
- `README.md` 首段仍为 410 项回归和旧 PR2 负载结果；应改为当前准确 SHA/工程结果及容量状态，链接本轮报告。旧值应保留在原历史报告中。
- PR3 旧容量报告已明确区分本地和 CI：本地 500 为51/51、CI 为17/51；不能用前者覆盖后者。1000 目标依旧1000/s，600秒读探针不等于多小时混合长稳。
- 1000 Agent mixed 写入、多小时增长世界、派生 worker 和 API 同时承载规模负载未执行，应继续标为 NOT_EXECUTED；不能作为绿色普通 CI 的隐含结论。

## 5. 本轮专项执行记录

轻量测试使用现有锁定 Python 环境并以当前工作树 `PYTHONPATH=src` 载入源码。最终轻量测试固定 CPU 4–8。随后真实 PostgreSQL 专项固定 CPU 6–8，父脚本、子数据库和测试进程继承相同 affinity；与主 Agent CPU0–3 负载、后端 Agent CPU4–5 profiling 分开，仍是共享宿主，不应称独占硬件。原始文件均位于主任务 `control/`；最终 PostgreSQL 结果由 `pr3-recovery-migration-worker.xml` 与 `.log` 及 `pr3-audit-pg/postgres_session.json` 核对。

本轮真实 PostgreSQL 17.11 专项结果：**20 passed、0 failed、0 errors、0 skipped**，JUnit 13.126 秒。覆盖 `test_p2_upgrade.py`、`test_derived_worker.py`、`test_online_backup.py`、`test_world_recovery.py`、`tests/reliability/test_recovery.py`。隔离集群 `fsync=on`、`synchronous_commit=on`、`max_connections=60`；进程成功退出。摘要和原始文件哈希已汇入 `control/pr3_audit_evidence.json`。这是有针对性的迁移/恢复/工作者检查，不替代主 Agent 后续完整工程、PITR 和容量 CI。

未执行任何远程 GitHub 写入、强推或合并；所有修改保留在 PR3 工作树供主 Agent 合并审阅和通过连接器分阶段提交。
