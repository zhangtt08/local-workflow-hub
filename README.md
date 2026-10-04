# WorkflowHub · 本机工具与 Workflow 编排中心

把这台电脑上**所有能被调用的东西**（PATH 上的可执行程序、Windows 内置命令、脚本工具、项目里的
npm/make 入口、已安装应用、别人的 Skill 包）统一登记成带参数说明的工具，然后通过 **REST / MCP /
function calling / CLI / 旧契约四端点** 五种方式交给 AI Agent 直接调用（五种入口共用同一服务层，
行为不会各说各话）；人也可以在桌面界面里搜索、试运行、串成 DAG。

界面是**方案三 Terminal Light** 设计语言：纸面绿底、全等宽字体、ASCII 目录树、`$ 命令` 式区块、
字符 DAG 图与日志流；整个软件只有一个终端式窗口，没有系统标题栏 —— ─ / □ / ✕ 画在窗口栏右侧，
整条可拖动、双击最大化、拖到屏幕边缘自动贴靠，任务栏图标与界面同源。右上角「◐ 夜间」可切到同语言的暗色版本。

## 交付形态

| 形态 | 位置 | 说明 |
| --- | --- | --- |
| 桌面 exe | **`WorkflowHub.exe`（就在这个文件夹根目录，39 MB 单文件）** | 双击即用；首次运行自动扫描本机并生成 `data/`。要给别人只拷这一个文件即可 |
| 源码运行 | `python -m workflowhub desktop` | 需要 Python 3.12 + `pip install -e .` |
| 仅服务 | `python -m workflowhub serve --port 8000` | 只跑 HTTP API，浏览器打开 `http://127.0.0.1:8000` |
| MCP 服务 | `python -m workflowhub mcp` 或 `WorkflowHub.exe --mcp` | STDIO，给支持 MCP 的 Agent 直接挂 |

exe 用系统里已有的 Edge/Chromium 打开窗口（不内置浏览器，所以是单个 39.5 MB 的 exe）。
拷给别人时只要拷这个 exe；没有 Chromium 内核浏览器时会直接打印界面地址，功能不受影响。
找不到它就在项目文件夹里搜 `WorkflowHub.exe`，或在 `scripts/build_exe.py` 重新生成。

## Agent 接入（最短路径）

```bash
curl -s http://127.0.0.1:8000/api/v1/agent/info          # 端点、鉴权、目录规模
curl -s "http://127.0.0.1:8000/api/v1/tools?query=ffmpeg&limit=5"
curl -s -X POST http://127.0.0.1:8000/api/v1/tools/cli-ffmpeg/invoke \
     -H "Content-Type: application/json" \
     -d '{"inputs":{"argv":["-version"],"dry_run":true}}'   # 先看会执行什么，再真跑
curl -s http://127.0.0.1:8000/api/v1/tools.json > tools.json  # 直接喂给 function calling
```

其余项目早已习惯另一套四端点契约，这里也照原样提供（同一批工具、同一套闸门）：

```bash
curl -s http://127.0.0.1:8000/api/health           # {ok, status, data:{project, tools, gates…}}
curl -s "http://127.0.0.1:8000/api/agent/tools?limit=50&offset=0"
curl -s http://127.0.0.1:8000/api/agent/manifest
curl -s -X POST http://127.0.0.1:8000/api/agent/tool \
     -H "Content-Type: application/json" \
     -d '{"tool":"cli_ffmpeg","input":{"argv":["-version"],"dry_run":true}}'
```

这套信封有两处**有意的差别**：`tools` 是分页视图（默认 200 条、上限 1000，全量走
`/api/v1/tools.json`）；命令跑通但退出码非 0 时回 `200 + ok:false`，不让客户端把它当成
传输错误去重发一条已经有副作用的命令。风险闸门不放宽：工具未启用 403，审批模式 409。

完整说明在应用内 **Agent 接入** 页，也可以 `GET /api/v1/agent/guide` 拿到同一份文本。要点：

- `inputs.argv` 原样按序追加，命令以 **exec 语义**启动、不经过 shell，所以参数不会被拼成注入语句。
- `flags` 与脚本源码里识别到的具名参数（`add_argument` / `param()`）自动映射成对应风格。
- `dry_run`、`timeout_seconds`、`cwd`、`env`、`stdin` 是通用调用选项；每次调用都会写审计。
- 单次调用 = 一个只含一个节点的临时 Workflow，和手搓 DAG 走同一条校验/执行/trace 链路。
- MCP 默认只暴露 8 个元工具（search → get → invoke），避免上千条 schema 挤爆上下文；
  `--expose-tools` 可改成每个工具一个 MCP 工具。

## 界面地图（每个地方是干什么的）

每个页面都有直达链接（`#/tools`、`#/workflows`…），浏览器与桌面窗口通用。

| 页面 | 作用 | 关键交互 |
| --- | --- | --- |
| 总览 | 本机能力概况 + 上手清单，数字全部来自实时目录 | 重新扫描、跳到下一步 |
| 工具库 | 搜索/筛选全部条目，点开看契约并试运行 | 星标、启停、备注别名、探测版本、蓝图接入为 Skill |
| 工作流 | DAG 的 JSON 编辑、强制校验、发布、执行 | 结构预览、执行输入、节点状态图 |
| 运行记录 | 每次调用的节点/输入/输出/日志/产物 | 按状态筛选、看产物文件、重跑单工具调用 |
| Agent 接入 | 五种接入方式的现成配置 + 端点自测 | 测试连接（真的逐个端点访问并给出耗时，含旧契约三端点） |
| 设置 | 扫描目录、风险开关、超时与输出上限、数据目录 | 打开数据目录、复制自检命令 |

快捷键：`Ctrl+K` 搜索并跳转，`Ctrl+R` 重新扫描，`1…6` 切换页面，`/` 聚焦搜索框，`Esc` 关闭弹层。
标题栏右侧有帮助按钮，里面是三步上手说明。

## 发现范围与安全边界

扫描是**只读**的：遍历 PATH 目录、注册表卸载项、以及设置里配置的目录（默认含本项目、
`Desktop\项目`、`Documents`，深度 4 层，跳过 `node_modules`/`.git`/`venv` 等）。
本机一次实测发现 1,529 条（可执行 895 / 脚本 419 / 项目入口 133 / 应用 79 / Skill 包 3；
风险 low 896 · medium 133 · high 500，其中启用 1,472），耗时 1.5～3 秒。这个数会随电脑当前装了
什么、项目文件夹里有什么而浮动，界面与 `/api/v1/overview` 给的永远是当下的真实值。
首次启动（含 exe）会自动扫描一次，不会给你一个空目录。

- 每条工具带 `risk`：`low` / `medium` / `high`（能执行任意代码或改系统的算 high，如 python、powershell、cmd）。
- 设置里可整体关闭高风险工具（对 Agent 直接返回 `permission_error`），或打开**审批模式**：
  高风险调用先返回 `awaiting_approval`，需要你在窗口标题栏的"待批准"里放行。
- 输出按字节上限截断并标记 `truncated`；超时强制杀进程；`data/` 外的文件读取一律拒绝。
- 默认只监听 `127.0.0.1`。要跨机器用，请设 `WORKFLOWHUB_TOKEN`（请求头 `X-WorkflowHub-Token`）。
- 这些是调用链约束，**不是安全沙箱**：请只对可信 Agent 开放。

## 开发

```powershell
cd 'C:\Users\Administrator\Desktop\项目\workflowhub'
python -m pip install -e .
python -m workflowhub doctor                  # 环境自检（依赖、数据目录、界面、浏览器）
python -m workflowhub scan --roots "D:\脚本"   # 手动扫描并落盘目录
python -m workflowhub tools ffmpeg            # 命令行搜索
python -m workflowhub invoke cli-python --argv --version --dry-run

cd frontend && npm install && npm run build    # 产出 frontend/dist，被后端内嵌
python -m unittest discover -s tests          # 105 项测试（契约/发现/执行/目录/API/MCP/窗口/桌面启动与 Profile）
python scripts/verify_agent_surface.py        # 真机端到端 36 步：扫描→搜索→dry_run→执行→审批→DAG→MCP→旧契约
python scripts/build_exe.py                   # 产出根目录的 WorkflowHub.exe（单文件）
```

目录：`backend/workflowhub/` 核心与适配（`discovery` 扫描、`catalog` 目录与状态、`command_executor`
命令执行、`services` 三个入口共用的服务层、`api` REST、`mcp_server` MCP、`desktop/` 无边框窗口）；
`frontend/src/` 界面；`skills/` 手写 Skill；`examples/` 示例 DAG；`data/` 运行数据（不入库）；
`scripts/` 打包与图标；`docs/architecture/` 架构与决策记录。

## 打不开 / 报错排查

- **双击后弹 "Unhandled exception in script"**：早期版本在无控制台时 `print` 会炸。现在启动即接管 stdout/stderr 到 `data/desktop.log`，崩溃写 `data/desktop-error.log` —— 出问题先看这两个文件。
- **双击没反应但后台已有实例**：第二次启动会检测到运行中的实例，把它的窗口切到前面并干净退出（日志会写明端口）。确实要多开就设 `WORKFLOWHUB_FORCE=1`（每个实例用独立的 `data/browser-profile-<端口>/`，不会抢 Chromium 的 profile 锁）。
- **机器上没有 Edge/Chromium**：不再报错退出，而是自动用默认浏览器打开界面地址并保持服务运行；界面里的退出走 `POST /api/v1/system/quit`。
- **界面打开极慢或"本地服务启动失败：TimeoutError"**：端口本身是活的，但 `/health` 慢到回不了话 ——
  早期版本每条工具的调用统计都要重读并重新解析一遍审计文件，目录 1,500 条时一次健康检查要读上千次盘。
  现在审计尾部按 `(mtime, size)` 只读一次并建每工具索引（实测 `/health` 6～10ms），且这条有专门的性能门禁测试。
- **启动时提示换过端口仍失败**：`free_port()` 交还端口到真正绑定之间有竞态，现在会自动换端口重试最多 6 次，
  并把服务线程的真实异常写进 `data/desktop.log`（不再只留一句"超时"）。要钉死一个端口：
  `WorkflowHub.exe --port 53121`，双击场景设环境变量 `WORKFLOWHUB_PORT=53121`。
- **端口/鉴权**：只监听 `127.0.0.1`，随机端口写在 `data/desktop.json`；跨机访问请设 `WORKFLOWHUB_TOKEN`
  （旧契约的四个端点同样受它保护，漏带令牌是 401）。
- **`data/browser-profile/` 越用越大**：这是 Chromium 的登录态目录。两处会让它失控：① 早期版本按随机
  端口命名（`browser-profile-<端口>`），每双击一次就多一份；现在固定复用同一个 `browser-profile/`，并在
  启动时清掉端口已没人应答的旧目录（最近的会改名保留，登录态不丢）。② Edge 自己会往里面灌组件字典
  （`ProvenanceData` / `component_crx_cache` / `Edge Entity Extraction` / `Subresource Filter`…），
  本机实测堆到 452 MB。现在每次启动（浏览器起来之前）与退出（浏览器真退场之后）各删一次这些可再生的目录，
  实测回收 386 MB + 12 MB，`data/` 从 440 MB 回到 51 MB，登录态（`Default/Cookies`）不受影响。只有 `WORKFLOWHUB_FORCE=1` 多开时才按端口隔离 profile，
  因为 Chromium 对同一个 profile 只允许一个实例。
- **界面空白**：`frontend/dist` 缺失，执行 `cd frontend && npm install && npm run build`。

## 已知边界

- 调度是顺序的；条件分支、并行、人工复核恢复、自然语言 Planner 仍未实现（模型与端口已留好）。
- 脚本参数是从源码推断的（`add_argument` / `param()` / `process.argv`），不保证覆盖所有 CLI 风格；
  识别不到时用 `argv` 原样传参即可。
- CI 工作流（GitHub Actions）只作为蓝图列出，不能本地直接执行；别人的 Skill 包可在工具库里一键
  复制到 `skills/` 后编排。
- 无边框窗口依赖 Chromium 的 app 模式：软件把 Chromium 自绘的那条 31px 工具条裁掉，
  再让界面自己画窗口按钮。DPI 缩放由页面自报视口高度反推，已按 100% 实测。
