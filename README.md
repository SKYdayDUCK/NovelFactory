# NovelFactory

中文 · [English](README.en.md)

NovelFactory 是一个多智能体小说生产系统。作者可以让角色按当时知道的事情行动，让编辑在同一版草稿上审读，再把获准的事件写入小说世界。作品暂停后，作者可以从保存的任务和审核回执继续写作。

例如，信使在第一卷收到密信，主角到第二卷才读到它。系统分别记录信件发生了什么、主角什么时候获知内容，以及提纲预计何时揭晓真相。写作、推演和审核可以并行，各自使用获准的信息。

## 从哪里开始

| 我想做的事 | 入口 |
| --- | --- |
| 在本机跑通一卷，查看输出与世界事件 | [快速开始](docs/zh-CN/02-快速开始.md) |
| 了解人物认知、正式世界与跨卷继承 | [项目介绍](docs/zh-CN/01-项目介绍.md) · [系统架构](docs/zh-CN/03-系统架构.md) |
| 设置员工、Skills、模型和作品预算 | [Agent 系统](docs/zh-CN/04-Agent系统.md) · [Agent Runtime](docs/zh-CN/05-Agent-Runtime.md) |
| 从提纲走到分章写作和文学审核 | [小说生产流水线](docs/zh-CN/06-小说生产流水线.md) |
| 从软件或模型宿主调用项目 | [MCP、HTTP 与管理接口](docs/zh-CN/07-接口参考.md) |
| 部署两个服务，演练中断与恢复 | [配置与部署](docs/zh-CN/08-配置与部署.md) · [运维与恢复](docs/zh-CN/09-运维与恢复.md) |
| 判断负载是否适合自己的机器 | [性能与实验](docs/zh-CN/10-性能与实验.md) |
| 不连接数据库和模型，测试到整书最终冻结 | [Dry Run 与程序生命周期](docs/zh-CN/11-Dry-Run与程序生命周期.md) |

## 一部小说如何推进

Agent Workshop 维护组织、员工、授权和 Skills。NovelFactory 把一次写作任务绑定到作品、分支、角色和世界版本。Supervisor 按服务端路由选择模型，Creative Harness 组织上下文、候选计划、推演和审读。世界内核核对依赖和批准后推进正式 HEAD。多个作品可共享模型执行资源，各自保留任务和账本。

原生 Codex 审核通过真实 MCP 核对固定版本的原文与系统数据，系统内疑问自动交独立角色补查。Flash 执行写作和退修；新稿重新通过认知、事实与文学审核。编辑批准后自动启动双路独立盲评，合格后冻结整书。退回、异常、重启和未知账务各自保留证据与有界恢复规则，离线模拟结果与真实作品验收分开记录。

篇幅仅作参考，系统不因字数不足或超出目标暂停、返工或凑字数补章。完本依据故事、情节与人物逻辑，以及正式计划和结局的实际完成情况。

```mermaid
flowchart TD
    A["作者 / 项目绑定"] --> NF["NovelFactory 任务"]
    W["Agent Workshop"] -->|安装委托| NF
    NF --> H["Creative Harness"]
    NF --> R["Supervisor / Agent Runtime"]
    H -->|上下文与审核依据| NF
    R -->|结果与调用回执| NF
    NF -->|获准的提案| K["世界内核"]
    K --> P["PostgreSQL 世界版本"]
```

## 运行六场短卷

在 Linux 源码目录中准备 Python 3.12、uv 和 PostgreSQL 17 服务端工具。将 `PG_BIN` 指向包含 `initdb` 的目录。下面的运行创建临时数据库，使用确定性模型夹具。样例输出记录场次数、模型调用和写作前后的世界事件数。

```bash
uv sync --locked --group dev --python 3.12
export PG_BIN=/usr/lib/postgresql/17/bin
out=$(mktemp -d)
uv run --no-sync python scripts/with_postgres.py --migrate -- \
  .venv/bin/python scripts/run_short_volume.py --execution-kind TEST_FIXTURE \
  --object-root "$out/objects" --output "$out/volume"
```

本轮实跑得到 6 场、2,204 个 Unicode 码点、15 次夹具调用和 0 次真实推理。写作前后均有 7 个已接受世界事件。接入真实模型时，由操作人员单独配置 Provider、授予调用权限并设置预算。[安装步骤与输出说明](docs/zh-CN/02-快速开始.md)

## 工程状态

| 范围 | 已取得的结果 |
| --- | --- |
| 核心行为与恢复 | 本轮单元/契约 860 项通过，另有 17 项子测试；PostgreSQL 分片与补测覆盖及平台限制单独记录。双服务、历史升级、Redis Streams 与 WAL/PITR 的历史证据保留。[最新范围](reports/story-completion-advisory-20261114.md) · [先前范围](reports/runtime-coverage-recovery-20261114.md) |
| 容量 | 两轮 500 Agent、每轮 10 分钟的读测试各完成 12,000 次请求。200 Agent 过载及 1000 Agent 开放到达仍未达到原门槛。 |
| 文学与费用 | 短卷和供应商故障测试使用合成输入。真人全文评分与商业模型账单需要单独取证。 |
| 离线整书流程 | 多章多版本的 50 组双书各八章全部最终冻结；37 类、740 个场景的不变量检查通过。[最新恢复验收](reports/runtime-coverage-recovery-20261114.md) · [历史记录](reports/dry_run/README.md) |
| 本机真实生成 | A、B 使用 Codex 审核与 Flash 写作。真实运行已通过任务绑定 MCP 与自动复核，另修复任务身份格式及独立审核记录冲突；小说尚未完本，无人值守生产验收仍在进行。[最新真实观察](reports/runtime-review-format-20261114.md) · [真实工具与恢复](reports/runtime-native-tools-20261114.md) · [流程验收](reports/runtime-coverage-recovery-20261114.md) |

项目使用 Python、SQLAlchemy、PostgreSQL 17、可选的 Redis Streams、MCP stdio 和 HTTP。可先运行 `python scripts/nf.py dry-run --books 3 --chapters 5 --seed 42`，经过真实 Harness 边界模拟规划、写作、随机审核、返工、双路全文审核及整书最终冻结，数据库和上游模型调用均为 0。默认命令必须达到全部小说最终冻结才返回成功。[Dry Run 与生命周期](docs/zh-CN/11-Dry-Run与程序生命周期.md)

管理器是程序，空闲时休眠并回收 Worker，按持久任务变化唤醒。真实模型分工为 Codex 承担复杂工作、规划和审核，DeepSeek Flash 承担写作、角色扮演和简单摘要。密钥使用本机凭据库；稳定上下文前缀提高缓存复用条件，按真实回执统计命中率。`temp/` 和 `.runtime/` 均不提交 Git。

参与开发前请阅读仓库约定。设计沿革与历史验收保留在原路径。[开发约定](AGENTS.md) · [历史验收](reports/p6_readiness/README.md) · [容量问题 #13](https://github.com/SKYdayDUCK/NovelFactory/issues/13) · [GitHub Actions](https://github.com/SKYdayDUCK/NovelFactory/actions)

## 许可证

GNU Affero General Public License v3.0。[LICENSE](LICENSE)
