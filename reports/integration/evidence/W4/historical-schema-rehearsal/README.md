# W4 历史源码与前向迁移复演

独立 Linux 环境分别检出 Workshop `4f7d8ca35bdf0e04045986950edf5819bd572e80` 和已签收的 NF `e6763d844f023d7c6a13060dbaebca0416fe3099`。原版 Workshop 的 57 项、发布 W0 升级 1 项、NF 工具 5 项、双服务 2 项、原生双 MCP 和物理 PITR 2 项均通过。另复跑含于上述 57 项的历史审核及 Skills 集合 18/18。旧版配对测试在一套临时 PostgreSQL 实例中建两个数据库与独立服务身份，两服务隔离验收则使用两套真实 PG17.11 集群。无商业模型调用。[来源 SHA、原始 6 组 JUnit、服务身份与 WAL](source-4f7d8ca-rehearsal.zip)

已部署的旧 w004b 图使用空合流修订 w002b。W4 集成基线 `6486c7570cb467d3a73f4f67399af4e814005266` 的 w004b 库即使执行 upgrade head，仍缺少正式 W2 的 `aw_current_message_edges`。在该基线实际运行原应用建库的回归用例，1 项按预期失败。[修复前原始日志和 JUnit](before-fix.zip)

补丁 `469f7f588769b1bb87a1844f7a2d6c2008abcb48` 在 w004c 恢复缺失的正式视图。4 项实测均通过。早期 w004、w004a、w004b 的 31、34、34 张原表所有已存在列和行摘要保持一致，三者的表、列、索引、约束及视图目录与全新库相同。各库的三份冻结 Skill 快照、两份已结算的受控 NF 回执和三个原始审核处置均可复查。旧 ASK 边保留在组织历史，当前路由隐藏该边，合法 REPORT 可用。对已有正式视图的库升两次后，视图 OID、ACL 与定义均不变。[正例 JUnit、逐表摘要及 PostgreSQL 原始记录](forward-migration.zip)

此页记录 W4 开发预演。正式候选及其合并后的主线分别以独立 SHA 验收。
