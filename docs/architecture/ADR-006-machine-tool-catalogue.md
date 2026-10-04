# ADR-006：把本机发现的工具纳入 Skill 契约

- 状态：已接受
- 日期：2026-10-03
- 相关：`backend/workflowhub/discovery.py`、`catalog.py`、`command_executor.py`、`services.py`

## 背景

产品目标是"识别电脑所有的 tool 和 workflow（含脚本工具），供 Agent 直接调用"。原骨架只有
`skills/<id>/skill.yaml` 这一种能力来源，且只支持 `executor.type = "python"` 的 JSON stdin/stdout 协议。
需要决定：发现的 1500+ 个本机命令以什么模型进入系统。

## 决策

1. **发现的工具变成普通 `SkillDefinition`**，`executor.type = "command"`，`entry` 为绝对路径、
   `args` 为固定前缀参数。`CompositeRegistry` 把手写 Skill 包与目录里"已启用"的工具合成一个注册表。
2. **通用调用字段固定为一小组**：`argv`、`flags`、`stdin`、`cwd`、`env`、`timeout_seconds`、`dry_run`；
   从脚本源码推断出的具名参数（`add_argument` / `param()` / `process.argv`）作为额外字段并列声明。
3. **必填只声明不强制**：脚本里 `required=True` 的参数在描述中写明"脚本要求必填"，但
   `ValueSpec.required=False`，避免 `--help` 这类合法调用被校验器拦下。
4. **没有命令入口的条目是蓝图**（CI 工作流、只有 manifest 的 Skill 包、找不到 exe 的应用）：
   不参与执行、不出现在 Agent schema 里，但提供"接入为本地 Skill"的显式动作。
5. **单次工具调用 = 单节点临时 Workflow**（`services.invoke_tool`），因此快照、trace、审计、
   重试与 DAG 复用同一条链路；审计另存 `data/catalog/audit.jsonl`（actor、命令行、退出码、耗时）。
6. **风险是三档标签 + 两个开关**（`high_risk_enabled`、`approval_mode`），默认全开并留痕；
   高风险不是"禁止"，而是"看得见、可关、可逐个审批"。

## 备选方案

- 自建 Tool 模型 + 独立执行路径：改动最小但立刻出现两套契约、两套校验、两套记录，Agent 也要学两套接口。
- 直接把每个 exe 变成一个手写 `skill.yaml`：1500 个 YAML 无法维护，且扫描结果会随机器变化。
- 用 shell 拼命令行：Agent 传参即注入风险，且中文路径/空格引号规则不可控。选择 `exec` 语义 + 数组参数。

## 后果

- 正面：Agent 三种入口（REST / MCP / CLI）共用 `services`，行为一致；DAG 可以把
  `python -c` 的输出接进手写 Skill；新增能力只需新 executor 类型。
- 代价：参数识别是启发式的，覆盖不了所有 CLI 风格（靠 `argv` 兜底）；目录体积大（本机 1523 条、
  `catalog.json` 约 2 MB），因此列表接口强制分页并带 facets；`exec` 语义下 `.cmd`/`.bat` 需要显式用
  `cmd.exe /c` 包装（扫描器已这样登记）。
- 风险：一旦 `high_risk_enabled=true`（默认），Agent 可以调用解释器执行任意代码。缓解是审批模式、
  全量审计、只监听 127.0.0.1、可选 `WORKFLOWHUB_TOKEN`，并在界面与指南里反复写明这不是沙箱。
