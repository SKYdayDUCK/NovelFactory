# PR #6 独立复核记录

审查对象为 `55d8979b5361a4837701d762feacd83036096b11`，Git tree `51b5a83050708ebbdfc43cbee74c14c05fae85d2`。在独立检出中创建新 PostgreSQL 17.11 数据库，开启 fsync 和 synchronous_commit，迁移到本版本 head 后运行。检查范围为投影完整性、授权读取及原容量门槛。此记录不授予生产合并许可。

| 验证 | 结果 |
|---|---|
| 实际 PostgreSQL 投影 | SQL 每次用 work_id、branch_id、world_revision_id 获取当前行及数据库的 JSONB 文本，命中条件包括完整文本和 state_hash。首次或任一项改变都重新验证规范哈希。每次重新解析 JSON 并验证调度清单。4 个线程各做 10 次读取和本地对象修改，两个作品各自的实体集稳定，错误组合的作品/分支/版本返回 VIEW_DENIED。 |
| 存储与恢复 | 原有测试验证 snapshot 或其 digest 独立改变时均拒绝读取。附加测试先预热投影，再在可丢弃数据库模拟同正文、不同调度 provenance 的不一致恢复，后一次读取报告 PROJECTION_CORRUPT。 |
| 兼容早期仓储 | 无原始 PG 文本接口时使用旧 `one("revisions", WorldRef)`，每次重算规范哈希；未初始化缓存的历史 Kernel 测试替身也可读。已校验一次的仓储记录随后发生改变而原 hash 保留时，实测返回 PROJECTION_CORRUPT。 |
| 权限与角色 | 读请求的授权、固定 View/KnowledgeCut 与响应释放仍受原应用路径约束。复核使用原 `test_audit_artifact_security` 验证角色私有材料、旧上下文、角色撤权和代际变化。投影代码没有缓存授权、响应或可变业务行。 |
| 独立回归 | `e2f0faba` 上先运行完整 queries（含额外两例）33/33。补检后，最终审核 SHA 上 queries、历史授权单元和附加 decoded 兼容用例共 38/38，失败、跳过均为 0。原始 JUnit 及追加的测试源码归档在 [独立证据目录](../../evidence/p55/independent-review/)。 |
| 历史容量 | 对 S8 `229f21a` 及合并后的 main `a8f37b7` 分别比较，`.github/workflows/p2-load.yml`、`.github/workflows/p3-scale-experiments.yml`、`benchmarks/p2_behavioral_load.py`、`p2_acceptance_matrix.py`、`p3_open_arrival.py`、`p3_stability.py` 文件完全相同。配对编译比较给两组克隆相同输入和任务代际，保留完整事实与 guard 比较。既有 200 人 overload、500 人 600 秒过载观测以及原容量首尝门槛不能用微基准判定达标。 |

上一轮 GitHub 500 人/600 秒独立到达负载针对 `bf0541a`，12,000 次中 11,494 个 200、506 个 429，探针门槛 NOT_MET，长时门槛未执行。上一轮 `e2f0faba` GitHub 的同机编译与 200 mixed 通过；200 overload 的真实容量断言失败。P4 止于镜像 DNS 失败，未运行 PITR。最新 SHA 的 Actions 初始记录为 P1/P2 [43036514325](https://github.com/SKYdayDUCK/NovelFactory/actions/runs/43036514325)、P4 PITR [43036514910](https://github.com/SKYdayDUCK/NovelFactory/actions/runs/43036514910)、P2 [43036514694](https://github.com/SKYdayDUCK/NovelFactory/actions/runs/43036514694)、P3/P4 [43036514266](https://github.com/SKYdayDUCK/NovelFactory/actions/runs/43036514266)、P3 规模 [43036514307](https://github.com/SKYdayDUCK/NovelFactory/actions/runs/43036514307)、原生 [43036514896](https://github.com/SKYdayDUCK/NovelFactory/actions/runs/43036514896)。最终结论以这些运行对应 SHA 和 job 为准。P4 CI 的系统包安装使用 runner 的发行版仓库，需查看每次恢复回执的 redis_version；独立本地 P5.5 PITR 原始回执记录的 Redis 版本为 8.0.2。

## 复现

在该 SHA 的一次性干净检出里，将归档的 `query-checks.py` 和 `decoded-revision.py` 分别复制到 `tests/queries/test_independent_checks.py` 和 `tests/unit/test_independent_decoded_revision.py`，使用锁定依赖与真实 PostgreSQL 17：

```sh
uv run --no-sync python scripts/with_postgres.py --migrate -- \
  python -m pytest -q tests/queries tests/unit/test_audit_artifact_security.py \
  tests/unit/test_independent_decoded_revision.py --junitxml=independent-junit.xml
```

内存仓储补测首次收集时使用了错误的相对导入。已修正独立测试文件并在新数据库完整复跑；上表最终 JUnit 为修正后的原始输出。此项补测未修改上游源码或历史测试。
