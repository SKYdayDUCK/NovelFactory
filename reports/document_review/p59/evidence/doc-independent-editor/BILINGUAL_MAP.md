# Bilingual reading map

All ten chapters have a corresponding document in the other language. Code, operator arguments, amounts, workload parameters, schema identifiers and fixture output are shared. Different verbs and sentence order in the narrative are deliberate editorial choices, subject to the meanings below.

| Chinese reader entry | English reader entry | Claim or action compared |
| --- | --- | --- |
| README.md | README.en.md | Letter delivery, character knowledge and future reveal; navigation; six-scene command; test/capacity status and license. |
| 01-项目介绍 | 01-project-overview | Scene planning, independent plan decision, versioned draft, accepted event, continuity and operating identities. |
| 02-快速开始 | 02-quick-start | Git checkout, locked uv environment, temporary PostgreSQL 17, output files, six scenes, 15 deterministic fixture calls, 0 paid calls and reader assessment. |
| 03-系统架构 | 03-system-architecture | Separate databases, installation delegation, internal supervisor boundary, world/actor/time cuts, task transitions and accounting. |
| 04-Agent系统 | 04-agent-platform | Employment revision vs. task generation, role-tool authorization, skill route and expiry, actor independence, separate grant to delegate. |
| 05-Agent-Runtime | 05-agent-runtime | DeepSeek, compatible HTTP and pinned Codex host; explicit override plus 11 levels; profile activation, same-attempt reconciliation and spending authority. |
| 06-小说生产流水线 | 06-production-pipeline | Two-volume book JSON; isolated directory per work; 32 chapters/32 scenes/8 candidates/128 nodes, two-volume lookahead; drafts, advice, adoption and review. |
| 07-接口参考 | 07-api-reference | All 98 entries in the same order; shared exact catalog and examples. Checked endpoint naming, fixture boundaries, two-service credentials and management commands. |
| 08-配置与部署 | 08-configuration-and-deployment | Distinct NF and Workshop service users and DBs, loopback ports, Redis indexes, schema migrations, zero-budget bootstrap and operator policy. |
| 09-运维与恢复 | 09-operations-and-recovery | EventHub repair, backup manifest, empty restore target, PostgreSQL WAL and paired service-recovery receipt/hold reconciliation. |
| 10-性能与实验 | 10-performance-and-evaluation | Source SHA, timing and SQL boundary, isolated route profiler, overload and continuous read results, microbenchmark regressions and ISOLATED/CAPACITY distinctions. |

| Code identifier or source term | Chinese | Meaning held constant in both languages |
| --- | --- | --- |
| work / work_id | 作品 / 作品标识 | One novel and its scope. A job or assignment is a task. |
| branch / revision / head | 分支 / 版本 / HEAD | A mutable branch head selects an immutable world revision. |
| grant / binding | 授权 / 绑定 | Authority is rechecked when used. A frozen binding identifies the assignment and available inputs. |
| generation / employment revision | 任务代际 / 任职版本 | A task execution generation and a Workshop employee's tenure are different counters. |
| knowledge cut / BELIEF | 知识切面 / 角色认知 | Role-visible material is restricted by identity and acquisition/story time at a concrete revision. |
| candidate / advisory review / receipt | 候选 / 咨询性审核（阅读意见）/ 回执 | A proposal or draft, an advisory judgment, and documented evidence of a state transition have different status. |
| Attempt / UNKNOWN | Attempt（一次模型调用）/ 结果或结算待核对 | Token usage, settlement, task result and outstanding reservation can be known independently. |
| provider profile / routing policy | 供应商配置 / 路由策略 | Operator-managed choices; caller prompts do not grant budget, capabilities or authorization. |
| wire starts / validated replies | 网络请求起发数 / 验证有效的响应数 | Different counting boundaries in load evidence. |
| NOT_MET / NOT_EXECUTED / NOT_EVALUATED | 门槛未达到 / 未执行 / 尚未评价 | Different evidence states. The engineering result does not supply a literary assessment. |
