"""Desktop entry point: one process serves UI + API and owns a borderless app window.

The visible web contents are shown without Chromium's own app bar (see ``window.py``), so the
product reads as a single application: the title bar, icon and window behaviour all come from
the app itself.
"""

from __future__ import annotations

import json
import os
import shutil
import socket
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any

import uvicorn

from ..api import create_app
from ..bootstrap import build_hub, static_root
from ..local_guard import publish_endpoint, retire_endpoint
from .window import WindowController, find_window, make_dpi_aware

BROWSER_SLOTS = (
    r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
    r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
    r"C:\Program Files\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
)
APP_TITLE = "WorkflowHub"


def bundled_asset(name: str) -> Path | None:
    roots = []
    if getattr(sys, "frozen", False):
        roots += [Path(sys._MEIPASS) / "assets", Path(sys.executable).parent / "assets"]  # type: ignore[attr-defined]
    roots.append(Path(__file__).resolve().parents[3] / "assets")
    for root in roots:
        candidate = root / name
        if candidate.is_file():
            return candidate
    return None


def chromium_from_playwright() -> str | None:
    base = Path(os.environ.get("PLAYWRIGHT_BROWSERS_PATH") or
                Path.home() / "AppData" / "Local" / "ms-playwright")
    if not base.is_dir():
        return None
    for folder in sorted(base.glob("chromium-*"), reverse=True):
        for name in ("chrome-win/chrome.exe", "chrome-win64/chrome.exe"):
            candidate = folder / name
            if candidate.is_file():
                return str(candidate)
    return None


def browser_candidates() -> list[str]:
    out = []
    for slot in BROWSER_SLOTS:
        if Path(slot).is_file():
            out.append(slot)
    local = Path(os.environ.get("LOCALAPPDATA", "")) / "Microsoft/Edge/Application/msedge.exe"
    if local.is_file():
        out.append(str(local))
    fallback = chromium_from_playwright()
    if fallback:
        out.append(fallback)
    return out


def free_port() -> int:
    import socket

    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


def clear_stop_marker(data: Path) -> None:
    """上一轮的退出标记不能留给下一次：浏览器兜底路径是"看到 `desktop.stop` 就收摊"，
    残留标记会让新实例刚起来就自杀。"""
    (Path(data) / "desktop.stop").unlink(missing_ok=True)


def existing_instance(data_dir: Path) -> dict[str, Any] | None:
    """Another WorkflowHub may already be running; a second double-click must not die on the
    Chromium profile lock or open a duplicate tray of processes."""
    marker = Path(data_dir) / "desktop.json"
    if not marker.is_file():
        return None
    try:
        info = json.loads(marker.read_text(encoding="utf-8"))
        import urllib.request

        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        with opener.open(f"http://127.0.0.1:{int(info['port'])}/health", timeout=1.2) as response:
            if json.loads(response.read().decode("utf-8")).get("status") == "ok":
                return info
    except Exception:
        return None
    return None


def focus_instance(info: dict[str, Any]) -> bool:
    """把已有实例的窗口带到前面 —— 这是**非 GET**，所以必须带本机共享口令。

    口令从它自己写的 `data/desktop.json` 里读（同一个二进制、同一台机器、同一个用户，
    读得到；浏览器读不到）。不带口令的第二次双击只会得到 401，然后表现为"没反应"。
    """
    try:
        import urllib.request

        body = json.dumps({"action": "focus"}).encode("utf-8")
        headers = {"Content-Type": "application/json"}
        token = str(((info.get("auth") or {}).get("token")) or "")
        if token:
            headers["X-WorkflowHub-Token"] = token
        request = urllib.request.Request(f"http://127.0.0.1:{info['port']}/api/v1/window/action",
                                         data=body, headers=headers, method="POST")
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        with opener.open(request, timeout=2.0) as response:
            return bool(json.loads(response.read().decode("utf-8")).get("ok"))
    except Exception:
        return False


def port_alive(port: int) -> bool:
    """只有真的收到 HTTP 应答才算活着。

    这台机器上"没人监听"表现为连接挂住而不是拒绝（本地代理/TUN 层会吞掉连接），所以 connect
    成功与否不能作数 —— 判反了会把还在用的 Profile 当垃圾删掉。
    """
    import urllib.error
    import urllib.request

    try:
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        with opener.open(f"http://127.0.0.1:{port}/health", timeout=0.6) as response:
            response.read(64)
        return True
    except urllib.error.HTTPError:
        return True  # 404/501 也算"有人在答"
    except Exception:
        return False


#: Edge 会把这些组件字典/缓存灌进 profile（本机实测一次就 354 MB），但它们每次都会重新下载，
#: 而登录态在 `Default/` 里 —— 所以清前者、保后者。
VOLATILE_PROFILE_PATTERNS = ("Provenance*", "component_crx_cache", "Edge Entity*", "Subresource*",
                             "Default/*Cache*", "Default/*Code Cache*", "GrShaderCache", "ShaderCache")


def trim_profile(profile: Path) -> int:
    """删掉可再生的组件/缓存目录，返回删掉的字节数。必须在启动浏览器之前调用。"""
    if not profile.is_dir():
        return 0
    freed = 0
    for pattern in VOLATILE_PROFILE_PATTERNS:
        for path in profile.glob(pattern):
            try:
                size = sum(f.stat().st_size for f in path.rglob("*") if f.is_file()) if path.is_dir() else path.stat().st_size
            except OSError:
                continue
            shutil.rmtree(path, ignore_errors=True) if path.is_dir() else path.unlink(missing_ok=True)
            if not path.exists():
                freed += size
    return freed


def profile_dir(data: Path, port: int) -> Path:
    """默认复用同一个 Chromium Profile。

    以前按端口命名（`browser-profile-<port>`），而每次双击端口都是随机的，等于每次启动
    复制一个新的 Edge profile（实测每个 40～70 MB）—— 用几天就是几个 GB。只有强制多开时
    才需要按端口隔离，因为 Chromium 对同一个 profile 只允许一个实例。
    """
    shared = data / "browser-profile"
    if os.environ.get("WORKFLOWHUB_FORCE") == "1":
        return data / f"browser-profile-{port}"
    stale = sorted((p for p in data.glob("browser-profile-*") if p.name.rsplit("-", 1)[-1].isdigit()),
                   key=lambda p: p.stat().st_mtime, reverse=True)
    for path in stale:
        old_port = int(path.name.rsplit("-", 1)[-1])
        if old_port == port or port_alive(old_port):
            continue                      # 端口还在应答 = 有实例在用这个 profile，不碰
        if not shared.exists():
            try:
                path.rename(shared)       # 最近一个空闲的旧目录转成共享目录，保住已登录状态
                continue
            except OSError:
                return path
        shutil.rmtree(path, ignore_errors=True)
    return shared


def start_server(app: Any, host: str, port: int) -> tuple[uvicorn.Server, threading.Thread, list[str]]:
    """起 uvicorn 子线程，并把线程里的死因抓出来 —— 不然端口没起来时只会看到"健康检查超时"。"""
    errors: list[str] = []
    level = os.environ.get("WORKFLOWHUB_LOG_LEVEL", "warning")
    config = uvicorn.Config(app, host=host, port=port, log_level=level,
                            access_log=False, lifespan="on")
    server = uvicorn.Server(config)

    def serve() -> None:
        try:
            server.run()
        except BaseException as exc:  # noqa: BLE001 - 线程里异常不抓走就只剩超时这一条线索
            errors.append(f"{type(exc).__name__}: {exc}")
            server.should_exit = True

    thread = threading.Thread(target=serve, daemon=True, name="workflowhub-server")
    thread.start()
    return server, thread, errors


def wait_for_server(port: int, instance: str = "", timeout: float = 30.0) -> tuple[bool, str]:
    """Poll our own health endpoint with proxies disabled: this machine sets HTTP_PROXY globally.

    还要求应答里带本次实例的身份：这台机器上有一层会接受任意本地端口连接却不回话的代理/TUN，
    端口被它吞掉时 connect 成功、响应永远不来，只等 `status==ok` 就会把真因写成"超时"。
    """
    import json
    import urllib.request

    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    deadline = time.time() + timeout
    last = ""
    while time.time() < deadline:
        try:
            with opener.open(f"http://127.0.0.1:{port}/health", timeout=1.5) as response:
                payload = json.loads(response.read().decode("utf-8"))
            if payload.get("status") == "ok" and (not instance or payload.get("instance") == instance):
                return True, ""
            last = f"端口 {port} 上的应答不属于本次实例：{str(payload)[:100]}"
        except Exception as exc:
            last = f"{type(exc).__name__}: {exc}"
        time.sleep(0.15)
    return False, last


def serve_ready(app: Any, instance: str, host: str, preferred: int | None = None,
                attempts: int = 6) -> tuple[uvicorn.Server | None, threading.Thread | None, int, str, list[str]]:
    """挑一个确实可用的端口。

    `free_port()` 探到空闲后会交还，uvicorn 再绑之间有别的过程抢占的竞态；失败就换端口重试，
    而不是双击一次打不开就报错走人。
    """
    errors: list[str] = []
    last = ""
    for attempt in range(attempts):
        port = preferred if preferred is not None else free_port()
        server, thread, errors = start_server(app, host, port)
        ready, why = wait_for_server(port, instance, timeout=8.0)
        if ready:
            return server, thread, port, "", errors
        last = f"{why or '健康检查超时'}（端口 {port}）"
        if errors:
            last = f"{last}；服务线程退出：{errors[-1]}"
        server.should_exit = True
        thread.join(timeout=3)
        if preferred is not None:
            break
    return None, None, 0, f"{last}（换过 {attempt + 1} 次端口）", errors


def launch_browser(exe: str, url: str, profile: Path, size: tuple[int, int]) -> subprocess.Popen:
    profile.mkdir(parents=True, exist_ok=True)
    args = [
        exe, f"--app={url}", f"--user-data-dir={profile}",
        f"--window-size={size[0]},{size[1]}", "--window-position=120,90",
        "--no-first-run", "--no-default-browser-check", "--disable-session-crashed-bubble",
        "--disable-features=Translate,MediaRouter,GlobalMediaControls,MediaControlsFuturePrivacyFilter",
        "--disable-background-timer-throttling", "--disable-popup-blocking", "--no-service-autorun",
        "--export-tagged-pdf", "--test-type",
    ]
    return subprocess.Popen(args, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                            stderr=subprocess.DEVNULL)  # noqa: S603


def run_desktop(host: str = "127.0.0.1", port: int | None = None, start: str = "maximized",
                url: str | None = None, browser: str | None = None, headless_check: bool = False) -> int:
    if os.name != "nt":
        raise SystemExit("桌面窗口模式仅支持 Windows；请改用 serve + 浏览器访问")
    make_dpi_aware()
    hub = build_hub()
    running = existing_instance(hub.roots["data"])
    if running and os.environ.get("WORKFLOWHUB_FORCE") != "1":
        focused = focus_instance(running)
        print(f"已有 WorkflowHub 在运行（端口 {running['port']}），{'已把它的窗口切到前面' if focused else '请直接使用已打开的窗口'}；"
              f"要开第二个实例请设 WORKFLOWHUB_FORCE=1")
        return 0
    if static_root() is None and url is None:
        print("未找到已构建的前端：请先在 frontend 目录执行 npm install && npm run build")
    os.environ["WORKFLOWHUB_DESKTOP"] = "1"
    clear_stop_marker(hub.roots["data"])
    if port is None:
        wanted = os.environ.get("WORKFLOWHUB_PORT", "").strip()
        port = int(wanted) if wanted.isdigit() and 1024 <= int(wanted) <= 65535 else None
    app = create_app(hub)
    # 口令由 create_app 保证存在（没有就当场生成并落盘）；这里把它挂回 hub，
    # `publish_endpoint` 与后面的自检都读同一份，不再各自算一遍。
    hub.token = str(getattr(app.state, "token", ""))
    server, thread, chosen_port, why, _ = serve_ready(app, hub.instance, host, preferred=port)
    if server is None or thread is None:
        print(f"本地服务启动失败：{why or '健康检查超时'}")
        print(f"可在命令行指定一个空闲端口重试：WorkflowHub.exe --port 53121（或设 WORKFLOWHUB_PORT）")
        return 1
    target = url or f"http://127.0.0.1:{chosen_port}/index.html"

    controller = WindowController(0, APP_TITLE, browser or "")
    if headless_check:
        print(f"服务已就绪：{target}")
        return 0

    candidates = [browser] if browser else browser_candidates()
    if not candidates:
        print(f"没有可用的 Chromium/Edge 窗口程序，请在浏览器中打开：{target}")
        server.should_exit = False
        thread.join()
        return 0

    profile = profile_dir(hub.roots["data"], chosen_port)
    freed = trim_profile(profile)          # 趁浏览器还没起来再清，边跑边删文件会把 Edge 弄坏
    if freed:
        print(f"已清理 Chromium 可再生缓存 {freed // 1048576} MB（登录态保留在 Default/ 内）")
    process: subprocess.Popen | None = None
    last_error = ""
    for exe in candidates:
        process = launch_browser(exe, target, profile, (1280, 820))
        controller.pid = process.pid
        controller.browser_exe = exe
        hwnd = controller.wait(timeout=9.0)
        if hwnd:
            break
        if process.poll() is not None:
            last_error = f"{exe} 退出码 {process.returncode}"
            process = None
            continue
        last_error = f"{exe} 未出现窗口"
        process.terminate()
        process = None
    if not process or not controller.hwnd:
        # No window is not a reason to die: hand the user a browser and keep the service alive.
        reason = last_error or "窗口未出现"
        print(f"未能打开桌面窗口（{reason}），改用默认浏览器打开 {target}")
        stopper = hub.roots["data"] / "desktop.stop"
        stopper.unlink(missing_ok=True)
        try:
            os.startfile(target)  # noqa: S606
        except OSError:
            print("请手动在浏览器打开上面的地址")
        publish_endpoint(hub, port=chosen_port, url=target, pid=os.getpid(),
                         extra={"window": None, "fallback": reason})
        try:
            while not stopper.exists():
                time.sleep(0.5)
        except KeyboardInterrupt:
            pass
        finally:
            stopper.unlink(missing_ok=True)
            retire_endpoint(hub)
            server.should_exit = True
        return 0

    icon = bundled_asset("app.ico")
    if icon:
        controller.set_icon(icon)
    if start == "maximized":
        controller.act("maximize")
    else:
        controller.visible = controller._default_rect()
        controller.maximized = False
        controller._apply(controller.visible)
    app.state.window = controller
    publish_endpoint(hub, port=chosen_port, url=target, pid=process.pid,
                     extra={"window": controller.state()})
    hub.catalog.audit({"type": "desktop_window", "actor": "desktop", "hwnd": controller.hwnd,
                       "status": "attached", "command_line": " ".join([str(a) for a in process.args])})

    try:
        while process.poll() is None:
            if not controller.is_alive():
                replacement = find_window(process.pid, APP_TITLE, controller.browser_exe)
                if not replacement:
                    break
                controller.attach(replacement)
            time.sleep(0.35)
    except KeyboardInterrupt:
        pass
    finally:
        retire_endpoint(hub)
        clear_stop_marker(hub.roots["data"])   # 界面退出按钮写的标记不能留给下一次启动
        app.state.window = None
        server.should_exit = True
        if process.poll() is None:
            controller.close()
            try:
                process.wait(timeout=4)
            except subprocess.TimeoutExpired:
                process.terminate()
                try:
                    process.wait(timeout=4)      # 浏览器还没退就动它的目录会写坏 profile
                except subprocess.TimeoutExpired:
                    pass
        time.sleep(0.3)
        closed = trim_profile(profile)           # 用一次涨一次也要收：退出时再回收一遍
        if closed:
            print(f"退出时回收 Chromium 可再生缓存 {closed // 1048576} MB")
    return 0
