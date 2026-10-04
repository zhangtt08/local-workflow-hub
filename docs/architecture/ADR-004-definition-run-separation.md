# ADR-004：定义与运行记录分离

状态：已采纳

决策：`WorkflowDefinition` 是可发布定义，`WorkflowRun` 是一次执行，包含固定定义快照；`NodeRun` 包含固定 Skill Contract 快照和统一 `SkillResult`。运行中状态变化只修改 Run，不修改定义。

原因：历史执行必须可解释、可追踪；定义后续变化不应重写过去。代价是运行记录会重复保存 Contract 数据。
