# Claude Code 项目适配说明

本项目的规范主源是根/局部 `AGENTS.md`，开发技能主源是 `.agents/skills/`。此目录只交付适配说明与模板，没有安装个人技能，也没有在项目根创建生效的 `CLAUDE.md`。

## 1. 先核查实际加载行为

核查日期为 2026-11-10。[Claude Code memory 官方文档](https://code.claude.com/docs/en/memory)说明，v2.1.277 及以后的相关版本，在祖先目录没有 `CLAUDE.md`、`.claude/CLAUDE.md` 或 `CLAUDE.local.md` 等条件下可以直接读取根和局部 `AGENTS.md`；已有 CLAUDE 指导时默认加载路径会变化。某些版本还提供配置选择，实施时以实际 Host 版本和配置为准。

因此，不要依据“存在 AGENTS 文件”或“版本足够新”直接宣称适配完成，也不要先放入一个空 CLAUDE 文件。P0 记录 Host 版本、启动目录、祖先指导文件及实际加载清单，再选下表的一条路径。

| 路径 | 适用情形 | 本项目处理 |
|---|---|---|
| 原生 AGENTS | 目标 Host 已实测加载根和局部 AGENTS | 保留主源；不额外生成会改变发现行为的 CLAUDE |
| CLAUDE 导入 | 旧版本、原生支持关闭，或已有 CLAUDE 工作流 | 采用下面的短导入，并核查局部规则 |

本项目没有使用 `AGENTS.override.md`；不得假定所有 Host 对覆盖文件、嵌套目录和技能发现规则都一致。已有团队配置应按差异合并，不覆盖用户原有指导。

## 2. 兼容导入路径

仅在确定采用 CLAUDE 路径时，将 [CLAUDE.md.template](CLAUDE.md.template) 的内容合并至项目根 `CLAUDE.md`。模板中的 `@AGENTS.md` 在根目录解析，不能直接在 `adapters/claude/` 中改名后宣称已生效。

根导入不等于局部导入已经生效。为下面每个已有局部规范的目录，按目标 Host 支持情况建立短 `CLAUDE.md`，内容引用同目录的 `@AGENTS.md`；若团队已有同目录 CLAUDE，只增加引用，不覆盖既有内容。也可采用目标 Host 已验证的其他加载配置，但须保留等价测试。

| 目录 | 主源 | 兼容文件中的引用 |
|---|---|---|
| `src/novel_factory/domain/` | `AGENTS.md` | `@AGENTS.md` |
| `src/novel_factory/application/` | `AGENTS.md` | `@AGENTS.md` |
| `src/novel_factory/interfaces/` | `AGENTS.md` | `@AGENTS.md` |
| `src/novel_factory/infrastructure/` | `AGENTS.md` | `@AGENTS.md` |
| `contracts/` | `AGENTS.md` | `@AGENTS.md` |
| `migrations/` | `AGENTS.md` | `@AGENTS.md` |
| `tests/` | `AGENTS.md` | `@AGENTS.md` |

这些兼容文件不复制规范正文，避免日后出现两份相互矛盾的规则。不要在开启原生 AGENTS 路径的环境中同时默认生成全部导入文件。

## 3. 开发技能适配

[Claude Code Skills 文档](https://code.claude.com/docs/en/skills)的项目技能目录是 `.claude/skills/<name>/SKILL.md`，与本包 Codex 主路径不同。

采用以下可检查的同步方式：

1. 保留 `.agents/skills/<name>/SKILL.md` 为唯一编辑主源。
2. 在需要 Claude 项目技能的仓库，将选中的开发技能目录生成到 `.claude/skills/`；同一深度下的相对参考链接仍指向仓库根。
3. 记录每个生成文件的源路径和 SHA-256；同步后检查正文和 frontmatter 一致。已有同名技能先比较差异，不直接覆盖。
4. 在目标 Host 显式触发技能，检查按需引用能够加载；隐式触发是否可用另按版本验证。
5. 若 Host 同时发现两个目录，通过其受支持的配置保留一个有效来源，避免同名技能重复出现。

运行时 `runtime/skills/` 由工厂自己的加载器管理，不复制为 Claude 开发技能。共同格式只用 `name/description`；平台专有前置字段、hooks 或工具配置需要单独适配与验证。`allowed-tools` 一类技能元数据不能替代服务器授权。

## 4. 最小兼容验收

选一个只修改 `contracts/` 示例的合成任务，确认 Agent：

- 能报告根和局部规范的来源，并遵守示例与 Schema 一致的要求。
- 能按任务读取 `nf-contract-change`，不一次加载全部运行时角色和技能。
- 没有因适配获得管理提交、任意角色身份、数据库生产凭据或外部发布能力。
- 读取失败时报告具体文件/加载问题，不假装已遵守未加载规范。

验收记录包含 Host 版本、配置路径、输入、实际加载清单、观察结果和剩余限制。本轮只核查官方文档并交付模板，尚未在真实 Claude Host 安装或运行这些项目技能。更多依据见 [官方核查](../../research/official_platform_notes.md)。
