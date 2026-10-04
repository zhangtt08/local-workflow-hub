"""Agent-facing documentation served over HTTP and printed by the CLI.

Kept in code so the UI page, the ``guide`` endpoint and ``python -m workflowhub --help`` cannot
drift from the routes that actually exist.
"""

GUIDE = """# WorkflowHub · Agent 接入指南

WorkflowHub 把这台电脑上的**可执行文件、系统命令、脚本工具、项目 npm/make 入口、已安装应用**
统一登记为带 JSON 契约的工具，Agent 可以用三种方式调用同一批能力。

## 0. 心智模型
- **工具（tool）**：本机扫描得到，一个 key（如 `cli-ffmpeg`）对应一条命令。参数是开放的：`argv` 原样传递。
- **Skill**：`skills/` 里手写的契约（固定输入输出），key 即 skill id。
- **Workflow**：把工具/Skill 连成 DAG，一次编排、多次复用；执行会固定定义与契约快照。
- **Run**：任何一次调用都会留下 run 记录（trace_id + 结构化事件），可回看、可审计。
- 单次调用一个工具 = 一个只含一个节点的临时 Workflow，因此与 DAG 走同一条校验/执行链路。

## 1. REST（最直接）
```
GET  /health                                  存活与工具数量
GET  /api/v1/agent/info                       端点清单、鉴权、风险开关
GET  /api/v1/tools?query=ffmpeg&limit=20      搜索目录（含分类/风险/是否启用）
GET  /api/v1/tools/{key}                      完整契约 + 参数说明 + 调用片段 + 最近审计
POST /api/v1/tools/{key}/invoke               执行：{"inputs":{...},"actor":"my-agent","wait":true}
GET  /api/v1/runs?limit=20 / /api/v1/runs/{id} 运行历史与结果
POST /api/v1/workflows                        保存 DAG（先校验，不可覆盖已发布版本）
POST /api/v1/workflows/{id}/run               {"version":1,"inputs":{...}}
GET  /api/v1/files?path=...                   读取 data/ 下的输出文件（越界会拒绝）
```

### invoke 的输入字段
| 字段 | 类型 | 说明 |
| --- | --- | --- |
| `argv` | string[] | 按顺序追加到命令后的参数，不经过 shell，因此不会发生注入 |
| `flags` | object | 具名参数；脚本类工具会按源码识别到的 `--x` / `-x` 风格自动拼装 |
| `stdin` | string | 写入进程标准输入 |
| `cwd` | string | 覆盖工作目录 |
| `env` | object | 追加环境变量（不会改动系统变量） |
| `timeout_seconds` | number | 本次调用超时，超时强制终止进程 |
| `dry_run` | boolean | **只返回将要执行的命令行，不运行**（第一次调用某个工具时先这样看一眼） |
契约里额外声明的具名字段（脚本源码里 `add_argument`/`param()` 识别到的）可以直接放在 `inputs` 顶层。

### 返回
```json
{"status":"succeeded","run_id":"…","trace_id":"…",
 "outputs":{"exit_code":0,"stdout":"…","stderr":"…","command_line":"…"},
 "artifacts":[{"uri":"…","media_type":"text/plain"}],"logs":["…"],"error":null}
```
失败时 `status` 为 `failed`，`error` 内含 `code/message/retryable/details`。非 2xx 统一是
`{"error":{"code":…,"message":…,"details":…}}`，`code` 取值见 `models.ErrorCode`。

## 1b. 旧契约（与其余 10 个项目同形，供现成 Agent 配置直接接）
```
GET  /api/health          {"ok":true,"status":"ok","data":{project,version,tools,enabled,gates,…}}
GET  /api/agent/tools     工具描述符 {name,description,input_schema,risk}，risk 用 read/write/destructive
GET  /api/agent/manifest  project/version/base_url/tools + 原生面清单
POST /api/agent/tool      {"tool":"<key 或 function 名>","input":{…},"actor":"…"} → {ok,tool,ms,data}
```
差异是有意的，别当 bug：① `tools` 是**分页视图**（默认 200、上限 1000），全量走 `/api/v1/tools.json`；
② 命令跑通但退出码非 0 返回 **HTTP 200 + `ok:false`**（避免客户端把它当传输错误而重发已有副作用的命令）；
③ 风险闸门照旧 —— 未启用 403、审批模式 409，错误信封里带 `workflowhub_code` 说明真实原因。

## 2. MCP（Agent 客户端原生支持时最省事）
把 `/api/v1/agent/mcp-config` 返回的 JSON 放进 Agent 的 MCP 配置即可（STDIO 传输）。暴露的工具：
`catalog_stats`、`search_tools`、`get_tool`、`invoke_tool`、`list_workflows`、`run_workflow`、
`get_run`、`recent_activity`。目录里有上千个工具，所以清单里只给这 8 个元工具：**先 search，再 invoke**，
避免把 1500 条 schema 塞进上下文。

## 3. Function calling（OpenAI / DeepSeek / Anthropic）
`GET /api/v1/tools.json` 直接返回 `tools` 数组；把 `function.name` 映射回 key（下划线换回连字符）
后调用 invoke。单个工具的片段见 `GET /api/v1/agent/snippets/{key}?example={"argv":["-v"]}`。

## 4. 安全与风险（不要跳过）
- 命令一律 `exec` 语义、无 shell；参数含 NUL 会被拒绝，条数与长度有上限。
- 每个工具有 `risk`：`low/medium/high`。`high` 表示它能改系统、联网或执行任意代码（如 powershell、python、curl）。
- 设置里的 `high_risk_enabled=false` 会让所有高风险工具对 Agent 直接返回 `permission_error`。
- 设置里的 `approval_mode=true` 时，高风险工具调用返回 `awaiting_approval`，需要在 WorkflowHub 窗口里批准，
  Agent 再带 `inputs.approved=true` 重试。
- 每次调用都写入 `data/catalog/audit.jsonl`：谁（actor）、调了什么、命令行、退出码、耗时。
- WorkflowHub 只能保护调用链，不能判断命令本身的后果。请只对可信 Agent 开放端口，跨机器时设置
  `WORKFLOWHUB_TOKEN`（请求头 `X-WorkflowHub-Token`）。

## 5. 常见问题
- **找不到工具**：先 `POST /api/v1/discovery/scan`（可传 `roots`），或看设置里的扫描目录是否覆盖到它。
- **中文名/空格路径**：`argv` 用数组传，不需要自己加引号。
- **中文乱码**：执行环境强制 `PYTHONUTF8=1`、`PYTHONIOENCODING=utf-8`；控制台仍显示乱码是 GBK 终端的问题，落盘文件是 UTF-8。
- **要看它到底会执行什么**：`dry_run=true`，或在工具详情页点「预览命令」。
"""

QUICKSTART = """WorkflowHub · 三步上手
1. 扫描本机：工具库 → 「重新扫描」。它读取 PATH、注册表已安装应用、以及设置里配置的扫描目录中的脚本与项目入口。
2. 确认可信范围：给常用工具点星标；高风险工具（powershell / python / curl 等）默认允许调用，
   若要每次先由你批准，在设置里打开「审批模式」。
3. 接给 Agent：Agent 接入页复制 MCP 配置或 curl/Python 片段；先用 dry_run 预览命令行，再正式调用。
每个工具的详情页都写明「它是什么、能做什么、参数怎么传、风险、最近调用」，不用离开界面就能学会。
"""
