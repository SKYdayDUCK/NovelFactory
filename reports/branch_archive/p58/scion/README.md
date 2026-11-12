# Scion 与 P5.8 的兼容实验

本目录保留 PR #4 原始对象清单、从历史差异提取的 15 文件兼容补丁、独立 PostgreSQL 校验程序和实测回执。补丁只在实验工作树中启用。

## 正式 main 复验

正式 P5.8-A main 为 `630c4bcf1f1a7d71f1632d6eb2d273923ac08014`。原 Scion 相对该提交落后 269、独有 13 个提交。在正式 main 和兼容工作树的独立 PostgreSQL 17.11 数据库上运行原基线 6 个场景、候选 6 个场景，每个场景的三臂回执、错误和 76 张表内容相同。单独的迁移、授权、CAS、对象与流水线回归通过 120/120。

| 新基线操作中位耗时（μs/次） | 基线 | 候选 |
|---|---:|---:|
| 三列元数据 | 731.608 | 276.078 |
| 4 / 2098 个 scope | 10522.646 | 421.074 |
| 静态 Schema 正例 | 598.033 | 418.092 |
| 8 事件后缀重放 | 7389.639 | 2586.327 |

性能单独使用新的临时 PostgreSQL 实例，在功能套件退出后采样。8 事件用例有 123 次已证后缀命中（含首次等价性检查与预热）。

## 原基线与恢复

| 引用 | 冻结 SHA |
|---|---|
| 原 PR base `dev/p3-p4-continuity` | `23a61398278c9a13e6450d444d4a6d25a88ef5e0` |
| 原 PR head `experiment/scion-incremental-20261111` | `4f249d520c921c8d6effee9978f7338b912f1dd0` |
| 原 S5 恢复头 `recovery/scion-s5-evidence-20261111` | `c019d971656426a5680d95d2c1bc5a35154d42fa` |

PR #4 有 13 个独立提交、277 个改动文件，其中 225 个属于历史证据。原始 GitHub 状态见 `raw/pr4-before.json`。将以上三条引用生成包含完整祖先的 Git bundle，从空裸库验证 bundle、恢复三条引用并执行 `git fsck --full`。经过恢复的 Git blob 在两份仓库中逐一比对类型、模式、路径、Git ID、字节数与 SHA-256，两个清单完全相同。额外登记了恢复头相对于 PR head 的 4 个不同 blob 及删除清单。逐项结果见 `raw/original-objects.json`，恢复回执与独立 bundle 指纹见 `raw/restoration.json`。在独立恢复出的原 head 实际执行原版六场景 PostgreSQL 差分，见 `raw/historical-restore.xml`。历史站点的功能、性能、容量和长稳数字都对应其原提交。

复核原对象时，先在本地恢复分支归档，再从仓库根目录执行。此操作只读取 Git 对象。

```bash
D="$PWD/reports/branch_archive/p58/scion"
python "$D/verify_original.py" "$PWD" /tmp/scion-original-objects.json
cmp /tmp/scion-original-objects.json "$D/raw/original-objects.json"
```

## 提取和去向

| 历史路径 | 兼容处理 |
|---|---|
| B1 Revision 元数据 | 派生 worker 与 repair review 使用显式列投影。查询端保留 P5.8 的实时 PG 文本与摘要复核；旧查询快捷路径没有迁入。 |
| B2 依赖范围 | 只在白名单命令里读取服务器计算的 scope keys，同时匹配 work、branch。其他命令沿用全范围校验。 |
| B3 重放 | 接入有界 `ReplayProofs`、纯后缀重放、可选延迟/重复父版本建证，缺少证明时完整重放。更新了终态枚举的符号名。 |
| 静态 JSON Schema 引用 | 只在实验开关开启时预解析支持的静态引用。保留标准校验器与原错误语义。 |
| 其他历史改动 | 保存在原 PR 和可重建归档，包括 S1–S5 的完整实验、旧 CI、负载驱动、原始数据。 |

将旧差异直接应用至 P5.8 的 kernel、queries、repository 均有上下文冲突。初次移植测试为 80/101 通过，过期 `world._TERMINAL` 引发可选证明路径回退。改用现有 `TERMINAL_LIFECYCLES` 后为 100/101 通过。最后一项原故障注入只拦截旧的 `repository.one`，注入未到达 P5.8 的 `revision_with_snapshot_text`。改在实际读取边界返回故意损坏且摘要配套的正文，并断言确实注入，101/101 通过。三份原始 JUnit 包含在 `raw/port-{1,2,3}.junit.xml.gz`。

原 query 优化与 P5.8 的实时 witness 有实质冲突，因此保留独立的旧 PR。筛选出的 15 个代码/测试文件进入以本次签收 P5.8-A main 为直接父提交的实验提交，见 `raw/acceptance.json` 的 `local_tested_candidate_sha`。新受审实验分支名称为 `experiment/scion-p58-compat`；发布回执由 B 阶段记录。实验开关不会安装进 P5.8 运行环境。该决定保留 PR #4 的原审查、提交顺序和实验对象。

## 在干净 worktree 复验

使用启用了 PostgreSQL 17 的仓库开发环境。为下面的命令设置 `NF_TEST_DATABASE_URL`，指向无业务数据、能新建测试库的本地 PostgreSQL；性能脚本检验该库的 public schema 为空后才执行迁移。所有场景均无模型计费请求。

```bash
D="$PWD/reports/branch_archive/p58/scion"
E="$(mktemp -d)"
BASE="$(python -c 'import json,sys; print(json.load(open(sys.argv[1]))["accepted_main"])' "$D/raw/acceptance.json")"
git worktree add --detach "$E/reference" "$BASE"
git worktree add --detach "$E/candidate" "$BASE"
gzip -dc "$D/compat.patch.gz" > "$E/compat.patch"
(cd "$E/candidate" && git apply --check "$E/compat.patch" && git apply "$E/compat.patch")
for TREE in "$E/reference" "$E/candidate"; do
    cp "$D/frozen_trace.py" "$TREE/tests/integration/test_p58_scion_trace.py"
done
(cd "$E/reference" && export PYTHONPATH="$PWD/src:$PWD" NF_SCION_FROZEN_P58_SHA="$BASE" \
  NF_SCION_DIFF_FROZEN=1 NF_SCION_DIFF_EVIDENCE="$E/oracle-traces" && \
  python -m pytest tests/integration/test_p58_scion_trace.py -q --junitxml="$E/oracle.xml")
(cd "$E/candidate" && export PYTHONPATH="$PWD/src:$PWD" NF_SCION_DIFF_EVIDENCE="$E/candidate-traces" && \
  python -m pytest tests/integration/test_p58_scion_trace.py -q --junitxml="$E/candidate.xml" && \
  python "$D/run_compatibility.py" "$E/regression.xml" && \
  python "$D/profile.py" "$E/operations.json")
python "$D/compare_traces.py" "$E/oracle-traces" "$E/candidate-traces" "$E/traces.json.gz"
```

冻结追踪跨进程比较干净 P5.8、补丁开关关闭、候选开关开启三个运行状态。六类轨迹包含真实数据库锁等待、已撤销授权、隐藏知识、故障回滚、出版连续性、修订历史及 CAS；对公开返回摘要、领域错误和 76 张 PostgreSQL 表的行数、内容逐项比对。仓库旧差分协议将 `created_at`、`updated_at`、`processed_at`、`delivered_at` 的自动时钟比较为是否存在。审计与 Redis 出站表的默认记录时间、重试时间在两个临时数据库输入端固定；仅在这份语义追踪中为查询编译的单调时钟输入固定刻度，保留审计持续时间字段进行逐值比较。合同、回执、事件与授权时间也逐值比较。独立回归另覆盖对象完整性、0003→当前 Alembic head→0003、多作品上下文和配额隔离，以及写作与审核调度。

微测包含 12 组交替先后顺序的成对测量，记录每组耗时、调用次数及值等价断言；重放还要求每次候选都实际命中已证明后缀。负载范围限于窄列、4 个选中 scope、一次静态 Schema 正例和 8 事件重放。未在新基线上重跑历史六臂 200–1000 Agent 端到端负载、长稳或模型创作。详见 `raw/acceptance.json`、`raw/main-benchmark.json` 和 `raw/main-frozen-traces.json.gz`。

已从 GitHub 重新获取[新实验分支](https://github.com/SKYdayDUCK/NovelFactory/tree/experiment/scion-p58-compat)。发布提交为 `75276a791294d4b17035aead4e33352e865e01aa`，父提交与测试提交的 Git 树经独立比对一致。详见[发布回执](raw/publication.json)。
