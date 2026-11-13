# P6 职责与指令核对

核对起点为 PR #35 的 `eacb7771ba1f41d2f73d642b7672a252ea665e25`。角色和技能新源提交为 `e7dc34d0c99f6f6d30117d1604cbda7945ba9d42`。仓库指导文件仅供开发和宿主阅读，运行时任务不会递归读取其链接。

| 检查范围 | 发现与处置 | 验证入口 |
|---|---|---|
| 根 AGENTS.md、SKILLS.md | 清理 P5 阶段性收尾和强制串行合并说明；列明开发、内部创作、独立评审的交接与恢复 | 目录链接检查、运行时 snapshot 读取清单 |
| application、domain、interfaces、infrastructure、contracts、migrations、tests、benchmarks 的局部 AGENTS.md | 保留事务、幂等、认知和范围约束；修正测试文档过时的阶段范围 | 各目录规范、PostgreSQL 集成测试 |
| Workshop 适配器 AGENTS.md 与 nf-resume | Workshop 组织恢复与作品恢复分别使用已安装 MCP 的专用入口，后台状态以调度回执和 Worker 活性为据 | `aw_resume_project`、`nf_resume_project` 的注册与恢复测试 |
| 七项开发技能 | nf-pipeline-change 增加禁代写与错误类别。其余并发、契约、内核、恢复、研究和项目唤醒技能保留各自触发条件与工具边界 | `validation/validate_package.py` 检查 frontmatter、路径与工具目录 |
| 十份角色、十二份生产技能及两个目录 | 统一到 2.3.0，删去重复通用协议、过期的 2.0.0 标题和诱导无工具模型操作的伪命令。卷导演的实际模式补齐 volume_review。按本次输出 Schema 交付，评审与正式接受分开 | 快照字节与 Git blob 对读；旧版 H1 快照重建；核对角色、模式与技能的允许集合 |
| Workshop 对接与 NF 任务 | 更新 Workshop 编制的角色/技能来源清单。保留任务输入字节与旧源版本的核验；岗位资格由 Workshop 的 NF_ROLES 限定，作品材料由 NF 授权。 | `workshop/scripts/freeze_nf_catalog.py`、`validation/validate_creative_harness.py` |

表中的检查入口是复验方式。演绎、长文质量、真实 Provider 可用性及最终 RELEASE 以各作品各自的执行记录验收。
