# W4 冻结候选的独立验证

`3ed83ffe779bf15e7965f2b2fc8236abaf90ff72`，树 `cb601579cceafd8cd33d35664a31f8d9dd38c879`。双 PostgreSQL 17.11、Redis8、真实 stdio/物理恢复及新增审核接口共 67 项通过、零跳过。五份 JUnit 与每份逐值 JSON 均在原始 ZIP 中。真实 NF 类型化审核、源阅读与合成账单完全分开核验（一次 HTTP、18 tokens、0.020000、零商业调用）。另有原测试跳过集对应的本地 17 项、其他独立验收 27 项。

当前正式 GitHub 工作流为 [Workshop](https://github.com/SKYdayDUCK/NovelFactory/actions/runs/43096271063) 和 [P1](https://github.com/SKYdayDUCK/NovelFactory/actions/runs/43096270977)，阶段关闭须以两者终态和实际合并后 main 的独立验收为准。
