# 实际 P1 旧源码写入后的 P2/P3 升级兼容性探针

## 本次结果

真实 PostgreSQL 17.11、共享宿主 CPU6–8 affinity。P1 是只读 Git worktree `721f155c9cc912cddfdae559afe4f2516d0967f2`，在该目录通过 `uv sync --locked --offline --group dev --python 3.12` 创建自己的解释器环境。P2/P3 测试使用冻结运行源码目录，防止并行开发改变本次受测字节；逐文件清单在 `p2-source-manifest.json` / `p3-source-manifest.json`。三者 `uv.lock` SHA-256 都是 `e262a34d691894a8a644ac9e561cee6a16c40d2eebd2755d87e13dcb99cdf0bd`。

1. P1 自身 Alembic 升级到其 head `0002`，实际 bootstrap 和6次正式接受，形成7修订、9任务样本、7历史/认知查询、3越权拒绝及47对象。
2. P2 新解释器进程载入冻结 P2 源码，迁移到 `0003`。33个原有业务表摘要完全相同，只有 Alembic 版本号改变，3个新增表为空。
3. P2 与 P3 各自验证6个原请求返回完全相同回执、7修订未变、7查询相同、3拒绝相同、9任务相同、47对象哈希相同；实际TCP启动后的健康/授权查询/角色越权状态依次为200/200/403；重试和读取前后的数据库摘要相同、结束连接归还0。

原始结果：`run03/p1.json`、`run03/p2.json`、`run03/p3.json`、`run03.log`、`pg-logs03/postgres_session.json`。整个子进程切换和检查窗口8.498667秒。`summary.json` 汇总文件哈希、来源以及限制。

`run03/private-state.json` 仅含合成测试身份，但包含用于后续验证的临时token，权限0600，**不应加入公开Git证据**。公开报告只包含哈希/计数，不需要这些token。

## 复现

保留 `check_stage.py` 与 `driver.py` 在同一目录。准备精确P1旧源码及两个受测源码目录。各目录需包含 `src/`、`contracts/`、`runtime/`、`migrations/`、`alembic.ini`、`pyproject.toml`、`uv.lock`。P1 使用其本目录锁定虚拟环境；目标解释器必须有目标锁定依赖。

用项目现有 `scripts/with_postgres.py` 创建独立测试集群，再运行以下驱动参数（输出目录必须全新）：

```bash
taskset -c 6-8 <P1_PYTHON> <P1_SOURCE>/scripts/with_postgres.py \
  --pg-bin <PG17_BIN> --log-dir <NEW_LOG_DIRECTORY> -- \
  <P1_PYTHON> driver.py \
  --baseline <P1_SOURCE> --baseline-python <P1_PYTHON> \
  --p2 <P2_FROZEN_SOURCE> --p3 <P3_FROZEN_SOURCE> \
  --target-python <TARGET_LOCKED_PYTHON> --output <NEW_RESULT_DIRECTORY>
```

每个源码阶段都在独立 Python 子进程中设置其自身 `PYTHONPATH`，并断言实际加载的 `Kernel` 文件来自指定目录。P1源码和目标源码不会被脚本修改，数据库只由隔离包装器创建与结束。HTTPX loopback探针显式 `trust_env=False`，不走宿主代理。

## 保留的探针脚手架失败

- `run.log`：首版探针把 `ObjectStore.inventory()` 的dict当作对象列表，AttributeError；仅修正探针API使用。
- `run02.log`：P1种库和P2数据核验完成，loopback客户端继承宿主SOCKS代理、锁定环境未安装socksio；显式关闭本地探针环境代理后重跑，没有给产品增加依赖。

两次失败原文保留，不被计入产品缺陷或迁移通过。成功结果只取完整 `run03`，不拼接不同运行。此后仅将驱动参数化，以便使用新输出目录复现；业务校验代码不变。

本证据只证明该明确合成历史的旧源码写入兼容性，不证明任意生产数据库升级、真实生产启动或规模容量。
