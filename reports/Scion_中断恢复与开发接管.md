# Scion 中断恢复与开发接管

2026-11-11，北京时间。接管时通过 GitHub API 读取了三个 PR、实验分支 ref 及对应 Git tree，随后在新目录只读克隆。分支关系为 main（721f155c9cc912cddfdae559afe4f2516d0967f2）、PR #2（a9547bd651e177d48c9d0e810362e411f510d62d）、PR #3（23a61398278c9a13e6450d444d4a6d25a88ef5e0）、PR #4（2c28acba6e1932657162c6e55b3288365ce3ea23）。三个 PR 均开放且为 Draft。实验分支的树为 6a30d048925a3f0e7021eed6de64d22aa9b9d61b。

## 旧工作与备份

先检查当前可访问的项目工作目录、本地 Git refs、不可达对象、归档及上传记录。找到 P0/P1 工作树 838a50e4866d3e1d35b1eec78e36994a3d8606c6 和 P0 工作树 b02ffa77c7f0eed5a88d62a9be1bfe84e4630d60。两处工作树均干净。唯一找到的不可达 blob ed3f40fcb1000eed4c1e0cdbcdc7fa718f98a096 是 P0/P1 运行指南。逐路径比较旧 P1 与远程：288 个模式及内容一致、25 个修改、2896 个新增、0 个删除。未在可访问工作树、8 个项目 ZIP 索引及已定位的对象中识别出阶段 B/C/D 的源码或上传映射。无法从分支树枚举 GitHub 上所有无引用对象，也无法断言其他隔离环境的文件已删除。

先归档后比较，保留了两个旧工作树、Git 对象、证据与审计材料。恢复归档含 1267 个文件，成员已逐项回读，归档 SHA-256 为 d4d7736279e5767bb5a2ec9ad3e717848d3a0a10cc3c8a9f2f224e7a2d66b63b，文件哈希清单 SHA-256 为 c4b9b13a5cbf670acc4cab14265bb6448c2b703014677f40a71f3183b6de04e0。旧目录未经清理、重置或覆盖。

## 接管判定与 CI

远程阶段 A 含原始研究、归因脚本及 PG17 数据。后续只使用 phase_a/profile_v2.json 作为阶段 A 的描述性基线，首轮 profile.json 的 codec 重复注册干扰已被记录。阶段 B/C/D 均需新验证。在干净的实验分支上可以继续，未对依赖分支或 main 执行写入。

[基础 CI 42871011730](https://github.com/SKYdayDUCK/NovelFactory/actions/runs/42871011730) 对应的失败需按作业定位。schema 作业 130021976434 报两份报告各 4 个未生成文件的链接，PG 作业 130021975869 在标准包签名密钥下载上遇到 curl 56。A0 把计划改为纯文本，优先复用已安装的 PG17 客户端及包索引，必要时有界重试，单独检查三个客户端的主版本。保留 PostgreSQL 完整回归、所有容量工况与原阈值。

其他历史运行：[42871011881](https://github.com/SKYdayDUCK/NovelFactory/actions/runs/42871011881)、[42871011544](https://github.com/SKYdayDUCK/NovelFactory/actions/runs/42871011544)、[42871011484](https://github.com/SKYdayDUCK/NovelFactory/actions/runs/42871011484)。容量结果按单个工况解读，不能用基础回归替代。B/C/D 的启用条件、差分和 A/B 以各自检查点为准。

A0 本地复核：包验证通过，35 个机制实验通过，既有单元、契约及 admission 子集 357 项全部通过。
