# 独立移植和回退

数据库实验依据 main 的 `434840593179e6c1faa6b7aeca3d7d747644506d` 和 #33 早期实现 `3e6b59268f1e89520230c8b70e9a26b9950a2f79`。这条实验历史包含未合并的运行时/数据库资料，合并整条分支会一并引入不在本轮采纳范围的变更。交付时 #33 已合入 `3f8d3d3c`；#34 随后合入 `bcb03637d4f01030fc95b99d1ff743b3758297ad`。两个条件采纳补丁均已在这个最新 main 上通过独立 apply --check，并在另一个隔离 checkout 同时应用运行回归。两个选取补丁共同移植到 `bcb03637` 后，真实 PG/SDK 的角色隔离、Workshop 冷/热 transport、peer death 和回执回归 9/9 通过。[最新 main 移植回归](validation/port-latest.xml)

| 补丁 | 基础对象 | 覆盖文件及用途 | 本轮验证 |
|---|---|---|---|
| [workshop-session.patch](patches/workshop-session.patch) | 接管时 main `43484059`；交付时 main `bcb03637` | Workshop reconcile 与其故障及双服务测试；增加同一安装的一批操作共享 stdio 会话的 CLI 开关 | 完整补丁通过 apply --check；新 transport 测试和双服务冷/热测试在 main 移植 checkout 上 3 项通过。角色读和数据库实验均无依赖。[原始 XML](validation/port-main.xml) |
| [compact-role-reads.patch](patches/compact-role-reads.patch) | #33 `877be80d` | 单独移植静态角色的任务/上下文读取、绑定构造和 CLI 开关及原有真实 stdio 反例 | 从冻结 #33 单独制作并运行；5 项原生 SDK/PG 集成测试通过，包括启用/关闭和错绑。该基础对象自身含 Runtime。旧 main `43484059` 缺少拆出的 bindings 模块；交付时的新 main 已含此模块。[原始 XML](validation/port-compact.xml) |
| [stdio-candidates.patch](patches/stdio-candidates.patch) | 同一 #33 | 研究复现整组 stdio 开关、反例和未采纳的探索路径 | apply --check 通过，13 项角色、原生调用、撤权与准入测试通过。这是复现实验的替代补丁，和上行有重叠。[原始 XML](validation/port-runtime.xml) |

Workshop 候选在两安装回执读取 A/B/B/A 中满足门槛。原始提交、本地实现和对照见 [Workshop](workflow/README.md)。真实委托创建的两个 A/B/B/A 块含一对尾延迟超限，不能据此承诺全批次所有类型都适用。后续集成者可先在受控的回执查询批次启用 `aw-reconcile --lookup-only --reuse-session`。默认值为关闭。运行时读取在已验证的十绑定分布可显式选择 `--compact-role-reads`，两绑定低到达率不满足双向尾延迟条件。

正式集成时重新抓取 main，检查 #33 是否已进入该 main，先在独立 checkout 执行相应命令。两个补丁分别评估，不叠加两个 stdio 补丁。在 CI 重跑当前身份/租约/工作流反例，再对目标环境和目标负载重新执行 A/B/B/A。出现异常时删除启动开关即可回退；不涉及数据库迁移或历史修订。

```sh
git apply --check /path/to/workshop-session.patch
git apply /path/to/workshop-session.patch
# 在已经具备 #33 API 的 checkout 中单独执行
git apply --check /path/to/compact-role-reads.patch
git apply /path/to/compact-role-reads.patch
```

历史原始记录中使用的本地 commit 在发布时通过 GitHub Git Data API 复制相同 tree，远端提交的作者元数据及 SHA 可不同。逐个交付片段的本地 HEAD、Git tree 和远端 HEAD 见 [source-map.json](source-map.json)。部分基准覆盖随后的纯测试或报告提交，产品代码摘要和驱动 SHA 记录在对应的 raw 文件内。
