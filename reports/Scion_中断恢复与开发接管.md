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

后续还按阶段 A 清单复核了三个已知历史 Git blob。PR #3 分析 49f5894ef391d4c8184e9b44d10af202e3dff4b6 和规模摘要 02c4fc15826c120b789b7492f0cc4da7f68339a6 已在实验树中。原来未出现在该树的 [PR #3 历史 CI 索引原文](../evidence/scion_20261111/takeover/historical_pr3_ci_index.txt) 通过已知 SHA 找回，Git blob 为 7ed41b6a65d9b423f43aa77adc9f1a3d616408f0，SHA-256 为 de0c32bd9b34e32603f6e59b626e64a0fb68198e4e27c4ea381d8effbe321d2a。三个对象的 Git SHA 和清单中的 SHA-256 均匹配。这些是旧 PR 的诊断，可用于核对历史基线，不含 Scion 原型源码。

接管后复核了旧 Git 提交的可达性。两个旧工作树共用对象库，三个可达提交分别为最初设计资产 033e98e34bcb570012194a0807b949468425244e、P0 的 b02ffa77c7f0eed5a88d62a9be1bfe84e4630d60、P1 的 838a50e4866d3e1d35b1eec78e36994a3d8606c6。三者与接管时远程 PR #4 的完整可达历史（含其自身共 27 个提交）无 SHA 交集；提交身份仅在本地历史中找到。上面的文件差分另行核实了大量内容已进入远程，两种历史不宜合并或重放。

## 旧工作树的可移植恢复证据

下列三个文件已在独立空 Git 对象库中验证。表内 SHA-256 对应仓库实际保存的字节。

| 证据 | 字节数 | SHA-256 |
|---|---:|---|
| [旧 P1 与接管 PR #4 的逐路径比较](../evidence/scion_20261111/takeover/old_p1_vs_pr4_files.tsv.gz) | 143152 | `d6ac41b9720947917be9c5b6cefec151c1832cccb57577c4eba161744c45a6fd` |
| [原始备份逐成员 SHA-256 清单](../evidence/scion_20261111/takeover/previous_backup_SHA256SUMS.gz) | 68976 | `66c40f02cd205c32ae5b0c88329119122120fadadbfd52522765561f76e741f1` |
| [补全旧历史所需的 Git 对象包](../evidence/scion_20261111/takeover/p0p1_missing_objects.pack) | 11179 | `1b50bbc4278f069545419caa9ed92d0a516f216889ef12fb03480a5c26028ba7` |

逐路径比较记录 3209 个路径、双方的 Git 模式及 blob SHA。成员清单仅保存 1267 个相对路径及文件哈希，解压后与接管时原始清单的 SHA-256 完全一致。Git 对象包仅保存旧提交、树、标签及指南对象。本机 Git 配置、生成的凭据、数据库和未跟踪文件的内容均未放入对象包。原始备份中其他现场证据保留在原归档，成员清单用于核验。

接管基线 `2c28acba6e1932657162c6e55b3288365ce3ea23` 的可达历史含 3483 个 Git 对象。旧对象库共有 405 个，其中 397 个可从该基线复用。对象包补全 3 个 commit、3 个 tree、1 个带注解的 tag 和 1 个 blob。验证时先向空裸库导入基线可达的对象，再导入对象包；随后按原值恢复分支及三个标签，并为悬空指南建立恢复引用。Git 严格完整性检查通过，三个提交的递归树逐路径匹配，指南原文 20170 字节，SHA-256 为 `3ffda74864c136341cd5489c07cf20111d9c24c15c95dc6d34bb112e15a6cb38`。

在包含接管基线完整历史及本报告附件的本地检出中，可通过以下命令在独立临时裸库复验。所有新建引用均位于该临时裸库。

```sh
source_repo="$(git rev-parse --show-toplevel)"
audit_repo="$(mktemp -d)"
pack="$source_repo/evidence/scion_20261111/takeover/p0p1_missing_objects.pack"
git init --bare --quiet "$audit_repo"
git -C "$audit_repo" fetch --no-tags "$source_repo" \
  2c28acba6e1932657162c6e55b3288365ce3ea23:refs/recovery/pr4-A
git -C "$audit_repo" index-pack --strict --stdin < "$pack"
git -C "$audit_repo" update-ref --stdin <<'REFS'
create refs/heads/development/p0-p1 838a50e4866d3e1d35b1eec78e36994a3d8606c6
create refs/tags/P0-complete b02ffa77c7f0eed5a88d62a9be1bfe84e4630d60
create refs/tags/P1-complete dc0f3c2ebe8955008f404733af711c0367cb54d4
create refs/tags/design-v2.0-R1 033e98e34bcb570012194a0807b949468425244e
create refs/recovery/p0p1-guide ed3f40fcb1000eed4c1e0cdbcdc7fa718f98a096
REFS
git -C "$audit_repo" symbolic-ref HEAD refs/heads/development/p0-p1
git -C "$audit_repo" fsck --full --strict --no-reflogs
git -C "$audit_repo" cat-file blob refs/recovery/p0p1-guide | sha256sum
```
