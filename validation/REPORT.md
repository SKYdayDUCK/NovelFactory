# v2.0-R1 设计包验证报告

日期：2026-11-10。对象是最终设计文件、结构契约、有限参考模型和负载假设计算。**设计一致、参考模型通过、业务实现正确与真实性能达标是四种不同证据，本报告分别记录。**

## 1. 环境与复现

实际环境：Python 3.12.14、jsonschema 4.26.0、referencing 0.37.0、PyYAML 6.0.3。静态检查不联网；实验及负载模型只依赖标准库。未启动 PostgreSQL、Docker、正式应用服务、Agent Host 或模型生产管线。

在包根运行：

```bash
python -m pip install -r validation/requirements-validation.txt
python validation/validate_package.py
python -m unittest discover -s experiments -p 'test_*.py' -v
python benchmarks/calculate_load.py
```

机器证据：[package_check_results.json](package_check_results.json)、[mechanism_results.json](mechanism_results.json)、[mechanism_test_output.txt](mechanism_test_output.txt)、[负载结果](../benchmarks/load_model_result.json)。验证脚本重新生成包检查结果；unittest 直接输出实验结果。requirements 仅为本包校验依赖，生产锁文件仍在 P0 生成。

## 2. 静态包检查

| 对象 | 实际结果 | 证明范围 |
|---|---|---|
| Schema | 3 份、94 个定义 | Draft 2020-12 元结构、格式及引用可解析 |
| 结构正例 | 58 个全部通过 | 已编码的类型、枚举、必填和条件约束 |
| 反例 | 31 个全部被拒绝 | 包括角色 cut 越界、FIXED 等未来、未知泄露、关系转换类型、计划冒充事实、缺引用等结构反例 |
| 工具目录 | 28 个、84 个输入/输出/错误引用 | 每个绑定有定义；不表示已部署 28 个工具 |
| 运行时目录 | 10 个角色、9 个技能、15 个角色/技能绑定、4 组受限模式、10 个 query profile | 工具、操作、平面、recipe 和角色匹配；服务端 grant 仍待实现 |
| 开发规范 | 9 份 AGENTS.md、6 个开发技能 | 规范存在，技能 frontmatter/路径有效；未在真实 Host 验证加载 |
| v1.1 基线 | 14 个原件哈希一致 | 原输入未改写；合订审计保留 |
| 契约兼容 | 原 core/literary 2.0 两份哈希一致 | 2.1 增量契约没有篡改旧字段定义 |
| 设计整合 | 原 01–12 均有实质修订，整合清单哈希匹配 | 补充要求实际进入主设计，不只是增加专题文件 |
| 原案例 | 44 个继承＋16 个追加，60 个来源文件哈希有效 | 案例仍待业务实现，未把旧案例改成新夹具 |
| R1 案例 | A–H 共 8 个家族、37 条检查 | ID、来源与待执行状态一致，execution_evidence 为空 |
| 图与链接 | 10 份 Mermaid 源文件与文档对应，本地 Markdown 目标均存在 | 未运行 Mermaid 渲染器；不检查所有标题锚点和外部网址可用性 |
| 规范化与证据 | 2 组规范化向量、2 个真实对象哈希、2 个 Unicode 原文跨度和阅读长度检查 | 有限样本字节/跨度一致，不代表所有抽取质量 |

最终链接、JSON 文件和代码围栏总量以机器结果为准，避免把重复引用数量当成设计覆盖率。示例 ID/哈希多数为合成格式夹具，不能用其通过推断对象存在、批准有效或权限正确。

## 3. 有限机制模型：35 项通过

统一 unittest 实际执行 **35 项，通过 35，失败 0，错误 0，跳过 0**。原 18 项保留，R1 新增 17 项。原始输出和测试名已保存。实现位于独立 [experiments](../experiments/README.md)，正式业务不得导入。

原模型覆盖丢回执幂等、同键异载荷、取消代际、失败不部分改变、分支继承效果、文本表现不施效、封闭意图到达排列、负向依赖、排他前沿、模糊时间、角色未知、Retcon 旧发言、宏微中间状态和 Unicode 证据跨度。

新增模型包含以下对照，具体映射见 [R1_NOTES](../experiments/R1_NOTES.md)：

- 同一 base 的独立命令可 guarded 接受；strict 第二个失败；证书后无关 HEAD 前移可保留资格。
- 同资源、跨实体约束、空查询新增和注册表删除重建，分别验证状态冲突、写偏斜、幻读与 ABA。
- 取消前后接受顺序和丢回执重试；此前成功与新迟到写入分别处理。
- 同快照固定事实/token，对照“旧事实＋新 token”的 TOCTOU 错误。
- 解释版本与活动计划选择变化；固定旧计划历史仍可读取。
- 一次 pin 的批量读取，对照逐项 latest 拼接不存在状态；FIXED 不能等成未来世界。
- 缓存命中撤权、批量内部操作/图权限和 ACTOR 平面边界。
- D20 借改赠保留旧 D25 权属；D35 人物误信与实际持有者分别查询。

这些实验是单进程内存中的显式有限交错，scope 来自手写夹具。没有验证真实数据库锁序、隔离、所有并发交错、持久化崩溃恢复、完整依赖推导或生产安全策略。模型耗时不是吞吐/延迟基准。

## 4. 负载假设计算：15 行、4 项检查

实际运行 [calculate_load.py](../benchmarks/calculate_load.py)，读取固定 seed 和声明参数，生成五档人口×三类缓存共 **15 行** JSON/CSV。四项检查通过：角色分配保持总人数、暖缓存减少预计读取、闭环在延迟增加后减速、open-loop 计划到达率保持不变。

结果状态为 `MODEL_CALCULATED_NOT_SERVICE_BENCHMARK`。SQL 需求、模型在途、token 和连接预算均按假设推导；没有 SQL、网络、服务或付费模型调用。`accepted_commit_tps`、`measured_latency_ms` 和 `measured_cost` 均为空，表示未测量，不表示零。

真实驱动、P50/P95/P99、CPU/内存/磁盘/连接、重试、索引延迟、拒绝/排队、公平性、恢复和费用的测量方案见 [15](../docs/15_并发压测与端到端验收.md)。10/50/200/500/1000 都是待验证负载档位，没有任何一个被本计算认证为容量。

## 5. 交叉审查和直接修正

原 [CROSS_REVIEW](CROSS_REVIEW.md) 和 [HANDOFF_EXERCISE](HANDOFF_EXERCISE.md) 保留为 v2.0 阶段历史审查，R1 当前依据是 [并发独立复核](R1_CONCURRENCY_REVIEW.md)、[需求与验收审查](R1_REQUIREMENTS.md)和 [CHANGELOG](../CHANGELOG_R1.md)。

本轮已修正：证书采样 TOCTOU、解释与活动计划选择的隐性依赖、旧批准与当前资格混淆、strict writer 漏更新 scope 的可能路径、批量绕过操作/plane 权限、上下文编译偷偷修改任务代际、跨分支采用误放宽，以及 D41 状态与排他前沿的夹具冲突。修改已进入主文档、Schema、角色 profile 和开发规范，不只留在风险表。

A–H 用例拥有明确初始条件、过程、预期状态和验收证据。它们的 **37 条检查与原 60 个案例粒度不同，且都没有执行业务验收**；不得相加后称“97 项测试通过”。

## 6. 后续必须取得的真实证据

P1 在目标 PostgreSQL 上验证原子接受、所有 writer 的 scope 维护、双连接冲突/幻读/写偏斜、撤权/取消/批准竞态、证书采样、短事务与恢复。P2 验证跨域调度闭合、真实 Host、固定写作批次、全文事件对账和完整短卷文学质量；P3 才扩展复杂语义依赖、宏微等价和高档容量。

每次后续报告应保存环境、代码/契约/规则版本、输入数据、执行命令、失败样本和实际输出。当前未验证事项仍按 ADR 和阶段出口管理，不能因为本设计包静态 PASS 而删去门槛。
