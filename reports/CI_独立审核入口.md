# P1 CI 独立审核入口

本仓库的 P1 审计收尾已提交至 `main`，没有启动 P2。机器可执行 CI 定义为 `.github/workflows/p1-integrity.yml`。

- [P1 baseline integrity 工作流](https://github.com/SKYdayDUCK/NovelFactory/actions/workflows/p1-integrity.yml)
- [全部 Actions 记录](https://github.com/SKYdayDUCK/NovelFactory/actions)
- [P1 收尾审计与交接](P1_收尾审计与交接.md)
- [测试失败复现与本地回归证据](evidence/)

## 审核准入规则

需要独立确认同一 P1 提交或对应 PR 提交的两组作业都成功：

1. **Schema, domain, HTTP boundaries**：锁定依赖安装、原有 Schema 正反例、独立机制实验、单元测试和慢请求体回归。
2. **PostgreSQL 17 integration, concurrency and restore**：真实 PostgreSQL 17.11、Alembic 迁移、完整 tests（含并发、任务竞态、对象存储与恢复）。

工作流在每次运行时上传 `p1-schema-unit-<attempt>` 和 `p1-postgres-concurrency-<attempt>` 两份独立审计附件（默认保存 90 天）；包含原始日志、JUnit XML、JSON 报告与恢复记录。**不能仅凭本地单元测试判定 P1 已通过真实 PostgreSQL 门槛。**

### 可重复检查

```bash
# 查看仓库当前 CI
gh run list -R SKYdayDUCK/NovelFactory --workflow p1-integrity.yml
# 选择一轮两份 job 成功的 run ID 查看日志并下载证据
gh run view <RUN_ID> -R SKYdayDUCK/NovelFactory --log
gh run download <RUN_ID> -R SKYdayDUCK/NovelFactory
```

如 GitHub Actions 无运行或任一 job 未通过，应维持 P1 的 CI 验收状态为未完成并保留失败证据；不要进入 P2。
