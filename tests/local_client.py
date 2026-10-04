"""测试里的"本机正当调用方"。

统一验收标准（personal-agent-hub/docs/AGENT_API_STANDARD.md）把边界收到四条判据上：
Host 必须是回环字面量、Origin 必须同源、非 GET 必须带共享口令。测试要走的正是
"用户自己双击 + Agent 读本机发现文件"那一条合法路径，所以每个用例都从这里起客户端，
而不是各自拼一套 —— 拼歪了就是假绿。

越界与缺口令那几类**反面**用例故意不走这里（见 tests/test_local_guard.py）。
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

from fastapi.testclient import TestClient

from workflowhub.api import create_app

#: 桌面端是随机端口，测试里固定成一个就够：判据看的是"Host 的端口 == 实际监听端口"，
#: 而 TestClient 会把 base_url 同时写进 Host 头与 ASGI scope["server"]。
LOCAL_BASE_URL = "http://127.0.0.1:8794"

#: 交付默认（`approval_mode=True`、`high_risk_enabled=False`）之下，凡是解释器名字落在
#: 高风险名单里的工具（`python`、`powershell` 都算）默认**不可调用**。
#: 本文件的用例测的是"用户已经把开关打开之后的调用链"，所以在这里替他走那一步 ——
#: 集中一处，别在 12 个用例里各写一遍。"默认必须是关的"由 tests/test_local_guard.py 钉住。
UNLOCK = {"high_risk_enabled": True, "approval_mode": False}


def unlock(hub) -> None:
    """把调用链用例需要的那两个开关打开（等价于用户在 设置 页里勾两下）。

    `registry.refresh()` 不是多余的：注册表是"当前可调用"的 Skill 快照，开了高风险不刷新
    就还是 `unknown Skill` —— 同一条判据现在也写在 `PUT /api/v1/settings` 里。
    """
    hub.catalog.update_settings(dict(UNLOCK))
    hub.registry.refresh()


def local_app(hub: Any):
    """按"首次启动"的样子建 app，并把生成好的口令挂在返回值上方便断言。"""
    return create_app(hub)


def local_client(hub: Any, *, app: Any = None, keep_defaults: bool = False,
                 **kwargs: Any) -> TestClient:
    """带共享口令的回环客户端：`headers` 里那把口令来自 app.state.token。"""
    if not keep_defaults:
        unlock(hub)
    app = app or create_app(hub)
    headers = {"X-WorkflowHub-Token": str(app.state.token)}
    headers.update(kwargs.pop("headers", {}) or {})
    return TestClient(app, base_url=LOCAL_BASE_URL, headers=headers, **kwargs)


def token_of(app: Any) -> str:
    return str(app.state.token)


def endpoint_files(hub: Any) -> list[Path]:
    return [Path(p) for p in getattr(hub, "endpoint_files", []) or []]
