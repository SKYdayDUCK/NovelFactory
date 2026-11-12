# P5.8 历史分支归档与恢复

本目录的 `cleanup-plan.json` 固定经审阅的分支名和 40 位 Git 对象。归档时使用的正式 main 前提提交保存在 `baseline`。原 Scion PR #4 的 base、head、已复验的新 Scion 实验分支、P5.7 独立审查分支、main 和本轮开发分支始终保留。

## 复核清单

在非浅克隆中执行以下命令。需要 Git、Python 3.10+ 和已具备本仓库读取权限的 GitHub CLI。首个命令将所有远端 heads 对应的提交取到本地。后续命令均不会写远端。

```bash
git fetch --prune --no-tags origin '+refs/heads/*:refs/remotes/origin/*'
python scripts/p58_branch_archive.py inventory --out /tmp/p58-review
gzip -dc /tmp/p58-review/branch-inventory.json.gz > /tmp/p58-review/branch-inventory.json
python -m json.tool /tmp/p58-review/cleanup-plan.json
python scripts/p58_branch_archive.py rehearse \
  --inventory /tmp/p58-review/branch-inventory.json.gz \
  --plan /tmp/p58-review/cleanup-plan.json \
  --remote "$(git remote get-url origin)" --out /tmp/p58-backup
```

`branches[]` 逐条列出 tip、所有 merge-base、相对 main 的双向提交数、独有提交，以及相对确定 merge-base 的 diff 路径与两端 blob OID。每个仅从该分支历史可达的 blob 都保留一个代表性历史路径、大小和 OID，涵盖已从 tip 删除的文件。分支名的跟踪文件引用含路径和行号。开放 PR 两侧的仓库、分支与 SHA、运行中的工作流、活动 Issue/PR 正文/评论/评审里出现的分支名和仓库内依赖旧分支名的证据链接收录在同一份清单。外部调用方若存在仅在仓库外维护的活动依赖，应在审核时将对应名称和 OID 从删除表移入保留表，记录理由。

| 类别 | 可核对的依据 | 处理 |
| --- | --- | --- |
| A | tip 可从正式 main 回溯，且未发现活动依赖 | 完整恢复通过后可删除引用 |
| B | tip 含尚未进入 main 的提交或对象 | 归档原始历史，下载 release 实物重建，通过后可删除引用 |
| C | 正在使用的线路、PR、保护设置、运行中工作流或跟踪的运行依赖 | 保留远端引用及原因 |

同一分支还可能出现在历史验收文档中。`references_in_tracked_text` 保留这些原始位置；当源文档使用分支作为当前运行依赖时，应保留该分支。公开 GitHub 层可能存在清单之外的消费者，评审记录应说明已核对的范围。变更须将经审阅的清单和计划一并提交至本目录。只有从已合入的、复验后的 P5.8-A 正式基线生成的清单可用作执行输入。

## 归档与远端执行

`.github/workflows/p58-branch-archive.yml` 在计划进入 main 时运行，或从 main 手动重新执行。同一时间最多有一个治理任务。工作流使用本仓库的临时 Actions token，读取公开的分支规则、PR 和运行状态，写入 release 和已审阅分支引用。工作流执行前需要仓库授予该任务相应权限；返回 403、发布失败、快照漂移或无法恢复时，脚本中止并保留尚未提交的变更。

1. 将每条独有 tip 与相对固定 main 新增的所有 Git 对象写入增量 bundle。记录与该清单绑定的 SHA-256 回执。
2. 从远端仅取正式 main，在另一个空 bare 仓库取固定前提提交。载入 bundle，逐条恢复所有已审阅名称、核对 tip，并执行 `git fsck --full`。仓库之间以 Git 传输协议复制，均不使用 alternates。
3. 以 `p58-history-<清单摘要前16位>` 发布归档 release，并将标签指向固定前提。四份 release 资产为 bundle、清单、计划、恢复回执。从该 release 下载资产后重新校验标签、SHA-256 和清单，在另一组空仓库中再次恢复。既有 release 可以复用，每次仍须完整下载校验。
4. 再读取远端最新 main、heads、开放 PR、活动运行与有效分支规则。校验名称及原 SHA、保留名单和代码依赖。以一次 Git 原子事务删除仍符合条件的确切引用；任一旧 SHA 已改变会使整批删除失败。GitHub 元数据和 Git 事务属于不同系统，开放 PR 会在执行前最后一次检查，引用 SHA 由 Git 服务端在事务中核对。
5. 下载发布的 `p58-refs-<run_id>-<attempt>.json` 可核对完整 `before`、`deleted`、`after`、独立发生的 tip 漂移、清理前后的开放 PR 与归档身份。只有 `state=VERIFIED` 表示已完成后验核对；传输故障时保留 `UNCONFIRMED`，其中未知字段为 null。最终回执也会下载比对。若上传最终回执时网络失败，可用工作流原始日志及远端状态核查，重新执行只处理仍存在且原 SHA 相同的目标。变更过的分支需另行审查。

## 从已发布的资产恢复

在 WSL2 或 Linux 的项目检出中执行。目标目录须为空。历史分支无需仍然存在，若正式 main 已前进，也可以直接使用 release 中的前提标签。

```bash
set -euo pipefail
repo=SKYdayDUCK/NovelFactory
inventory=reports/branch_archive/p58/cleanup-plan.json
tag=$(python -c 'import json,sys; print("p58-history-"+json.load(open(sys.argv[1]))["inventory_sha256"][:16])' "$inventory")
assets=$(mktemp -d)
recovery=$(mktemp -d)
gh release download "$tag" --repo "$repo" --dir "$assets"
python scripts/p58_branch_archive.py restore \
  --inventory "$assets/branch-inventory.json.gz" --plan "$assets/cleanup-plan.json" \
  --archive "$assets/p58-history.bundle" --receipt "$assets/p58-archive-receipt.json" \
  --remote "https://github.com/$repo.git" --base-ref "refs/tags/$tag" --out "$recovery"
git -C "$recovery/recovered.git" for-each-ref refs/p58-restore \
  --format='%(objectname) %(refname:strip=2)'
git -C "$recovery/recovered.git" log -1 refs/p58-restore/experiment/scion-incremental-20261111
```

使用清单内的原始 commit 和路径，例如 `git -C "$recovery/recovered.git" show "$oid:$path"`，可直接核查历史实验数据。回执中的 `bundle_sha256` 是该次已上传二进制资产的 SHA-256。重新打包可能产生另一份有效的 bundle；恢复核验使用已发布的原件和同一回执。

本轮[逐分支审查](BRANCHES.md)登记 91 个远端引用。31 个已在正式 main 中，50 个有独有历史，另 10 个有保留依据。91 个冻结 tip 的[发布前恢复回执](raw/prepublication-restore.json)与[Scion 独立验收](scion/README.md)在本目录。真实远端事务的回执将随[归档 Release](https://github.com/SKYdayDUCK/NovelFactory/releases/tag/p58-history-4157e475a9b33ba2)发布。

[已执行的最终签收](FINAL.md)记录 91 → 10 的实际远端事务、原始 GitHub 回执与删除后的独立恢复结果。
