# ADR-001：Skill Contract 优先

状态：已采纳

决策：每个 Skill 在 `skill.yaml` 声明身份、版本、输入输出字段、执行器、权限、依赖、超时和重试策略；Registry 用严格的 Pydantic 模型校验后才发布。业务 Skill 的 Python 文件不进入 Agent 或 Runner 的条件分支。

原因：新增能力只需添加目录并刷新发现，调用方依赖稳定契约。代价是 Skill 实现必须遵守 JSON 输入输出协议；动态非结构化结果不能直接进入工作流。
