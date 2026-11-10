# P3-03：中文文学记忆与检索评测基础

本目录全部故事材料均为 **TEST_FIXTURE**。`corpus.json` 是中文长上下文合成语料，不是一次真实模型完成的小说。它用 40 个原创受控来源单元与确定性背景段落构成 48 章、384 个原文段、78,952 个 Unicode code point。背景展开只扩大证据之间的距离，不代表自然长篇的语义难度或文学质量。

## 两类实验分开

`evaluate.py` 比较本地确定性检索表示；`service_probe.py` 通过真实 PostgreSQL 17、Kernel、WorldQueryService 和 ContextCompiler 检查运行边界及上下文编译成本。离线表示的图不是另一个世界数据库；离线作用域标签不替代生产权限。现有 `ReviewService` 的文学档案、全文覆盖、精确原文与反证仍是正式服务入口。

| 方法 | 明确的实现 | 不能据此推导 |
|---|---|---|
| `ordinary_summary` | 每章获准原文的首段与末段，保留完整原文锚点；不按题目挑摘要 | 不代表真实 LLM 摘要能力 |
| `character_memory` | 按稳定人物 ID 留存全部有角色关联的原文条目、时间与类别 | 不代表所有世界规则都天然属于人物档案 |
| `temporal_graph` | 同一获准候选集，中文二元字片与实体匹配；前三个种子的一跳来源链接扩展 | 不证明任意深度图检索充分，也不证明文学提升 |
| `graph_without_links` | 与图方法相同的候选集和排序，关闭来源链接扩展 | 只消融链接，不消融权限或时间正确性 |

所有方法先固定 work / branch / revision / plane / actor / story tick / known_at / reader position，再产生候选。排名和边扩展只能消费获准列表；隐藏节点不能成为通向可见节点的捷径。公开纯算法入口 `src/novel_factory/algorithms/memory_retrieval.py` **不负责授权**，尚未挂接生产 MemoryService。调用者必须从正式服务取得固定切面并在交付前重新授权。

普通摘要与人物档案的来源覆盖不同，因此三类表示的整体差异不能单独归因于“图优于摘要”。`graph_without_links` 提供更窄的配对对照。当前语料尚未证明需要专用图数据库、embedding 或模型重排。

## 数据、问题与指标

- `source_units.json`：原创来源文本、稳定 ID、世界/认知/计划/文学平面、时间与获知时间、适用分支/版本、来源链接。
- `build_corpus.py` / `corpus.json`：确定性距离展开，原文 SHA-256 与 Unicode 半开区间锚点。生成器不读取金标准。
- `gold_cases.json`：35 题，28 个证据召回题、7 个隔离反例。包含世界规则、宏观事件重复叙述、长期情绪、关系不对称、认知迟获、伏笔、声线、风格、承诺、历史修订、未采用候选和读者披露。
- 金标准根据原文语义预先编写；程序不根据排名结果补改答案。但它仍是开发者标注，**不是独立作者的文学评价**。

问题不只标一条“最佳相似文本”。需要反证或前后变化的题目给出多个必需来源，只有全部召回才算 `all_required_evidence`。未召回和过预算原文保存在每题原始记录中。检索无结果只表示没有召回证据，不表示世界中不存在。

`review_class` 区分有明确事实冲突证据的 `HARD_CONSISTENCY` 与需编辑判断的 `SUBJECTIVE_LITERARY`。当前测试只检查这些问题的证据是否可得，不让确定性字词匹配冒充文学判断；没有自动宣布“笑意味着悲伤清零”或“删掉某句必然更好”。未来语义评测必须另存模型答复、引用、误报/漏报及作者裁决。

主要指标是必需来源宏平均召回、完整证据题数、禁止/越界来源命中、精确锚点完整性、实际上下文 UTF-8 字节、候选索引文本字节和本地耗时。三档统一上限为 1,500 / 3,000 / 6,000 字节，包含出处标题和分隔符，不切断原文段落。真实供应商 token、费用和生成答案正确率均为未测量。上下文始终标记 `PARTIAL_SEARCH_ONLY`，不宣称硬约束已经完整。

## 复现

在项目根目录执行：

```bash
uv sync --frozen
uv run --no-sync python benchmarks/memory/build_corpus.py
uv run --no-sync python benchmarks/memory/evaluate.py --output evidence/p3/memory/reproduction
uv run --no-sync python -m pytest tests/unit/test_memory_retrieval.py -q
```

已有 PostgreSQL 17 二进制时，使用仓库隔离工具运行实际服务探针；将 `/path/to/pg17/bin` 换成本机路径：

```bash
uv run --no-sync python scripts/with_postgres.py \
  --pg-bin /path/to/pg17/bin --log-dir evidence/p3/memory/reproduction-pg --migrate -- \
  python benchmarks/memory/service_probe.py --output evidence/p3/memory/reproduction-service --repeats 3

uv run --no-sync python scripts/with_postgres.py \
  --pg-bin /path/to/pg17/bin --log-dir evidence/p3/memory/reproduction-tests-pg -- \
  python -m pytest tests/integration/test_memory_service_probe.py -q
```

离线结果为 `summary.json` 与完整 `cases.jsonl`；保留语料、金标准、脚本及排序器哈希。每题记录精确选中 ID、来源 hash、分数、链接原因、缺失证据与上下文 hash，可由来源重构原文。不同运行的 wall time 会不同；应比较证据选择和哈希，不要求运行时长逐位相同。

真实服务探针输出 `service_probe.json`，含真实请求/响应、已接受种子事件回执、测试实体映射、固定 WorldRef、ContextBundle、SQL 计数和九项门禁。不会输出 principal bearer token 或数据库凭据。SQL 统计范围是 `context_compile` 期间的 SQLAlchemy `before_cursor_execute`，不含前面的任务和输入清单创建。

## 下一道验收门槛

选择至少两部获准的中文实际作品或真实生产短卷；冻结训练/开发/测试划分，扩充非词面重合、长距离、多跳、同名和真实改稿样本。普通摘要与文学档案应来自同样来源、同样预算的真实外部任务，并记录生成成本。之后做盲化配对阅读和作者意见，分别报告事实正确、认知边界、证据召回、风格/声线和主观阅读效果。现阶段只完成可运行的评测基础。
