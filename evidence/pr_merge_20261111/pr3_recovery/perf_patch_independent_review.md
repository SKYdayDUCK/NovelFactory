# 性能补丁独立审核（只读）

审核范围：基于 e0b0f2b080f3225d8be8e202e13258bc480f5b84 的 `domain/world.py`、`application/kernel.py` 修改与两项新增测试文件。作者为 backend_perf；本审核未改作者源码/测试，未重跑 PostgreSQL 或负载。

## 结论

**未发现阻断缺陷；允许作为第一阶段远程 checkpoint 候选。此结论仅是该补丁的独立正确性审核，不代替完整回归、真实 GitHub CI、业务完成或容量验收，也不构成 PR 合并许可。**

## 逐项判断

1. **严格输入和类型：保持。** `replay()` 仍先调用 `plain()` 与原 `canonical_bytes()`，因此浮点、自定义标量、非法 Unicode、非字符串对象键等仍会拒绝。Memo 的 JSON 序列化不会把非法输入净化为合法数据后再验证。原 genesis 验证只在已有相同完整字节、相同解释器身份的有效命中上省略；事件 schema、canonical、重复 effect/occurrence/group、时序检查及完整 apply/约束检查仍逐次执行。
2. **顺序：保持。** Memo 身份及缓存输出均使用未排序 JSON，包含原嵌套字典插入顺序；不会将 entity-map 顺序不同的 genesis 混用而改变 `lifecycle_history`。列表次序也不变。SQL `IN` 结果显式构造成 id 映射，再依原 `selection.values()` 顺序取出，未依赖 PostgreSQL 行顺序。
3. **修改隔离和线程：保持。** Cache 仅持有 immutable bytes；首次 miss 返回的可变 snapshot 已在写入 bytes 后与 cache 隔离，hit 返回新解码的对象。输入由原 `plain()` 复制。LRU 访问和写入受 lock 保护；并发 miss 可以重复计算，但不会共享可变快照或应用多次正式效果。
4. **内存边界：成立但口径须准确。** 8 entries / 16 MiB 限制输入 bytes 加输出 bytes 的缓存载荷；不是进程 RSS 上限。超过单条预算不缓存。每工作者各持一份缓存，不能把 16 MiB 当成所有工作者共同额度。键元数据及 Python 容器开销另计，但 entry 数量有界。
5. **解释器及升级：当前范围安全。** Key 包含 interpreter/projection 版本、predicate 定义、terminal 集合和完整 genesis。现有 PR3 RuntimeUpgradeService 只允许 `IDENTICAL_SEMANTICS_METADATA_ONLY`，拒绝 reducer/calendar/predicate/world-rule 语义变化；这类升级不使初始纯计算失效。部署新程序重新创建 process-local cache。未来若引入同一进程动态加载新的 schema/helper/reducer 语义，需一并扩展版本键/清缓存并提供迁移证明；本轮没有该机制，也没有悄然增加其支持。
6. **权限、证书、任务及历史：保持。** 本 cache 不接触 grant、角色、WorldRef、HEAD、授权结果或接受证书；权限仍在应用服务验证。缓存命中不输出 revision/accept receipt。kernel diff 未修改授权、task generation、history epoch、scope token、interpretation、approval、幂等或 CAS 条件。
7. **事务回滚后的缓存：无未提交世界状态泄漏。** Memo 只保存由显式 genesis 得出的初始 snapshot，不保存 event 后状态/事务 id/receipt/数据库行引用。合法 genesis 在后续事件校验或事务回滚后仍留存，仅允许将来同字节的初始计算复用；事件重放和授权仍必须重新通过。批量 revision mapping 仍在既有事务内，`add_many()` 无独立 commit、无忽略冲突。
8. **唯一效果及批量写：保持。** 原 replay 仍检测重复 effect/occurrence/group；输出 events 不因字典中转而去重。既有 composite FK、revision+occurrence 主键与 revision+effect_group 唯一约束不变。`add_many` 按 256 行分批，所有 batch 共用 caller transaction；没有提高并行正式接受范围。
9. **prepare 同行复用：保持。** 只在 `parent == wr` 且同一 REPEATABLE READ 事务内复用已由 `revision()` 验证的不可变行。`build()` 只读 snapshot；`state_from_revision()` deepcopies schedule state，schedule validation 不修改原 revision。不同 base 继续读取原行；相同 base 没有跨事务/HEAD cache。

## 实际验证及测试覆盖

本审核者独立运行：`taskset -c 8 .venv/bin/python -m pytest tests/unit/test_genesis_memo.py -q`，结果 **10 passed in 0.11s**，exit 0。覆盖复用、完整 event 检查、输入/返回修改隔离、键序、5 类非法 genesis、LRU bytes/entry 限制、并发返回独立性、解释器版本隔离。

静态审读新增 PostgreSQL 用例：一项通过 before_commit 注入失败，检查 HEAD/revision/mappings/events 全部回滚，重试后单个正式效果及幂等 receipt、旧 snapshot 不变；另一项确认相同 base 的 prepare 减少重复 revision 读取并可正常接受。作者报告的 PG 结果和 5 对真实存储 genesis 测量，应以其独立证据文件为准，本审核者没有重跑或把它写成本人实测。

新增用例不是整个权限/竞争矩阵的替代。四事件 mapping 用例不跨越 256 行 batch 边界，且没有刻意打乱 `IN` 结果；这两点属于非阻断覆盖边界：batch 原语既有事务性和重建序列的实现可直接核查，不应因此削减原回归 gate。

## 本次审核绑定的源文件 SHA-256

| 文件 | SHA-256 |
| --- | --- |
| `src/novel_factory/domain/world.py` | `666de00b7f3a9090f9093ce939f44b489b0abc5deb34beab2a1ddfbdfd096471` |
| `src/novel_factory/application/kernel.py` | `5edd50ce4785808db3b45d57dba665d0f1922b410a7209bddb31abd3de81c584` |
| `tests/unit/test_genesis_memo.py` | `18fc207a7eac09e81a6e42285c8708dca35af16ba218b8bd80cb3038464a1b22` |
| `tests/integration/test_batched_selected_history.py` | `79e7706a1474367b33c1b4bc168a7ba3eb3d87d2002b9c36a48ee4bbd122a209` |
