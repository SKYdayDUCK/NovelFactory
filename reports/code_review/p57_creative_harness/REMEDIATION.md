# 修复与复核台账

首次正式合并之后，每组修复与原始审查文档分别提交到开发分支。下列 12 个提交覆盖五项 P1，包含修复本身以及测试时点、既有回执顺序和旧用例导入的必要修正。用 Git 对每个开发修复逐一执行 `git cherry-pick --no-commit`，与所选主线提交核对全部文件、整树和二进制 patch SHA-256。每个主线修复只有一个父提交。最终产品的修复差异为 19 个文件、862 行新增和 120 行删除。[逐提交 Git 重放](https://github.com/SKYdayDUCK/NovelFactory/actions/runs/43203324524/job/131132080228)

| Finding | 开发分支修复 SHA | 主线对应 SHA | 修复行为 |
| --- | --- | --- | --- |
| P57-TC-01 | [0a24898c6a6719b6e61d454ce05ef13eb4f72d4a](https://github.com/SKYdayDUCK/NovelFactory/commit/0a24898c6a6719b6e61d454ce05ef13eb4f72d4a) | [1c01dabe7f2de2308a814be14c79e02ab17cc3aa](https://github.com/SKYdayDUCK/NovelFactory/commit/1c01dabe7f2de2308a814be14c79e02ab17cc3aa) | install the checked-out Workshop in the dual-service runner |
| P57-CR-01 | [3cfb4884120ddeb9a08e8c1396766a78f17dbc8a](https://github.com/SKYdayDUCK/NovelFactory/commit/3cfb4884120ddeb9a08e8c1396766a78f17dbc8a) | [bc165661cf2960979df9f15d5789e2c441ac14b6](https://github.com/SKYdayDUCK/NovelFactory/commit/bc165661cf2960979df9f15d5789e2c441ac14b6) | require disjoint manuscripts for held-out qualification |
| P57-SEC-02 | [0fba1c9e852ac10ef4f4edb801a9b890a5718490](https://github.com/SKYdayDUCK/NovelFactory/commit/0fba1c9e852ac10ef4f4edb801a9b890a5718490) | [b4970e8622f72e1965dc5d878d0cd13bd3961f46](https://github.com/SKYdayDUCK/NovelFactory/commit/b4970e8622f72e1965dc5d878d0cd13bd3961f46) | revalidate originating Skill at planning and review decisions |
| P57-SEC-02 | [e1f74c72ea7ddd08ca349c3203dfb96a3ae89a47](https://github.com/SKYdayDUCK/NovelFactory/commit/e1f74c72ea7ddd08ca349c3203dfb96a3ae89a47) | [fb1160cbe610c50c2b4e1532c5e42ebf1f7da761](https://github.com/SKYdayDUCK/NovelFactory/commit/fb1160cbe610c50c2b4e1532c5e42ebf1f7da761) | enter the mutation gate only for the final simulation write |
| P57-SEC-02 | [9a205cdd897d66f981eaf346673908fb15c64edc](https://github.com/SKYdayDUCK/NovelFactory/commit/9a205cdd897d66f981eaf346673908fb15c64edc) | [bcc691aa671a0f402b1c005ea513e7a691f90376](https://github.com/SKYdayDUCK/NovelFactory/commit/bcc691aa671a0f402b1c005ea513e7a691f90376) | align the planning fixture cutoff with its initial frontier |
| P57-SEC-02 | [2e5c3a34d7173199387913c6a3b3da7bd2cded21](https://github.com/SKYdayDUCK/NovelFactory/commit/2e5c3a34d7173199387913c6a3b3da7bd2cded21) | [a1047b0038917c4de58ae4a4831d7a08abd8044b](https://github.com/SKYdayDUCK/NovelFactory/commit/a1047b0038917c4de58ae4a4831d7a08abd8044b) | retain request order when refreshing signed review receipts |
| P57-SEC-01 | [425b80002aff73b78f30b6a27503001775b22907](https://github.com/SKYdayDUCK/NovelFactory/commit/425b80002aff73b78f30b6a27503001775b22907) | [52dea422b5e931d5e2c16eeaeaf4170f01a470a2](https://github.com/SKYdayDUCK/NovelFactory/commit/52dea422b5e931d5e2c16eeaeaf4170f01a470a2) | enforce originating branch scope for creative gateway artifacts |
| P57-SEC-01 | [3a979cb0fd74170e256fb26703ad9e59396b428f](https://github.com/SKYdayDUCK/NovelFactory/commit/3a979cb0fd74170e256fb26703ad9e59396b428f) | [95fd21afec125276a4794351526a5f959153389a](https://github.com/SKYdayDUCK/NovelFactory/commit/95fd21afec125276a4794351526a5f959153389a) | cover historical inputs and task-scoped preparations across branches |
| P57-SEC-03 | [7e63ef5e8c60f5988a05e06a75fa2176c450d0ad](https://github.com/SKYdayDUCK/NovelFactory/commit/7e63ef5e8c60f5988a05e06a75fa2176c450d0ad) | [2b26f54b4ec9a196c33edd23e6ff12f853821d6a](https://github.com/SKYdayDUCK/NovelFactory/commit/2b26f54b4ec9a196c33edd23e6ff12f853821d6a) | verify the actual editor's independent subject group at final acceptance |
| P57-SEC-03 | [a6f08bf65b4c66a913964d7e88b250500f05c985](https://github.com/SKYdayDUCK/NovelFactory/commit/a6f08bf65b4c66a913964d7e88b250500f05c985) | [92fbf9bcdfaa1dde80516c211aad04605b1af801](https://github.com/SKYdayDUCK/NovelFactory/commit/92fbf9bcdfaa1dde80516c211aad04605b1af801) | give the alternate editor a valid review horizon |
| P57-SEC-03 | [aadaf43050f068a15bd9d753ccba1a48b9e8f225](https://github.com/SKYdayDUCK/NovelFactory/commit/aadaf43050f068a15bd9d753ccba1a48b9e8f225) | [8e880af06508acb19e5c6074da3e8de8cd24d007](https://github.com/SKYdayDUCK/NovelFactory/commit/8e880af06508acb19e5c6074da3e8de8cd24d007) | verify one editor subject across all manuscripts in a final decision |
| P57-SEC-03 | [644b69bc0e397abcb894a395707a76d6d10dc861](https://github.com/SKYdayDUCK/NovelFactory/commit/644b69bc0e397abcb894a395707a76d6d10dc861) | [d6ff88223d6ae655a45d713687e5e8119e157737](https://github.com/SKYdayDUCK/NovelFactory/commit/d6ff88223d6ae655a45d713687e5e8119e157737) | retain the span helper for the existing editorial coverage case |

## 纠正后的行为

- **原始创作材料**　新 S1 输入在准备事务登记来源分支，到任务创建和真实分派时再次核对。新制品关联已冻结快照。旧阶段从既有审计事件及任务代际还原分支，缺乏可信绑定的旧 S1 输入只对全作品域管理员可见。原候选和旧记录的字节保持可核验。
- **来源资格**　独立性凭据与提供该结果的员工 Skill 分别验证。最终事务检查冻结源与当前 NF 能力，Workshop 见证在写事务外取得，过期见证不能用于最终动作。双系统保留短时有效见证，未引入跨库全局线性事务。模拟持锁顺序与已存在的备份门配合。[SEC-02 普通验收全 5 作业](https://github.com/SKYdayDUCK/NovelFactory/actions/runs/43202624163)
- **最终编辑**　只有正式 RETAIN/ACCEPT_REVISION 要求当前实际编辑注册。原生或 W4 读者独立性仍复核，线下签名把实际编辑 grant、epoch、每份稿件及实质主体组绑定，覆盖 Workshop 登记的贡献者。每份稿件保存对应的编辑和阅读证据摘要。正常退回、延后、撤回走原流程。
- **工程留出资格**　两个报告若共享冻结正文，不能充当独立样本。inspect 与 require_release 对已归档的旧测量重建来源哈希，一次升级前的真实合成运行证实，相同旧报告能同时保留原记录且被当前资格规则拒绝。[历史兼容回执](https://github.com/SKYdayDUCK/NovelFactory/actions/runs/43203462448/job/131132551642)
- **Workshop 验收**　在原来运行全套集成的解释器中安装检出包并设置当前 PATH，保留两个数据库与独立子进程。原来失败的 14 项完整合集复验通过。之后在含新增审核用例的正式主线复验，17 项全部通过，两个 W6 场景作业均绿。两个 PG 系统标识不同，双 SSH 收据显示双方检出 SHA 均为当前 main、跨服务读取拒绝 2 次、模型轮次为 0。[正式主线 Workshop](https://github.com/SKYdayDUCK/NovelFactory/actions/runs/43204723135/job/131136868005) [原集合重跑](https://github.com/SKYdayDUCK/NovelFactory/actions/runs/43201789218/job/131126778921)

## 延期 P2

| 问题 | 影响范围和延期原因 | 验收后续修复所需证据 |
| --- | --- | --- |
| P57-CR-02 | 需要约定读者与编辑端口的语义并兼容历史报告。当前部署保留逐稿人工阅读。 | 给人工或受托读者增加明示已审清/未给判断的区别，补两种业务结果的成对用例。 |
| DB-01 | 需单独组织双集群灾备资格演练，不能以已通过的单时间线试验承诺现场灾备。 | 按两独立集群生成两份基备及归档，故意错开命名时间点并保留两个 system_identifier、LSN 和对账结果，更新正式验收用语。 |
| PERF-01 | 分页需要兼容返回契约和历史完整性验证。影响已授权的管理查询，未发现对核心世界或费用账本的破坏。 | 设计带冻结 epoch 的分页游标及批量检索，保留逐项完整性语义，另做长历史基准测试。 |
| PERF-02 | 属于观测分类，金额与预算的权威来源独立。投产计量前须修正并回填相容范围，本次只选取严重修复进入主线。 | 以授权工作、分支、任务代际与 attempt 连接首阶段不可变回执，金额未知保持 null，已知 Token 单独记录，核对 Skill、调用数量及延迟分组。 |

原始七份报告记载的是问题首次发现时的状态。最终门槛、主线测试和主线对审查目录的隔离以 [正式主线回执](MAIN_MERGE_RECEIPT.md) 为准。
