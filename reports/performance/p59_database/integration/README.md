# 将可选任务 GET 优化应用到 P5.9

补丁基于 `dev/p59-runtime-architecture-refactor` 在本次验证时的提交 `877be80d7c1dfa8b6fb9c9c5a2a570c7b14d3ca0`。它提取实验提交 `4c4a36c` 的单条任务读取路径，涉及三个产品文件和一个独立测试文件。该测试只使用项目现有的测试夹具、PostgreSQL、FastAPI 和 SQLAlchemy。数据库迁移及新增依赖数量均为零。

在需要集成时，将本目录的 [format-patch](0001-opt-in-fenced-task-get.patch) 保存到目标检出目录之外，例如相邻目录的 `opt-in-task-get.patch`。在基于上述 SHA 的干净工作树中执行下列命令。若上游继续演进，需针对新的提交重新运行用例。

```sh
git am -3 ../opt-in-task-get.patch
git show --stat --oneline HEAD
```

拥有本实验分支的仓库也可以导出补丁：

```sh
git show origin/experiment/p59-database-throughput:reports/performance/p59_database/integration/0001-opt-in-fenced-task-get.patch > ../opt-in-task-get.patch
```

正常服务启动时 `NF_HTTP_FENCED_TASK_STATUS` 默认为关闭。设置为 `1` 并重启 HTTP 服务后，已认证的 `GET /api/v1/tasks/{task_id}` 在同一事务内读取任务、该代次的不可变输入和被 SHARE 锁定的作品行，再锁定并验证授权行、任务所有权和读取范围。切回 `0` 或移除该环境变量并重启，即可恢复原读取方式。其他入口继续调用原有 `Kernel.get_task`。已集成的提交如需从代码中移除，可对实际应用后的提交执行 `git revert <SHA>`。

本次验证从两个独立的 detached 工作树开始。第二个工作树实际执行 `git am -3` 后产生了与补丁来源相同的 Git tree，并在真实 PostgreSQL 17.11 上运行 18 个用例，全部通过。覆盖未设置、显式关闭及启用三种配置，核验完整响应、5/5/3 次 SQL、角色及作品隔离、撤权与行锁顺序、原有绑定 Agent 入口、HTTP 读写和操作员 CLI。用例与 SHA 清单及原始 JUnit 一并保留。[验证清单](verified.json) · [JUnit](applied-postgres.xml)
