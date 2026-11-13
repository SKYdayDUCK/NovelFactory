# 实验源码和远端交付

本分支通过 GitHub Git Data API 发布。发布者对本地目标提交和远端新提交的根 Git tree SHA 逐次比较，通过期望分支头执行原子更新。发布提交作者、时间和提交拆分可能使远端 HEAD 与报告内开发提交 SHA 不同。两次对照使用同一 tree 才能证明源文件一致。

主要测量由 Python 3.12 的仓库锁定环境运行。Linux 测试使用 PostgreSQL 17.11、Redis、真实 Python MCP SDK 和真实回环 TCP/stdio。每个独立 PostgreSQL 实验启动新的实例或数据库。启动时 max_connections=128；生产应用连接池的 2 / 4 / 8 上限分别写在各实验中，监视连接及客户端进程单列。实验机由并行任务共享。封闭环吞吐反映这台主机上的对应测试时段，不能用不同负载、主机时段或线程数的点计算单一系统加速比。个别早期试验在活动工作树中发起，后续复验使用固定 worktree；报告列出两者及错误尝试。没有调用商业模型。

本地复现先从下文登记的 Git bundle 取回报告中使用的开发提交，然后用相应 README 中的 CLI。需要外部 PostgreSQL 的驱动只接受一个明确设置的 NF_TEST_DATABASE_URL。原始请求记录保存业务 ID、验证结果、计时和计数，测试 bearer、安装密钥及 Provider 配置不入库。ABBA 表中 A 和 B 的准确含义见每项实验，不跨实验复用分母。

| 阶段 | 冻结或实现提交 | 可单独复查的改动 |
|---|---|---|
| 基线 | 41ff440、52c8aa3、637119a | 从 bcb03637 建立指标，选择性移植角色读取、同安装会话复用及任务元数据读取 |
| A | 2e1f734、317b114、fc38dc3 | 普通任务跨协议操作、安装预授权委托、精确恢复范围 |
| B | a1a54d9、2807a41、146ede9 | 分支增量统计、5000 项进度对比、损坏时的检查与重建 |
| C | fd059ce、adb7236、e9c930a | 数据库串行准入、失败前提交检验和深度/存储实验 |
| D | 65e5afe、443ea20、609172a | 执行类别租约、旧索引入口和总连接池核算 |
| E | e62db03、c79715e、046294b | 成组消息、安装范围内订阅唤醒、到期操作员处置 |
| F | 135d3b8、d31fa06、770cd07、7e7dd4d | 合并、集合同域读取和实际 Host 组批 |
| G | d00d1ab、5795961、4f809f3、9b599d8 | 固定资源副本路由、资源竞争、共享认证槽和 30 组混合读 |
| H | 2c1d065、f5a1c51、b7a73e5、a13661a | 交叉审核、同 main 生命周期测试、二十项验收 |

完整实验运行还需克隆库里的合同、运行时角色资料、Alembic 迁移及 `uv.lock`。不要在正式库上运行会创建、丢弃数据库或注入故障的基准。独立恢复环境的最小流程如下。

```sh
uv sync --locked --group dev --python 3.12
uv sync --project workshop --locked --extra streams --group dev --python 3.12
uv run python -m alembic upgrade head
uv run python -m pytest tests -q
uv run python -m benchmarks.hera_mixed --help
```

NF 使用 NF_DATABASE_URL 和 NF_TEST_DATABASE_URL，Workshop 使用 AW_DATABASE_URL，真实 Redis 用 AW_REDIS_TEST_URL 等各测试指定的变量。双服务故障验证创建两个受限 SQL 账户和数据库，需要 AW_DUAL_TEST_ADMIN_URL 和指向该检出的 AW_NF_SOURCE。每份专题报告另列运行该实验所需的命令与配置。
