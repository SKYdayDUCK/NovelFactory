# W3 独立身份与 Linux 隔离复验

检出 `912d20b5f42cb39b0955e5015ac13cec182c75fe`，树 `b49d1c054a78ae06bcb92354c094d6171707b9ea`。在可创建 Linux 用户命名空间的运行环境完整运行 48 项，全部通过，零跳过。需隔离宿主能力的 17 项包含 13 项角色选择工具、2 项角色容器及 2 项受控角色读取。其余包括 16 项 NF 委托和 15 项 Workshop 组织、任期与安装授权检查。

[原始 JUnit、环境与实际 PostgreSQL 记录](original-junit-and-environment.zip) · [逐目录 Git 对象、精确测试及 SHA-256](result.json)

此前正常卷纲导演交接的控制试验及权限修订来自独立的生成功能测试。修前只有测试覆盖层，产品源码为 `607a1f74`；修后采用 `64625243` 的授权修订，独立检出为 `c9579e79`，共 7 项通过。[实际源码、检出树及校验](handoff-source.json) · [正向交接前后原始 JUnit、测试差异与 WAL 回执](handoff-positive-control.zip)
