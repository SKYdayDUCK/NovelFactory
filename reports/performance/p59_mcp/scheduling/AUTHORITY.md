# 独立核对授权、SDK 缓存与取消

源码基线为候选提交 `2d3fad6` 和锁定的 Python SDK 1.29.0。SDK 低层 `Server.list_tools()` 对返回列表会清除该 Server 的旧工具定义再刷新。缓存命中时，`call_tool()` 仍会进入应用 handler；缓存未命中时，会先回调注册的 list handler，即便 `validate_input=False`。因此冷启动、空目录或未获授权名称可能触发额外的发现 SQL。批量发现的收益不能直接计入所有已预热调用。[SDK v1.29.0](https://github.com/modelcontextprotocol/python-sdk/blob/v1.29.0/src/mcp/server/lowlevel/server.py)

`invoke_bound_tool` 保持可信 ceiling、当前可见性、参数校验和审计。静态任务在一个 UoW 内核验作品、当前授权、任务代际、有效租约、知识切面和角色工具，读取对应任务或上下文。对作品/授权的共享行锁在只读 UoW 提交时释放，撤权在同一条授权行更新锁上串行化。已完成授权核验的在途调用和撤权事务需要按其线性化顺序解释。撤权提交后开始的调用不可用旧缓存继续读。任务行原读路径使用普通 SELECT，不能由此声称通过网络送达的瞬间任务仍未取消。[绑定工具](../../../../src/novel_factory/application/bound_tools.py) · [应用服务](../../../../src/novel_factory/application/mcp_runtime.py)

发现分组限定在一个可信 runtime 内，每组最多四个等待者，在开始授权检查前关闭分组。静态一次检查按项复制工具名；动态任务与控制器继续逐项判定。队满、背压、授权拒绝及内部错误均可能表现为空目录。只有精确匹配角色预期工具集合的结果可计为成功。已撤销绑定的错误调用不返回目录、正文或缓存值。取消单一等待者保留其他成员的 future；已派发的同步工作仍保留 AnyIO 槽到真实执行结束。开发基线的取消测试覆盖 SDK 的 AnyIO scope，不能据此给所有外部裸 `asyncio.Task.cancel()` 的强行卸载作保证。[MCP 入口](../../../../src/novel_factory/interfaces/mcp_stdio.py) · [取消和在途发现反例](../../../../tests/unit/test_bound_tool_admission.py)

新增四项反例覆盖实际 PG 撤权锁先于分组读取、模拟外部 Workshop witness 等待期间撤权并在真实本地事务中阻止交付、同一动态组的各项独立拒绝、真实 SDK stdio 客户端已热缓存时直接调用和重新发现。动态 witness 时序是故障注入，不是对真实 Workshop 远程服务发起的负载测试。[新增测试](../../../../tests/integration/test_p59_mcp_authority.py) · [本轮结果](authority.xml)

独立故障实验在 PostgreSQL 确认授权查询正在等锁后，发送真实 SDK 取消消息。两次重叠发现取消一项，另一项成功；使用测试屏障确认四个请求进入同一个生产分组后，取消其中一项，剩余三项均取得完整目录，下一个角色读取也正确。撤权后四次发现均为空、热工具直接调用拒绝，实验末 idle-in-transaction 为 0。早先一次未强制同组的四项并发，有未取消项未通过完整目录断言；目前无法区分普通有界准入拒绝与取消时序对该失败的贡献。该失败应保留在容量报告中。已测范围内未发现撤权后的目录或正文泄漏。[取消和重连结果](../stdio/reconnect-group.json)
