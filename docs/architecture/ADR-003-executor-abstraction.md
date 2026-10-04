# ADR-003：执行器通过协议和路由解耦

状态：已采纳

决策：Runner 只查 `executor.type` 路由并调用 `SkillExecutor.execute`。第一种适配器是有超时的本地 Python 子进程；未来 Shell、HTTP、MCP 等实现同一协议，在组合根注册。

原因：执行方式扩展不应修改 Workflow Engine。当前权限声明仅用于准入检查，本地进程不提供强隔离；不可信代码需要另建沙箱执行器。
