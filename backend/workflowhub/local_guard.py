"""本机边界 —— 谁能驱动这个服务，判据只有一份。

为什么需要这一层（2026-10-05 验收会上定性的两条 BLOCKER）：

改之前这里只有一条"Origin 等于 Host 就放行"的判断，而 Host 是**请求自己带来的**，
`allow_origins=["*"]` 又把所有跨源调用都放开，于是任何网页都能驱动本机 1,529 个能力：

* DNS rebinding —— 恶意域名解析到 127.0.0.1，页面带着 `Host: evil.test:8794` 打进来，
  它和自己的 Host 自然"同源"，于是守卫形同没有（`evil.test` 本身就是被允许的那一方）；
* 没有配 `WORKFLOWHUB_TOKEN` 时（默认就是没配）任何请求都不带 token 也照样通过，
  `POST /api/v1/tools` 登记一条绝对路径命令、`/invoke` 立刻把它跑起来 = 任意进程启动。

所以这里的判据按"默认拒绝"来写，四条各自独立、互为兜底：

1. **Host 必须是字面量回环**：`127.0.0.1` / `localhost` / `[::1]`，端口必须等于服务真正
   监听的那个端口（从 ASGI `scope["server"]` 取，不靠配置，也不会和实际端口漂移）。
   这条与请求自己的 Origin 无关 —— rebinding 场景下 Host 就是 `evil.test`，直接拒。
2. **Origin / Referer 必须等于同一个 `host:port`**：只在浏览器会送的那几个头上判，
   命令行调用方（不送 Origin）走第 4 条的共享口令。
3. **不再送 `Access-Control-Allow-Origin: *`**：CORS 中间件整个移除，界面与自己是同源，
   别的源一律拿不到读权限（见 `api.py` 里那段注释）。
4. **非 GET 一律要共享口令**（timing-safe 比较）。口令是首次启动自己生成的，
   落在 `data/token.json`（0600），并随实际端口写进 `data/desktop.json` 与
   `agent/.endpoint` —— 本机的正当调用方（桌面壳、Agent、MCP 桥）读得到这些文件，
   网页读不到（同源策略 + 上面第 1、2 条）。

还有一条边界要写清楚：界面（Chromium 窗口里的页面）没有地方填口令，所以口令由服务
在**已通过第 1、2 条的回环请求**上以 `HttpOnly; SameSite=Strict` 的 Cookie 发给它，
并且这条路上额外要求 `Origin` 与 Host 逐字相等 —— 因为 Cookie 按域名（含回环 IP）
共享而**不按端口**共享，同一台机器上另一个端口的网页本来能白骑这个 Cookie。
"""

from __future__ import annotations

import hmac
import json
import os
import re
import secrets
import stat
import time
from pathlib import Path
from typing import Any, Iterable
from urllib.parse import urlsplit

from .errors import WorkflowHubError
from .models import ErrorCode

#: 字面量回环主机名。`0.0.0.0` 与局域网地址不在名单里，也永远不该出现在这里。
LOOPBACK_HOSTS = frozenset({"127.0.0.1", "localhost", "::1"})
TOKEN_ENV = "WORKFLOWHUB_TOKEN"
#: 唯一的加白名单出口（反向代理/开发模式的 Vite 端口）。逗号分隔的 `host[:port]`。
EXTRA_HOSTS_ENV = "WORKFLOWHUB_ALLOWED_HOSTS"
MAX_BODY_ENV = "WORKFLOWHUB_MAX_BODY_BYTES"
TOKEN_HEADER = "x-workflowhub-token"
SESSION_COOKIE = "workflowhub_session"
TOKEN_FILE = "token.json"
ENDPOINT_FILE = "agent-endpoint.json"
#: 请求体上限：没有上限时一个 20 GB 的 POST 能把这台机器的内存吃光（第 4 号 NICE）。
DEFAULT_MAX_BODY_BYTES = 8 * 1024 * 1024

_BRACKETED = re.compile(r"^\[(.*)\]$", re.S)


def split_host(value: str | None) -> tuple[str, int | None]:
    """`[::1]:8794` / `127.0.0.1:8794` / `localhost` → (主机名小写, 端口或 None)。"""
    text = str(value or "").strip()
    if not text:
        return "", None
    if text.startswith("["):
        closed = text.find("]")
        if closed < 0:
            return text.lower(), None
        host = _BRACKETED.sub(r"\1", text[: closed + 1])
        rest = text[closed + 1:]
    else:
        host, _, rest = text.partition(":")
        if ":" in rest:                      # `a:b:c` 不是 host:port，整段当主机名
            return text.lower(), None
    port: int | None = None
    rest = rest.lstrip(":")
    if rest.isdigit():
        port = int(rest)
    return host.strip().lower().rstrip("."), port


def allowed_hosts() -> dict[str, str | None]:
    """字面量回环 + 运维显式加白的那几个主机名（`WORKFLOWHUB_ALLOWED_HOSTS`）。"""
    extra = os.environ.get(EXTRA_HOSTS_ENV, "")
    out = {host: None for host in LOOPBACK_HOSTS}
    for item in extra.split(","):
        host, port = split_host(item)
        if host:
            out[host] = port
    return out


def host_problem(value: str | None, *, bound_port: int | None = None) -> str:
    """空字符串 = 放行；否则返回一句能直接给人看的拒绝理由。

    `bound_port` 来自 ASGI `scope["server"][1]`，也就是**这个进程真的绑在哪个端口上**，
    不是配置里写的期望值 —— 桌面端每次双击端口都是随机的，判据必须跟着真实端口走。
    """
    host, port = split_host(value)
    if not host:
        return "缺少 Host 头"
    known = allowed_hosts()
    if host not in known:
        return f"Host 不是本机回环地址：{host or '（空）'}"
    pinned = known[host]
    wanted = pinned if pinned is not None else bound_port
    if wanted is not None and wanted > 0:
        if port is None:
            return f"Host 端口与服务实际端口 {wanted} 不一致：{value}"
        if port != wanted:
            return f"Host 端口 {port} 不是本服务监听的端口 {wanted}"
    return ""


def origin_problem(value: str | None, host: str | None) -> str:
    """Origin / Referer 必须指向**同一个** host:port —— 拿它和请求自己的 Host 比是本次的 bug。

    这里比的是"Origin 的 host:port"与"Host 的 host:port（端口缺省时补上实际监听端口）"，
    两边都先过一遍字面量回环判定，所以一个把 Host 伪造成 `127.0.0.1:8794` 的跨源请求
    也过不去：它的 Origin 是 `http://evil.test`。
    """
    text = str(value or "").strip()
    if not text:
        return ""                                    # 不送 Origin 的调用方由口令那条判
    parts = urlsplit(text)
    origin_host, origin_port = parts.hostname or "", parts.port
    if parts.scheme not in ("http", "https"):
        return f"Origin 协议不是 http(s)：{text}"
    if host_problem(f"{origin_host}:{origin_port}" if origin_port else origin_host) :
        return f"Origin 不是本机回环地址：{text}"
    target_host, target_port = split_host(host)
    if target_port is None:
        target_port = int(os.environ.get("WORKFLOWHUB_PORT", "0") or 0) or None
    if origin_host != target_host:
        return f"Origin 主机 {origin_host} 与请求的 Host 主机 {target_host} 不同源"
    if target_port is not None and origin_port != target_port:
        return f"Origin 端口 {origin_port} 与请求的 Host 端口 {target_port} 不同源"
    return ""


# ── 口令：首次启动自己生成，落盘权限收紧 ────────────────────────────────────────
def _restrict(path: Path) -> None:
    """尽力收紧到"只有本人可读写"。

    Windows 上 chmod 只能设/清只读位（NTFS 的真实边界是acl，而 `data/` 在用户配置文件
    目录下，本来就只对该用户可见），POSIX 上是货真价实的 0600。两条都写，并且**不把
    Windows 那半边说成做到了**：判据写在 README 的安全段落里，别让人以为它是 ACL。
    """
    try:
        path.chmod(stat.S_IRUSR | stat.S_IWUSR)
    except OSError:
        pass


def token_path(data_dir: Path | str) -> Path:
    return Path(data_dir) / TOKEN_FILE


def read_token(data_dir: Path | str) -> str:
    path = token_path(data_dir)
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return ""
    return str(raw.get("token") or "").strip()


def ensure_token(data_dir: Path | str) -> tuple[str, str]:
    """返回 (口令, 来源说明)。默认**必配**：没有就当场生成一个并落盘。

    三条来源按优先级：环境变量 `WORKFLOWHUB_TOKEN`（显式覆盖，运维想统一发牌时用）
    → `data/token.json`（上一次生成的）→ 新生成。生成时写文件是原子的，权限 0600。
    """
    from_env = os.environ.get(TOKEN_ENV, "").strip()
    if from_env:
        return from_env, f"env:{TOKEN_ENV}"
    path = token_path(data_dir)
    # 注意传的是**目录**：`read_token` 自己会拼 `TOKEN_FILE`。这里以前直接把文件路径递进去，
    # 于是它去读 `data/token.json/token.json`，永远读不到 → 每次启动都换一把新口令，
    # 桌面壳/Agent/MCP 手里的发现和界面 Cookie 全部对不上（401 循环）。
    stored = read_token(data_dir)
    if stored:
        return stored, str(path)
    token = secrets.token_urlsafe(32)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps({
        "token": token,
        "created_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "note": "WorkflowHub 的本机共享口令：非 GET 请求要带 X-WorkflowHub-Token。"
                "删掉这个文件会在下次启动时换一把新口令；改环境变量的那一条覆盖它。",
    }, ensure_ascii=False, indent=1), encoding="utf-8")
    _restrict(tmp)
    tmp.replace(path)
    _restrict(path)
    return token, str(path)


def token_matches(provided: str | None, expected: str) -> bool:
    """timing-safe 比较 —— 口令对不对不该由响应时间泄露。"""
    if not expected:
        return True
    return hmac.compare_digest(str(provided or ""), expected)


def session_cookie(expected_token: str) -> str:
    """给已通过回环判定的浏览器会话发的 Cookie 值（就是那把共享口令）。"""
    return f"{SESSION_COOKIE}={expected_token}; Path=/; HttpOnly; SameSite=Strict"


def write_endpoint(data_dir: Path | str, *, port: int, url: str, pid: int,
                   token: str, extra: dict[str, Any] | None = None) -> Path:
    """把"实际地址 + 口令"写给本机调用方：`agent/.endpoint` 与 `data/desktop.json`。

    统一标准（personal-agent-hub/docs/AGENT_API_STANDARD.md）说端口只是默认值，
    调用方一律读 `agent/.endpoint`；而 hub 的 `src/hub.js` 现在读的是
    `workflowhub/data/desktop.json`。两处都写，同一把口令，别让它分叉。
    """
    payload: dict[str, Any] = {
        "url": url, "base_url": url, "port": port, "pid": pid,
        "host": "127.0.0.1",
        "auth": {"header": "X-WorkflowHub-Token", "token": token},
        "note": "只在本机可达：服务只绑 127.0.0.1，且 Host/Origin 必须是回环字面量。",
        "written_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        **(extra or {}),
    }
    data = Path(data_dir)
    data.mkdir(parents=True, exist_ok=True)
    target = data / ENDPOINT_FILE
    target.write_text(json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8")
    _restrict(target)
    return target


# ── 路径围堵：读取与写出共用同一把尺 ───────────────────────────────────────────
def require_within(raw: str | Path, roots: Iterable[Path | str], *, what: str = "文件") -> Path:
    """把调用方给的路径按 `roots` 围住，越界就当场说清楚。

    `/api/v1/files` 一直是对的（只允许 data/ 与 skills/），而 `/window/capture?path=`
    把同一个参数直接交给窗口去写文件，等于给了一个任意路径写点（第 3 号 BLOCKER）。
    两处现在共用这一把尺：先 resolve（Windows 上 `..`、短名、大小写都在这里被折平），
    再逐个 root 比 `is_relative_to`。
    """
    text = str(raw or "").strip()
    if not text:
        raise WorkflowHubError(ErrorCode.INPUT_ERROR, f"{what}路径是空的")
    candidates = [Path(root).resolve() for root in roots]
    try:
        target = Path(text).expanduser().resolve()
    except OSError as exc:
        raise WorkflowHubError(ErrorCode.INPUT_ERROR,
                               f"{what}路径读不出来：{exc}", {"path": text}) from exc
    for root in candidates:
        if target == root or target.is_relative_to(root):
            return target
    raise WorkflowHubError(
        ErrorCode.PERMISSION_ERROR,
        f"{what}只能落在允许的目录内：{target}",
        {"path": str(target), "allowed_roots": [str(r) for r in candidates],
         "hint": "把路径指到 data/（运行产物与截图）或 skills/ 里面再试"},
    )


def publish_endpoint(hub: Any, *, port: int, url: str, pid: int,
                     extra: dict[str, Any] | None = None) -> dict[str, Any]:
    """把"实际地址 + 本机口令"写给调用方：`data/desktop.json`、`data/agent-endpoint.json`、
    以及统一标准里的 `agent/.endpoint`。

    为什么要程序写而不是让用户配环境变量（第 1 号 BLOCKER 的"零配置"那半条）：桌面端每次
    双击都是**随机端口**，口令又是首次启动生成的，这两件事用户都不该知道 —— 他双击就能用。
    而网页拿不到这个文件：同源策略读不了本地磁盘，跨源请求又先被 Host/Origin 判据拒掉。
    三处写的是同一把口令，别让它分叉。
    """
    token = str(getattr(hub, "token", "") or ensure_token(hub.roots["data"])[0])
    payload: dict[str, Any] = {
        "url": url, "port": port, "pid": pid, "host": "127.0.0.1",
        "started_at": time.time(), "token": token,
        "auth": {"header": "X-WorkflowHub-Token", "token": token},
        "note": "只在本机可达：服务只绑 127.0.0.1，Host/Origin 还必须是回环字面量。",
        **(extra or {})}
    endpoint = Path(hub.roots["data"]) / "desktop.json"
    endpoint.parent.mkdir(parents=True, exist_ok=True)
    endpoint.write_text(json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8")
    _restrict(endpoint)
    published = [endpoint, write_endpoint(hub.roots["data"], port=port, url=url, pid=pid,
                                         token=token, extra=extra)]
    agent_dir = Path(hub.roots["project"]) / "agent"
    if agent_dir.is_dir():
        # 统一标准的发现文件（AGENT_API_STANDARD.md：端口只是默认值，调用方一律读
        # agent/.endpoint）。打包出去的目录里没有 agent/ 就跳过，不为此造目录。
        try:
            marker = agent_dir / ".endpoint"
            marker.write_text(json.dumps({k: payload[k] for k in ("url", "port", "pid", "token", "auth")},
                                         ensure_ascii=False, indent=1), encoding="utf-8")
            _restrict(marker)
            published.append(marker)
        except OSError:
            pass
    hub.endpoint_files = published
    return payload


def retire_endpoint(hub: Any) -> None:
    """服务停了就把发现文件一起收掉：留着它，调用方会一直往一个死端口上打。

    只删**本次发布写下的**那几个（`hub.endpoint_files`）加本 data 目录里那两个：
    仓库里的 `agent/.endpoint` 可能是另一个实例写的，测试更是绝不该碰它。
    """
    targets = [Path(p) for p in getattr(hub, "endpoint_files", []) or []]
    targets += [Path(hub.roots["data"]) / "desktop.json",
                Path(hub.roots["data"]) / "agent-endpoint.json"]
    for path in targets:
        try:
            path.unlink(missing_ok=True)
        except OSError:
            pass


def max_body_bytes() -> int:
    raw = os.environ.get(MAX_BODY_ENV, "").strip()
    if raw.isdigit():
        return max(1024, int(raw))
    return DEFAULT_MAX_BODY_BYTES


__all__ = ["LOOPBACK_HOSTS", "MAX_BODY_ENV", "TOKEN_ENV", "TOKEN_FILE", "TOKEN_HEADER",
           "ENDPOINT_FILE", "EXTRA_HOSTS_ENV", "SESSION_COOKIE", "allowed_hosts",
           "host_problem", "max_body_bytes", "origin_problem", "publish_endpoint",
           "read_token", "require_within", "retire_endpoint", "session_cookie", "split_host",
           "token_matches", "token_path", "ensure_token", "write_endpoint"]
