# WorkflowHub 的 Agent API 接入面

统一契约（`personal-agent-hub/docs/AGENT_API_STANDARD.md`）的四个端点**就挂在项目自己的
FastAPI 服务上**，实现文件 `backend/workflowhub/legacy_agent_api.py`。本项目不新增 agent 进程，
也不新增端口。

## 端口：8794（serve 模式固定；桌面端随机）

| 面 | 地址 |
| --- | --- |
| 服务 | `http://127.0.0.1:8794` |
| 启动 | `python -m workflowhub serve --port 8794`，**cwd 必须是 `backend/`**（包在 `backend/workflowhub/`，未装成 wheel；已写进 `launch.json` 的 `cwd`） |
| 桌面端 | 双击 `WorkflowHub.exe`：起随机端口并把地址写 `data/desktop.json`，个人 Agent 按 `WORKFLOWHUB_BASE` → `desktop.json` → `8794` 依次发现 |
| MCP | `python -m workflowhub mcp`（项目自带的 stdio 服务，不在这里再套一层 node 桥） |

8000 归声析（vocal）的常驻后端，两边不许抢；`personal-agent-hub` 的 `validateCatalog` 现在会拒绝
两个项目登记同一个端口。

## 四端点

```
GET  /api/health          {ok:true,status:"ok",version,data:{project,tools,gates…}}
GET  /api/agent/tools     分页视图：{ok,data:[{name,catalogue_key,description,input_schema,risk}]}
GET  /api/agent/manifest  project/version/base_url/counts/tools/conventions/gates
POST /api/agent/tool      {tool,input,actor} → {ok,data:{status,run_id,trace_id,approval,outputs,artifacts,logs}}
```

## 与共享契约相关的四条设计决定

1. **工具名带 `workflowhub.` 前缀**（契约要求全局唯一）。目录里的原始 key（`cli-ffmpeg`）只在本机唯一，
   放进整个生态就不够了。`catalogue_key` 同时给出原始名；`POST` 两个写法都认，改名没打断老客户端。
2. **`tools` 是分页视图，不是全量**。本机目录实测 1530 条，全量 schema 走 `GET /api/v1/tools.json`，
   搜索走 `GET /api/v1/tools?query=`。MCP 面是 8 个元工具（`catalog_stats`/`search_tools`/`get_tool`/
   `invoke_tool` 等），故意不把 1500 条塞进 Agent 上下文——`tools.json` 实测 1.4 MB。
   所以验收器对比 MCP 与 HTTP 数量时按"非空 + 不超过目录总数"判，而不是相等。
3. **命令跑通但退出码非 0 → HTTP 200 + `ok:false`**。不能报 5xx：客户端会把"已经跑过、已有副作用"的
   命令当传输失败重发。
4. **`risk` 用 read/write/destructive/exec 四级**，与 `catalog.json` 的 `class` 同一套词。
   `diskpart`/`format`/`python` 这类是 `destructive`，不是 `exec`。`exec` 与 `destructive` 的描述符
   都报出 `confirm` 入参：契约叫 `confirm`、本项目审批门叫 `approved`，`POST` 里改名送进同一道
   `needs_approval` 判定，判定本身一点不放宽——审批模式开着又不带确认，照样 `permission_denied`。

## 自检

```bash
# 生态总验收（会自己按 launch.json 起服务、跑完关掉）
cd ~/Desktop/项目/personal-agent-hub && node scripts/verify-agent-apis.mjs --only=workflowhub
# 项目自己的测试
cd ~/Desktop/项目/workflowhub && python -m pytest tests -q
```

设了 `WORKFLOWHUB_TOKEN` 时 `/api/` 全部要带 `X-WorkflowHub-Token` 头；本机默认不设。
`data/`（含 desktop.json、audit.jsonl、browser-profile）是运行数据，不入库、不清空。
