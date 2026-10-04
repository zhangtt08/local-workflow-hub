# ADR-005：发布版本不可覆盖

状态：已采纳

决策：Workflow 以 `id + version` 发布，文件仓储拒绝覆盖已存在版本的不同内容。Run 引用固定 `workflow_version_id`；NodeRun 引用固定 `skill_id + skill_version`。版本号不是“latest”别名，执行必须显式选择版本。

原因：保存后的 Workflow 可跳过 LLM 规划重复执行，且历史 Run 不被编辑影响。后续 Skill 发布仓库应给实现包加内容哈希并禁止复用版本号，以支持代码级重放。
