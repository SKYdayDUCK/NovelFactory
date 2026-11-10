# 官方平台与工程规范核查

> 核查日期：2026-11-10。本文说明当前文档支持的边界；实际开发的兼容矩阵必须记录所安装Host、SDK、数据库与依赖版本。本轮未在真实Codex/Claude Host上安装本项目技能或执行MCP联调。

## 1. 主平台和技能发现

本项目以 **Codex的仓库级开发指导与技能格式** 为主。官方文档说明：仓库技能在从工作目录到仓库根的`.agents/skills`中发现；先加载名称和说明，按需读取`SKILL.md`；根与局部`AGENTS.md`提供项目指导。来源：[Codex Skills](https://learn.chatgpt.com/docs/build-skills)、[Customization](https://learn.chatgpt.com/docs/customization/overview)。

本项目采取的具体安排：

- `.agents/skills/<name>/SKILL.md` 只放开发技能；文档/Schema更改、测试与迁移流程可按需触发。
- `runtime/skills/<name>/SKILL.md` 由小说工厂自有catalog与角色配置加载，不宣称会被Codex自动发现。
- `SKILLS.md` 是项目导航，不是Host标准入口。用户请求“设计技能文件”不会自动执行个人技能安装。
- `name/description`使用通用格式；平台专有扩展留在适配配置，避免混入共同规范。

[Agent Skills规范](https://agentskills.io/specification)要求单数`SKILL.md`、YAML元数据和匹配目录的技能名。规范中的`allowed-tools`为实验字段；本项目的服务端能力、作品/分支范围和审批绑定不依赖它。

## 2. Claude Code适配：避免使用过时假设

[Claude Code memory官方文档](https://code.claude.com/docs/en/memory)说明，v2.1.277及以后在相应条件下可直接读AGENTS；祖先存在CLAUDE.md或CLAUDE.local.md时默认改读CLAUDE。旧版本或支持关闭的会话仍须使用CLAUDE导入。技能路径则为[`.claude/skills/<name>/SKILL.md`](https://code.claude.com/docs/en/skills)，并有平台特有加载规则。

本项目适配流程是：

1. 先记录Host版本、模式及实际加载的指导文件，选择“直接AGENTS”或“CLAUDE导入”一种路径。
2. 若直接AGENTS可用，不新增空CLAUDE导致AGENTS意外失效。
3. 若采用CLAUDE兼容路径，使用短导入文件引用根规范；按适配清单为有局部AGENTS的目录补对应导入或明确读取，不能只导入根文件就宣称局部约束已生效。
4. 开发技能从`.agents/skills`生成或同步到`.claude/skills`，保留内容哈希和唯一源，避免双份手工维护。普通平台移植无需修改运行时角色权限。
5. 选一个涉及局部规范的合成任务，检查最终输出确实使用该规范；文件存在不等于Host已经加载。

平台的指导文件均不是数据库授权。平台hook或工具许可可加强开发环境，但小说系统权限仍在应用服务校验。

## 3. MCP兼容profile

本阶段建议把 **`2025-11-25`作为首个兼容profile的候选基线**，理由是能用已定义的tools、structured content与stdio完成最小接口。该选择不声明它是最新版，也不声明当前每个Host都支持。P0必须通过官方SDK与目标Host联调后锁定实际版本；如双方只共同支持另一版本，改用其完整profile并记录ADR。

| 部分 | 2025-11-25 profile | 2026-07-28 profile核查结果 | 本项目约束 |
|---|---|---|---|
| 初始化/版本 | `initialize`协商版本和能力 | stdio现代profile使用请求元数据和发现兼容机制 | 不把新版本字符串写进旧实现充当升级 |
| HTTP | Streamable HTTP可含GET SSE和协议会话 | 移除GET stream及协议级sessions | 业务任务不绑定transport会话 |
| 工具结果 | `content/structuredContent/isError`等 | 增加/调整结果协议与请求元数据要求 | 由版本化adapter序列化；领域DTO稳定 |
| 身份 | HTTP授权与stdio凭据入口不同 | HTTP仍为受保护资源的授权模型 | 通过服务器身份派生actor/权限，工具参数不得提升权限 |

官方依据：[旧版Lifecycle](https://modelcontextprotocol.io/specification/2025-11-25/basic/lifecycle)、[旧版Transports](https://modelcontextprotocol.io/specification/2025-11-25/basic/transports)、[旧版Tools](https://modelcontextprotocol.io/specification/2025-11-25/server/tools)、[新版stdio](https://modelcontextprotocol.io/specification/2026-07-28/basic/transports/stdio)、[新版HTTP](https://modelcontextprotocol.io/specification/2026-07-28/basic/transports/streamable-http)、[新版Tools](https://modelcontextprotocol.io/specification/2026-07-28/server/tools)。

MVP采用本地stdio：协议stdout不得混入日志，凭据由受控进程环境传入。远程接入再部署HTTPS与[匹配版本的MCP HTTP授权](https://modelcontextprotocol.io/specification/2025-11-25/basic/authorization)；授权、Origin、受众和scope检查随所用SDK验证。仅监听localhost不替代身份校验。

长任务返回应用层`task_id`，后续查询任务与提交收据；应用取消使任务代际失效。transport取消只能停止请求等待，不能推断已经接受的事件被撤销。默认不依赖MCP实验tasks、sampling、elicitation、共享会话内存；开启前独立验证。

## 4. 数据库和Python实现注意事项

推荐PostgreSQL17维护分支；[官方版本政策](https://www.postgresql.org/support/versioning/)显示其本轮仍受支持。补丁版本在实施时按安全维护状态锁定，不在设计文档伪造已安装版本。SQLAlchemy2.0为维护分支；[官方Session说明](https://docs.sqlalchemy.org/en/20/orm/session_basics.html)要求每线程/异步任务独立Session。P0可以比较2.1兼容性，但未经测试不静默升级。

PostgreSQL可实施行锁、唯一键、外键与排斥约束；跨行不变量不能塞入引用其他行的CHECK函数。`SKIP LOCKED`适合领取任务，不适合查询完整世界状态。[约束](https://www.postgresql.org/docs/17/ddl-constraints.html)｜[锁](https://www.postgresql.org/docs/17/explicit-locking.html)｜[SELECT](https://www.postgresql.org/docs/17/sql-select.html)。

备份采用数据库一致副本与正文/对象清单联合核验；[pg_dump](https://www.postgresql.org/docs/17/app-pgdump.html)不自动涵盖集群角色或外部对象。恢复测试需要检查原事件ID、分支头、快照与文本哈希，不能只以数据库能启动作为成功。

[Pydantic strict模式](https://pydantic.dev/docs/validation/latest/concepts/strict_mode/)可减少隐式类型转换，但JSON日期等仍有具体类型规则。契约必须同时检查格式、额外字段、枚举和跨字段业务语义；不要把“能转成int”视为合法故事时间，也不要把“符合JSON Schema”视为有提交权限。

## 5. 工作流框架的适用位置

[LangGraph持久化](https://docs.langchain.com/oss/python/langgraph/persistence)区分线程checkpoint和跨线程store；[故障容错](https://docs.langchain.com/oss/python/langgraph/fault-tolerance)明确节点重试有至少一次执行语义，外部副作用须自行幂等，丢回执不等于操作未发生。

本项目首期数据库状态机可满足流程、租约与恢复需求。若后续工作流图复杂到难以维护，可以把LangGraph作为编排层适配器，调用同一服务。领域接受、审批、task generation、effects去重仍由内核执行。不能同时让图checkpoint和世界库各自保存一份可自由修改的权威世界状态。

## 6. P0平台验收最小清单

- Host：根/局部AGENTS或兼容导入实际加载；开发技能显式与隐式调用分别验证。
- MCP：列工具、输入错误、结构化输出、超时、断线、重复请求、身份越界和版本不兼容有可解释结果。
- Python：锁定解析出的依赖及支持版本；契约校验方式与JSON示例一致。
- PostgreSQL：目标版本运行迁移、并发领取/提交、约束失败、事务重试与备份恢复。
- 运行时：更换Host或技能版本不能改变服务端角色能力；数据中的指令不能升格为权限。

这些是后续真实环境验证门槛，本轮的文档核查不能替代它们。
