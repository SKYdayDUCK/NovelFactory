# PR 合并与 main 验收记录

日期：2026-11-11（北京时间）。仓库：[SKYdayDUCK/NovelFactory](https://github.com/SKYdayDUCK/NovelFactory)。

## 1. 实际合并状态

| 对象 | 本轮结果 | 实际范围 |
| --- | --- | --- |
| [PR #2](https://github.com/SKYdayDUCK/NovelFactory/pull/2) | **未合并，Draft，merged=false** | 原热点/1000RPS强制容量未通过；修复已远程提交。 |
| [PR #3](https://github.com/SKYdayDUCK/NovelFactory/pull/3) | **未合并，Draft，merged=false** | 依赖PR2；1000只读/600秒仍NOT_MET；base仍dev/p2-production-loop。 |
| main | **未变更** | `721f155c9cc912cddfdae559afe4f2516d0967f2`，仍为P1。 |
| Ready/merge/retarget | 未执行 | 未满足用户授权的条件，没有跳过PR2先合PR3。 |
| 版本标签 | 未创建 | 没有把工程候选命名为全面生产验收版本。 |

重新查询时两PR均open、Git图mergeable=true、mergeable_state=unstable，无评论/审查线程；分支无冲突不能替代用户强制验收。没有取得merged=true，没有新的main合并SHA，因此不声明集成完成。

## 2. 本轮实际远程源码检查点

| 阶段 | Commit SHA | Tree SHA | 父提交与内容 |
| --- | --- | --- | --- |
| P2 第一修复 | `0a0af490c7768fe4b7d2da16dbaa74f0d9585f0d` | `8db652361f4e67819e753767772093206fa553dc` | parent e0b0f2b；初始快照复用、批量SQL、观测/重复CI、第一证据。 |
| P3 恢复修复/继承 | `008fccf23291c6f74693e450a88c224a436be7f6` | `350a3bd277ed1405e6e2ff4333315c4707c25eff` | parents 24b203f + 0a0af49；完整runtime集合检查、P3规模触发、旧main兼容。 |
| P2 第二修复 | `971fbef29eceb422d77271d11841bae1abdfd73f` | `e87b88d343e47361d16161f2721a596083a71085` | parent 0a0af49；严格canonical等价优化、冻结差异测试、正负配对、全部首轮CI失败证据。 |
| P3 最终应用代码 | `e3c7b79b0058656f11257ea55edc75371382b73e` | `72328df65eafff795a5df1a10117c68c2d899bb6` | parents 008fccf + 971fbef；继承第二修复、客户端校准、PITR完整附件。 |

全部使用GitHub连接器create_blob→create_tree→create_commit→update_ref(force=false, expected_sha)。每次先读取当前分支HEAD/tree，随后读回完整树、所有blob与关键源码；第二轮P2核对2477个blob、P3核对2584个blob。未执行CLI远程push/merge/force或重写已发布历史。对象SHA与上传映射已保留，失败上传可续传而不需重做开发。

PR3以额外父提交继承P2，保留堆叠祖先。最终应用树上merge-base(971fbef,e3c7b79)=971fbef，PR3相对P2是P3/P4增量；不是反向合并main。资料收尾提交仅保存本报告、摘要和原始证据；它的身份由包含这些文件的Git提交记录确定，以上实验只绑定已核验的代码SHA，不冒充对未来提交的测量。

## 3. 实际 CI 运行

### P2 第一修复 0a0af49

| 触发 | 工程 | 四格容量 | 独立200/Context |
| --- | --- | --- | --- |
| PR | [42855526952](https://github.com/SKYdayDUCK/NovelFactory/actions/runs/42855526952) SUCCESS；436 PG/240子集 | [42855526861](https://github.com/SKYdayDUCK/NovelFactory/actions/runs/42855526861) FAILURE | [42855527063](https://github.com/SKYdayDUCK/NovelFactory/actions/runs/42855527063) FAILURE |
| push | [42855274085](https://github.com/SKYdayDUCK/NovelFactory/actions/runs/42855274085) SUCCESS；436/240 | [42855273814](https://github.com/SKYdayDUCK/NovelFactory/actions/runs/42855273814) FAILURE | [42855273912](https://github.com/SKYdayDUCK/NovelFactory/actions/runs/42855273912) FAILURE |

PR实际checkout 6eadcc325a75ccb7d600c56cb4c023d367dc9ede 的tree等于0a0af49。18/18 Artifact ZIP digest匹配，20次容量原始记录和四JUnit长期归档。

### P3 第一修复 008fccf

| 触发 | 工程 | 规模500/1000/600秒 | PITR | 独立200 |
| --- | --- | --- | --- | --- |
| PR | [42856323577](https://github.com/SKYdayDUCK/NovelFactory/actions/runs/42856323577) SUCCESS；595 PG/344子集 | [42856323313](https://github.com/SKYdayDUCK/NovelFactory/actions/runs/42856323313) FAILURE | [42856323203](https://github.com/SKYdayDUCK/NovelFactory/actions/runs/42856323203) SUCCESS | [42856323239](https://github.com/SKYdayDUCK/NovelFactory/actions/runs/42856323239) FAILURE |
| push | [42856309898](https://github.com/SKYdayDUCK/NovelFactory/actions/runs/42856309898) SUCCESS；595/344 | [42856309344](https://github.com/SKYdayDUCK/NovelFactory/actions/runs/42856309344) FAILURE | [42856309403](https://github.com/SKYdayDUCK/NovelFactory/actions/runs/42856309403) SUCCESS | [42856309340](https://github.com/SKYdayDUCK/NovelFactory/actions/runs/42856309340) FAILURE |

PITR的恢复断言成功，但旧artifact仅保留manifest的8/11项；这是真实证据缺口，不把它写成“全包可恢复”。后续补齐新包，旧包原样保留。

### P2 最终代码 971fbef

| 触发 | 工程 | 四格容量 | 独立200/Context |
| --- | --- | --- | --- |
| PR | [42857853036](https://github.com/SKYdayDUCK/NovelFactory/actions/runs/42857853036) SUCCESS；472 PG/276子集 | [42857853046](https://github.com/SKYdayDUCK/NovelFactory/actions/runs/42857853046) FAILURE | [42857853112](https://github.com/SKYdayDUCK/NovelFactory/actions/runs/42857853112) FAILURE |
| push | [42857838549](https://github.com/SKYdayDUCK/NovelFactory/actions/runs/42857838549) SUCCESS；472/276 | [42857838507](https://github.com/SKYdayDUCK/NovelFactory/actions/runs/42857838507) FAILURE | [42857838475](https://github.com/SKYdayDUCK/NovelFactory/actions/runs/42857838475) FAILURE |

PR checkout 4eec0c5d 的完整SHA及Git commit回读记录在ci_stage3/checkout_tree_proof.json；tree与971fbef完全相同。18/18 ZIP digest匹配。全部20次负载保留；正常全部通过，热点世界效果完成而容量失败。

### P3 最终代码 e3c7b79

| 触发 | 工程 | 四格容量 | 独立200/Context | 规模 | PITR |
| --- | --- | --- | --- | --- | --- |
| PR | [42859219253](https://github.com/SKYdayDUCK/NovelFactory/actions/runs/42859219253) SUCCESS；631 PG/380子集 | [42859219454](https://github.com/SKYdayDUCK/NovelFactory/actions/runs/42859219454) FAILURE | [42859219256](https://github.com/SKYdayDUCK/NovelFactory/actions/runs/42859219256) FAILURE | [42859219338](https://github.com/SKYdayDUCK/NovelFactory/actions/runs/42859219338) FAILURE | [42859219591](https://github.com/SKYdayDUCK/NovelFactory/actions/runs/42859219591) attempt2 SUCCESS |
| push | [42859202765](https://github.com/SKYdayDUCK/NovelFactory/actions/runs/42859202765) SUCCESS；631 PG/380子集 | [42859202233](https://github.com/SKYdayDUCK/NovelFactory/actions/runs/42859202233) FAILURE | [42859202425](https://github.com/SKYdayDUCK/NovelFactory/actions/runs/42859202425) FAILURE | [42859202367](https://github.com/SKYdayDUCK/NovelFactory/actions/runs/42859202367) FAILURE | [42859202666](https://github.com/SKYdayDUCK/NovelFactory/actions/runs/42859202666) SUCCESS |

PR实际checkout 9fd27f6dae5a157e29d92b3797764d0d74683a5c，连接器读回tree与e3c7b79同为72328df65eafff795a5df1a10117c68c2d899bb6。

PITR PR attempt1在下载PostgreSQL签名公钥时curl56，应用安装/恢复未执行且无artifact；保留job129980491802日志和元数据后，仅通过连接器重试失败job，attempt2通过。push与PR成功ZIP均逐成员核对11/11及完整SHA。重试基础设施失败没有改源码或门槛，也没有重跑容量挑选好成绩。

所有检查结论区分测试失败、容量NOT_MET和基础设施失败。工程artifact中的验收矩阵仍含PARTIAL_WITH_STATED_BOUNDARIES、BLOCKED_ENVIRONMENT、PENDING_AUTHOR；10Agent smoke不能替代全四格负载。没有用工程绿色隐藏容量失败。

## 4. main 验收范围

| 项目 | 当前main上的结论 |
| --- | --- |
| P0/P1内核 | 仍为原已验收721f155；本轮用该实际源码建立旧数据库/历史做兼容验证。 |
| P2完整生产链路 | 尚未合入main；在P2修复分支测试通过的工程范围不能冒领为main状态。 |
| P3/P4高级模块 | 尚未合入main；PR3保持独立增量。 |
| Schema/迁移 | 分支原契约/迁移与旧main升级实验通过；main没有新增本轮迁移。 |
| 历史/代际/权限/PG/恢复 | 修复分支原回归与独立安装恢复通过，不存在本轮合并提交可做“合并后”验证。 |
| 正式世界/正文/发布 | 分支工程测试覆盖；真实模型和人工文学未验收。 |
| 合并后CI | NOT_APPLICABLE：未发生合并，不能伪造合并SHA/CI。 |
| 稳定开发基线 | main继续是P1；分支是有明确容量限制的工程候选，不是全面生产通过版本。 |

## 5. 决策与恢复路径

不合并是执行用户的条件授权。当前最小安全落点是保留分阶段远程成果、完整失败证据及原依赖关系。未合并原因、所有测量定义和下一步见 [性能与容量修复对照](性能与容量修复对照.md) 与 [未通过目标与后续工作](未通过目标与后续工作.md)。

若未来原门槛全部通过：重新读取main/PR2 HEAD和审查状态→Ready→连接器merge_pull_request(expected_head_sha, merge commit)→确认merged=true及新main→合并后回归；随后才将PR3 base改为main、核对仅剩P3/P4增量并跑必要CI。任何范围缩减或接受NOT_MET都需要用户新的明确批准。

## 6. 已提交的P2资料收尾

P2资料提交为 `a9547bd651e177d48c9d0e810362e411f510d62d`，tree `62b274b6436540df8aaf7e0527f6485af836499f`，父节点971fbef。远程2690个blob完整读回一致；应用源码、测试、契约、迁移、benchmark、工作流和依赖未改变。已实际触发PR工程42863611743、四格42863611898、独立200/Context42863611810，以及push工程42863601042。其终态与包含本报告的最终资料HEAD结果在交付回复和PR状态中另行核对，不把资料提交的新调度预先记为通过。上述正文数据仍严格绑定各被测代码检查点。
