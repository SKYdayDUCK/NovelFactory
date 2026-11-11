# P3-01 候选：Context Compiler 的有限优化

日期：2026-11-10。基线为 `0609f680fad782c827dd3834a073f8f9bcddaf0d`。本批在 P2 的主要正确性测试、短卷闭环和真实负载测量之后实施。**这是可回退的 P3-01 候选进展，不是整个 P3-01 或 200 Agent 容量验收通过。** 真实模型和作者评价仍未满足，200 档业务负载仍有失败。

## 1. 测量依据与改动范围

原实现编译 1/20/100 个主体时分别执行 51/89/249 条 SQL，其中 5/43/203 条用于逐行登记同一个 Context 的依赖。载荷在主体循环中没有变化，但完整 canonical fingerprint 会重复计算。原始 SQL 计数、逐次耗时和 cProfile 信息保存在 [before.json](../evidence/p3/context/before.json)；[before-provenance.json](../evidence/p3/context/before-provenance.json)验证两份被修改核心文件当时逐字节等于基线。

改动仅有两处：

- `WorldQueryService.context_compile` 将相同载荷的摘要在循环外计算一次，所有主体继续引用原语义的同一个 `result_sha256`。
- `KernelRepository.add_many` 将相同的依赖行按最多 256 行一批插入，继续使用调用方已有事务。它不自行提交，不忽略冲突，不吞异常。当前 InputManifest 的主体及谓词上限使依赖 scope 最多 223 个，本用例只需要一批。

没有新增迁移、缓存权限结果、删除 revision 校验、改变世界命令接受方式或扩展 `DEPENDENCY_GUARDED` 命令模板。正式事件、时态关系、查询 plane、generation、当前授权、固定 WorldRef、幂等回执及上下文 Outbox 的校验顺序不变。

## 2. 先运行的安全测试

[新增安全测试](../tests/queries/test_context_dependency_atomicity.py)在优化前实际运行 **3 passed / 1.22 秒**，优化后与整个查询、世界竞争子集一起运行 **43 passed / 21.40 秒**。

| 安全性质 | 实际断言 |
|---|---|
| 完整依赖 | 正向、负向实体、谓词别名、进程覆盖、规则和 scope registry 的所有持久依赖行、版本和摘要；Impact 能发现负向及过程覆盖依赖 |
| 原子失败 | 在 PostgreSQL 已执行 dependency INSERT 后抛出异常；context、task binding、dependencies、receipt、outbox、artifact 引用全部回滚；重试只落一份结果 |
| 撤权竞态 | 采样完成后、持久化前撤销授权；最终写入被拒绝，没有 context 或依赖绑定 |
| 幂等 | 原请求返回同一编译结果；同键修改 token budget 被拒绝 |

证据：[优化前命令](../evidence/p3/context/safety-before.json)、[前 JUnit](../evidence/p3/context/safety-before.xml)、[优化后命令](../evidence/p3/context/safety-after.json)、[后 JUnit](../evidence/p3/context/safety-after.xml)。最终全量回归另由本轮交接报告列出，不能以这 43 项替代全集。

## 3. 同一切面的配对比较

最初分别初始化 before/after 世界。完整事实哈希比较正确地失败，因为两个世界的 WorldRef、正式事件和证据身份不同。原失败命令和日志保留于 [unpaired-compare-failure.json](../evidence/p3/context/unpaired-compare-failure.json)；没有删除证据字段使比较“通过”。早期结果没有保存完整 facts，无法补造旧运行的逐字段比较。

[配对实验脚本](../benchmarks/context_paired_profile.py)随后采取严格方法：

1. 从 Git 的精确 `0609f680` blob 加载原版 `WorldQueryService`，验证源码 SHA-256；确认原 `Repository.add` AST 未变，六份共享核心文件逐字节仍等于基线。
2. 在一个真实 PostgreSQL 数据库中，通过正式服务创建 1,001 个实体、1,000 条初始关系、12 个实际接受事件；两种编译器使用同一个 Kernel 和同一个不可变 WorldRef。
3. 每次编译创建独立的真实任务和 InputManifest，避免重复请求走幂等快速返回。1/20/100 主体各五对，交替先运行旧版或新版。任务准备、事后持久化检查不计入编译耗时。
4. 完整 facts 连同所有 evidence identities 严格相等。task-specific `query_id` 先逐一验证 UUID5 派生，再比较其余查询字段。独立 InputManifest 先验证完整内容、摘要和各自真实 ArtifactRef，再比较共享 interpretation 和全部依赖 scope/version/type/work/branch。
5. 编译前后的世界 HEAD、状态 hash、manifest、scope versions、事件总数必须不变；结束再次核对测量源码没有变化。

15/15 对均通过完整事实、证据和已验证依赖比较。原始请求关联、完整 facts、全部 dependency manifest、持久行、种子事件回执和逐次计数保存在 [paired-run.json](../evidence/p3/context/paired-run.json)，运行日志与 PostgreSQL 启停记录同目录保存。

| 主体数 | 配对数 | SQL：原版 → 候选 | dependency INSERT：原版 → 候选 | 中位耗时：原版 → 候选 | 观察到的耗时变化 |
|---|---:|---:|---:|---:|---:|
| 1 | 5 | 51 → 47 | 5 → 1 | 73.89 → 75.31 ms | 慢 1.92%，未观察到加速 |
| 20 | 5 | 89 → 47 | 43 → 1 | 142.34 → 125.61 ms | 减少 11.75% |
| 100 | 5 | 249 → 47 | 203 → 1 | 418.84 → 240.85 ms | 减少 42.49% |

硬件为共享 Linux 开发宿主，CPU 型号 AMD EPYC 9V74，可见逻辑 CPU 9；Python 3.12.14、PostgreSQL 17.11；连接池 4、overflow 0，数据库 max_connections 60、shared_buffers 128MB、fsync/synchronous_commit 均开启。SQL 数以编译调用期间的 SQLAlchemy cursor execution 计，不包含任务准备、事后检查或连接 pre-ping。

这是单进程、暖 PostgreSQL/OS 缓冲下的五对观察，没有统计置信区间或生产 SLO。摘要复用与批量插入一起变化，未单独分离两者耗时贡献。没有重跑并声称修复了 200 档完整业务负载，也不能从单次编译提升推导总吞吐提升。

## 4. JSONB、缓存与暂不修改的部分

原始 profile 的 13 个世界 revision（初始 + 12 正式事件）有以下实测体积：

| 数据 | 原版样本 |
|---|---:|
| `sum(pg_column_size(snapshot))` | 3,800,070 bytes |
| 快照序列化 JSON 文本合计 | 15,332,949 bytes |
| 最大单快照 JSON 文本 | 1,186,471 bytes |
| selection 列存储合计 | 6,330 bytes |

这些列体积不是整个数据库、磁盘 WAL 或索引总空间。单个高频实体变化也保留完整世界快照，说明存储和重建成本仍值得继续测量。不同独立运行的随机证据身份与压缩会影响物理体积，不能把微小字节差异当作本批的存储优化。

同一个固定切面的状态查询，应用缓存冷样本约 86.96 ms，五个暖样本约 66–74 ms，但均执行 20 条 SQL。缓存命中仍做当前授权、版本和完整状态读取/校验；本批保留这些围栏。没有清空 PostgreSQL 或 OS 缓存，因而这里的“冷”只指应用结果缓存。

当前时态关系主要在完整 JSONB 中解释，不能直接断言添加某个图索引就会解决本次瓶颈。增量投影、历史重放加速、细分 `SCOPE_REGISTRY:world`、减少不相关修改冲突，以及 JOINT 的 guarded 扩展均未实施。它们需要新的安全证明和独立测量；原严格共同输入规则继续生效。没有引入新的数据库、消息系统或图数据库。

## 5. 复现与回退

配对脚本需要完整 Git 历史可读到基线。没有该 blob 时应 fetch 对应提交，不能改为猜测原实现。所有数据库写入经现有应用接口完成，底层 SELECT 仅用于测量和核验。

```bash
uv sync --locked --group dev --python 3.12
uv run --no-sync python scripts/with_postgres.py --pg-bin "$PG_BIN" \
  --log-dir evidence/reproduction/context-paired-pg --migrate -- \
  .venv/bin/python -m benchmarks.context_paired_profile \
  --output evidence/reproduction/context-paired.json \
  --entities 1000 --history 12 --subjects 1 20 100 --repeats 5

uv run --no-sync python scripts/with_postgres.py --pg-bin "$PG_BIN" \
  --log-dir evidence/reproduction/context-safety-pg -- \
  .venv/bin/python -m pytest tests/queries tests/concurrency/test_world_races.py \
  -q --junitxml=evidence/reproduction/context-safety.xml
```

实际执行的安全测试命令以对应 JSON 为准。候选核心改动只有两份文件，无迁移；若独立审核发现问题，可单独 revert 本批性能提交，保留 A/B/D 和全部证据。恢复逐行 INSERT 不需要数据转换。该回退是能力说明，本轮没有为了测试而改写已经保存的运行结果。

**停止状态：有限性能候选已实现、已做前后安全回归和同切面配对测量；P3-01 全项未验收。**
