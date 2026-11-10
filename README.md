> **P0/P1 开发审核版已实现。** 当前运行说明见 [复现与运行指南](reports/复现与运行指南.md)，实际验收与限制见 [P0/P1 集成审计报告](reports/P0_P1_集成审计报告.md) 和 [架构差异与未完成问题](reports/架构差异与未完成问题.md)。以下保留原设计包说明；原设计验证文字不代表本轮业务测试结论。本轮停止于 P1。

# 小说 Agent 工厂 v2.0-R1：统一详细技术设计与开发交接包

**交付日期：2026-11-10。范围：研究、详细设计、规范编制、契约验证与独立小实验；完整业务系统尚待实施。**

本包将统一世界时间线、动态知识演化和大量外部 Agent 的并发访问，整合进入原 v2.0 设计。原 01–12 全部经过实质修订，新增 13–15 展开时间、并发及压测机制；不是另外拼接一套历史库、未来库或状态系统。v1.1 的世界/叙事分离、角色自主、动态剧情和文学创作原则保持不变。

先读 [00_交付总报告.md](00_交付总报告.md) 和 [R1 变更说明](CHANGELOG_R1.md)。开发 Agent 再读取 [AGENTS.md](AGENTS.md)、目标目录的局部规范及 [SKILLS.md](SKILLS.md)，按具体任务加载文档和技能。

## 1. 技术文档导航

| 文档 | 主要责任 |
|---|---|
| [01 详细技术架构](docs/01_详细技术架构.md) | 推荐技术栈、模块化单体、无状态接入、多工作者、权威边界与部署演进 |
| [02 代码架构与模块设计](docs/02_代码架构与模块设计.md) | 完整目标代码树、服务/内部组件/端口、输入输出、依赖、错误与测试责任 |
| [03 数据模型与知识图谱](docs/03_数据模型与知识图谱.md) | 统一实体、动态关系、时态事实、认知、物品生命周期、过程覆盖与索引 |
| [04 事件溯源与版本体系](docs/04_事件溯源与版本体系.md) | 不可变历史、选择清单、严格/受守卫接受、分支、快照、Retcon 和失效 |
| [05 世界模拟与调度引擎](docs/05_世界模拟与调度引擎.md) | 局部前沿、跨域通道、交汇窗口、同时行动、联合裁决与宏微模拟 |
| [06 Agent 编排与管理](docs/06_Agent编排与管理.md) | 角色/任务/调用生命周期、代际固定、预算、租约、取消和失败恢复 |
| [07 剧情与文学生产系统](docs/07_剧情与文学生产系统.md) | 卷级契约、意外重规划、固定批次并行写作、事件对账、审核与整卷编辑 |
| [08 MCP 与工具接口](docs/08_MCP与工具接口.md) | 统一服务下的 SDK/CLI/HTTP/MCP、批量查询、上下文编译和安全提交 |
| [09 长期记忆与检索](docs/09_长期记忆与检索.md) | Context Compiler、证据与硬约束召回、角色隔离、文学记忆和缓存 |
| [10 论文与技术资料库](docs/10_论文与技术资料库.md) | 论文机制到工程模块的映射、一手技术证据、限制与待验证假设 |
| [11 研发路线与测试计划](docs/11_研发路线与测试计划.md) | P0–P4、早期并发基础、短卷端到端 MVP、工程与文学验收 |
| [12 架构决策与风险](docs/12_架构决策与风险.md) | 原 20 项 v2 ADR 与新增 10 项 R1 决策、备选方案及原型门槛 |
| [13 统一时间线与动态知识演化](docs/13_统一时间线与动态知识演化.md) | 四个时间维度、统一查询、事实/认知/计划/候选、历史发现及修订 |
| [14 高并发服务与一致性协议](docs/14_高并发服务与一致性协议.md) | 读取/任务/接受的不同并发策略、依赖守卫、证书、短事务和恢复 |
| [15 并发压测与端到端验收](docs/15_并发压测与端到端验收.md) | 五档行为负载、十类压测、A–H 案例、指标、环境与扩展门槛 |

13–15 是同一架构的专题展开。出现歧义时先核对 [R1 契约说明](contracts/R1_README.md)、ADR 和对应服务责任，不能另造相似字段绕过既有契约。

## 2. 开发交接资产

| 入口 | 实际交付 |
|---|---|
| [contracts](contracts/README.md) | 3 份 Schema、94 个定义、28 个工具、58 个结构正例和 31 个反例 |
| [R1 契约说明](contracts/R1_README.md) | 2.1 增量契约、原 2.0 兼容、跨对象等式、查询切面、证书和批准绑定 |
| [报告契约](contracts/report_templates.md) | Retcon、影响、重验证及正文对账的字段和门禁；机器 DTO 在 P1/P2 实现 |
| [角色目录](runtime/roles/README.md)及[查询配置](runtime/query_profiles.json) | 10 份运行时角色与按角色限制的平面、操作和编译 recipe；实际权限由服务端 grant 决定 |
| [技能索引](SKILLS.md) | 6 个开发技能、9 个运行时技能；按需加载，技能不能授予权限 |
| [目标代码骨架](project_layout/README.md) | 当前资产与将来代码分开；完整目标树及组件职责见 02 |
| [架构图](diagrams/README.md) | 10 份 Mermaid 源文件，含统一时间线和高并发服务图 |
| [原研究来源](research/sources.json)及[论文卡片](research/paper_cards.md) | 保留 v1.1 的 79 个去重 URL 和重点论文实现映射；继承与核查状态明确区分 |
| [R1 证据](research/r1_evidence.md)及[来源清单](research/r1_sources.json) | 23 组官方一手资料，说明事实、工程推论与实测的边界 |
| [负载模型](benchmarks/README.md) | 10/50/200/500/1000 逻辑 Agent，冷/混合/暖缓存共 15 行算术结果及可复现计算器 |
| [原需求矩阵](validation/requirement_matrix.md)及[R1 需求审查](validation/R1_REQUIREMENTS.md) | 原任务与补充任务逐项定位到设计、契约、验收和风险 |
| [原案例矩阵](validation/case_matrix.json)及[A–H 矩阵](validation/r1_case_matrix.json) | 原 44＋16 个案例保留；另有 8 个案例家族、37 条检查，全部待业务验收 |

根规范和 8 份局部 `AGENTS.md` 面向开发 Agent；小说角色和运行时技能独立管理。开发技能以 Codex 的 `.agents/skills` 为主；Claude 适配见 [adapters/claude](adapters/claude/README.md)。本包提供项目文件，没有安装个人技能。

## 3. 复现与证据等级

从包根目录执行：

```bash
python -m pip install -r validation/requirements-validation.txt
python validation/validate_package.py
python -m unittest discover -s experiments -p 'test_*.py' -v
python benchmarks/calculate_load.py
```

依赖清单仅用于设计验证，不是生产依赖。实验和负载计算只依赖 Python 标准库。完整结果见 [验证报告](validation/REPORT.md)、[R1 并发审查](validation/R1_CONCURRENCY_REVIEW.md)及[R1 需求审查](validation/R1_REQUIREMENTS.md)。

本轮实际执行了静态包检查、原 18＋新增 17＝35 项有限机制实验，以及 15 行负载算术和 4 项模型一致性检查。它们分别证明格式/引用一致、选定局部反例得到处理和假设计算可复现。没有启动真实 PostgreSQL、运行多进程服务压测、调用生产模型或完成真实短卷验收；没有任何档位的已实现吞吐、P95/P99 或成本结论。

## 4. 基线、兼容与实施顺序

v1.1 的 14 份 Markdown 原件保留在 [baseline/v1_1](baseline/v1_1/README.md)，本轮逐项核验哈希；[合订审计](validation/baseline_reading_audit.json)保留完整阅读证据。原核心和文学 2.0 Schema 的字节也保持不变；[整合清单](validation/r1_integration_manifest.json)记录它们的哈希及 01–12 的修订证据。

建议顺序：P0 固定契约与环境；P1 建立可信状态、少量封闭命令的依赖守卫、统一查询和多工作者预算；P2 随即完成 5–10 场景的真实短卷生产闭环；P3/P4 再以实测决定扩展。不要因为文档覆盖完整就一次性实现所有可选组件。

`MANIFEST.sha256` 列出最终交付文件内容哈希，不包含它自身。当前 `src/`、`tests/`、`migrations/` 只放必要规范；四个 Python 文件分别用于包检查、两组独立机制实验和负载计算，没有业务占位实现。
