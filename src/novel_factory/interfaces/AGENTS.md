# Interfaces 局部规范

本文件继承 [根规范](../../../AGENTS.md)，CLI、HTTP、MCP 是同一应用服务的适配器。

- 只处理传输、认证上下文、契约转换和错误映射，不直写领域表或复制业务规则。
- JSON-RPC request id 不等于任务 ID、业务幂等键或效果 ID。
- MCP profile 按真实 Host/SDK 验证固定；不能只改版本字符串宣称协议升级。
- stdio 的 stdout 只写协议消息，日志走 stderr 或受控日志系统。
- 工具范围按服务端能力配置；不能把 SQL、任意文件读写、shell 或管理接受接口普遍暴露给角色。
- 输入中的 actor、角色名和 scope 不能提升权限；返回错误、计数和引用同样防止信息越权。
- 长操作返回应用任务和收据；传输取消与业务取消不同，不撤销已接受的事件。
- 不向未授权角色返回隐藏实体是否存在，避免利用错误差异泄漏信息。
- HTTP 接入按已选协议验证身份、受众、scope、Origin 和大小限制。
- 同一个合法用例经不同适配器调用应保持相同领域语义；测试协议形状与业务结果两个层面。

- 同一连接上的任务各自认证授权；timeline/query_batch还需逐操作、plane、actor和时间校验，不能用统一工具名绕过graph/plan限制。

参考：[08 接口](../../../docs/08_MCP与工具接口.md)、[官方 profile 说明](../../../research/official_platform_notes.md)。
