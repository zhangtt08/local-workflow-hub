# ADR-007：无边框桌面外壳（自绘窗口控件 + exe 打包）

- 状态：已接受
- 日期：2026-10-03
- 相关：`backend/workflowhub/desktop/window.py`、`desktop/launcher.py`、`frontend/src/winbar.ts`、`scripts/WorkflowHub.spec`

## 背景

要求是"系统边框（─ □ ✕）融入软件内部并封装为桌面 exe"，成品不能看起来像"浏览器里开了个网页"。
本机已验证的事实：pywebview/WebView2 无法桥接 JS（WebView2 托管程序集缺失）；Electron/Tauri 需要
在线安装；而 Chromium 的 `--app` 窗口会在客户区顶部自绘约 31px 工具条，**清掉 `WS_CAPTION` 也不会消失**
（样式位确实被清，但条仍然绘制，视口仍下移 31px）。

## 决策

1. **外壳 = 本机已有的 Chromium 内核浏览器（Edge 优先）以 `--app` 启动**，由 Python 进程负责
   生命周期：起 uvicorn（随机端口、只绑 127.0.0.1）→ 健康检查 → 拉起浏览器 → 认领窗口 →
   浏览器退出即关服务。
2. **裁掉 Chromium 自绘工具条**：`SetWindowLongW` 去掉 `WS_CAPTION|WS_THICKFRAME|WS_SYSMENU`，
   再用 `SetWindowRgn` 把客户区顶部 `bar` 像素裁出窗口区域，并把窗口整体上移 `bar` 像素，
   于是"可见矩形"就是网页内容本身。`bar` 不写死：页面加载后上报 `innerHeight × devicePixelRatio`，
   服务端用 `clientHeight - innerHeight × dpr` 反算，DPI 缩放与主题变化都能自适应。
3. **窗口几何全部走 Win32**（`SetWindowPos`），最大化/贴靠用工作区矩形减去 `bar` 再补回，
   所以最大化时顶部不会露出 31px 桌面缝隙；窗口态用圆角区域、最大化态用直角，贴近原生观感。
4. **交互由界面自绘**：标题栏、─ □ ✕、拖拽移动、八向拉伸、双击最大化、拖到屏幕边缘贴靠，
   全部通过 `POST /api/v1/window/action` 下发；指针位移用 `movementX/Y` 累加，避免"窗口跟着鼠标走
   导致 clientX 不变"的反馈死循环。非桌面模式（浏览器预览）这些端点明确返回不可用，界面显示
   "浏览器预览"而不是假装能控制窗口。
5. **打包用 PyInstaller onedir**（`console=False`、`icon=assets/app.ico`），`data/skills/examples`
   落在 exe 同级目录，界面与资源通过 `--add-data` 内嵌；不内置浏览器，因此包体 81 MB。
   选择 onedir 而非 onefile：启动不解压、可就地排查、避免杀软误报。
6. **图标一体化**：同一张图生成 `app.ico`（16→256 多尺寸）、界面 SVG 标记与 `favicon.ico`，
   并用 `WM_SETICON` 覆盖到 app 窗口，任务栏与标题栏一致；16px 版本做对比度校验，避免小尺寸糊掉。

## 备选方案

- Electron/Tauri：需要联网装工具链，且与"零依赖优先"的偏好冲突。
- WinForms + WebView2：本机 WebView2 托管包装器缺失，离线不可构建。
- pywebview：JS 桥在本机静默失效（`window.pywebview.api` 不出现），无法承载界面逻辑。
- CDP `Browser.setWindowBounds` + `windowState: fullscreen`：确实能去条，但窗口会变成全屏、
  无法做可缩放/贴靠的窗口态，因此只作为思路记录，未采用。

## 后果

- 正面：一个进程、一个端口、一套界面；没有系统边框与滚动条痕迹，任务栏图标与界面同源；
  同一份代码在浏览器里也能跑（便于开发调试），差异被显式标注。
- 代价：依赖机器上存在 Chromium 内核浏览器；`SetWindowRgn` 是窗口区域裁剪，因此窗口阴影由
  界面自己用边框+圆角模拟；取证只能用 `PrintWindow`（GPU 合成下常返回空白），验证外观需屏幕截图。
- 风险：Chromium 版本改变工具条高度或 `--app` 行为时，依赖页面自报视口重新计算 `bar`；
  若某天条高为 0，逻辑自动退化为不裁剪（`bar = 0`）。
