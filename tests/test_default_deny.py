"""交付出去的那一档必须是"默认拒绝"—— 每条性质各一个用例，各有一个能红的靶子。

2026-10-05 验收会定性的 RCE 链是两句话：**登记任意绝对路径命令** → **立刻 invoke 把它跑起来**。
当时挡住这条链的只有两个东西，而两个都是坏的：`allow_origins=["*"]` 让任何网页能发这两个请求，
`WORKFLOWHUB_TOKEN` 没配就等于不校验。这一组用例钉的是**修好之后的默认值**：

* 口令必配：没有环境变量就首次启动自己生成一把，落盘只给本人读，本机调用方从发现文件里拿到它；
* `approval_mode=True`：高风险工具先出审批单；
* `high_risk_enabled=False`：扫描到的/手动登记的高风险命令默认**根本不可调用**；
* 状态变更路由上**一个 `Access-Control-Allow-Origin` 都不许有**（`*` 更不行）；
* Host 只与字面量回环名单比，**永不与请求自己带来的 Origin 比**（DNS rebinding 的成因）；
* 于是那条链在跨源那一侧一步都走不通，而本机的正当调用方（界面 / 桌面壳 / Agent / MCP）照样走得通。

每条都配"合法路径仍然走得通"的对照：只测拒绝会把界面自己也一起关掉，那不算修好。
"""
from __future__ import annotations

import json
import os
import stat
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from fastapi.testclient import TestClient
from local_client import LOCAL_BASE_URL, local_client, token_of
from workflowhub.api import create_app
from workflowhub.bootstrap import build_hub
from workflowhub.catalog import DEFAULT_SETTINGS
from workflowhub.local_guard import (ENDPOINT_FILE, LOOPBACK_HOSTS, TOKEN_FILE,
                                     allowed_hosts, host_problem, publish_endpoint,
                                     retire_endpoint, token_path)

#: 网页那一侧的身份：交给 Host 判据的时候必须"字面量不是回环"
FOREIGN_HOSTS = ("evil.test:8794", "0.0.0.0:8794", "192.168.1.7:8794", "127.0.0.1.evil.test:8794",
                 "evil.127.0.0.1:8794", "localhost.evil.test:8794", "127.1:8794", "127.0.1:8794")
#: 只有这三把字面量是"本机回环"，名单里不许有通配、空值或 `0.0.0.0`
EXPECTED_LOOPBACK_LITERALS = {"127.0.0.1", "localhost", "::1"}

FAKE_TOOL = """\
import sys
from pathlib import Path
(Path(__file__).parent / "RAN").write_text(" ".join(sys.argv[1:]), encoding="utf-8")
print("ran")
"""


def tokens_in_discovery_files(hub) -> list[tuple[str, str]]:
    """从"本机调用方实际会读的那几个文件"里取口令：(路径, 读到的值)，读不到写空串。"""
    data = Path(hub.roots["data"])
    rows: list[tuple[str, str]] = []
    for path in (data / "desktop.json", data / ENDPOINT_FILE,
                 Path(hub.roots["project"]) / "agent" / ".endpoint"):
        if not path.is_file():
            rows.append((str(path), ""))
            continue
        raw = json.loads(path.read_text(encoding="utf-8"))
        rows.append((str(path), str((raw.get("auth") or {}).get("token") or raw.get("token") or "")))
    return rows


class DefaultDenyFixture(unittest.TestCase):
    """每条性质一个 fresh hub：默认值被上一条用例改过就不算"交付出去的那一档"。"""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        self.tool_path = self.root / "fake_tool.py"
        self.tool_path.write_text(FAKE_TOOL, encoding="utf-8")
        self.marker = self.root / "RAN"
        os.environ.pop("WORKFLOWHUB_TOKEN", None)       # 交付默认：运维没发牌，由服务自己生成
        os.environ.pop("WORKFLOWHUB_ALLOWED_HOSTS", None)
        os.environ.pop("WORKFLOWHUB_MAX_BODY_BYTES", None)
        self.hub = build_hub(self.root / "data")
        # 发布发现文件会写 `agent/.endpoint`，指向仓库根的那一份不许被测试动到
        self.hub.roots["project"] = self.root
        (self.root / "agent").mkdir(exist_ok=True)

    def tearDown(self):
        retire_endpoint(self.hub)

    # ── 夹具 ────────────────────────────────────────────────────────────────────
    def fresh_app(self):
        return create_app(self.hub)

    def app_client(self):
        """带口令的回环客户端，但**不动那两个默认开关**（keep_defaults），
        这样"交付出去的那一档"在用例里就是它本来的样子。"""
        return local_client(self.hub, app=self.fresh_app(), keep_defaults=True)

    def register(self, client, name: str, risk: str = "high") -> str:
        registered = client.post("/api/v1/tools", json={
            "name": name, "command": [sys.executable, str(self.tool_path)], "risk": risk})
        self.assertEqual(registered.status_code, 200, registered.text)
        return registered.json()["key"]

    def unlock_high_risk(self, *, approval: bool | None = None) -> None:
        """替用户走那一格开关（`approval=None` 表示审批那格保持交付默认不动）。"""
        patch = {"high_risk_enabled": True}
        if approval is not None:
            patch["approval_mode"] = approval
        self.hub.catalog.update_settings(patch)
        self.hub.registry.refresh()


class TokenIsMandatoryTests(DefaultDenyFixture):
    """判据 (d)：口令必配、自动生成、只给本人读、本机调用方从发现文件里拿得到。"""

    def test_token_is_auto_generated_mandatory_and_owner_only_on_disk(self):
        app = self.fresh_app()
        token = token_of(app)
        self.assertTrue(token, "默认必须有一把口令，空口令等于这道闸门不存在")
        self.assertGreaterEqual(len(token), 32, "短到能被猜到的口令等于没有")
        path = token_path(self.hub.roots["data"])
        self.assertEqual(Path(path).name, TOKEN_FILE)
        self.assertTrue(path.is_file(), "口令没落盘：重启就换一把，所有本机调用方一起失效")
        self.assertEqual(json.loads(path.read_text(encoding="utf-8"))["token"], token)
        mode = stat.S_IMODE(path.stat().st_mode)
        self.assertTrue(mode & stat.S_IRUSR and mode & stat.S_IWUSR, "本人读写位必须在")
        if os.name == "posix":
            self.assertEqual(mode, 0o600, "POSIX 上 0600 是货真价实的位掩码")
        else:
            # Windows 的 chmod 只能设/清只读位（`local_guard._restrict` 的文档就明写了
            # 不把这一半说成做到了），所以这里按**平台真能表达的那道边界**判：
            # NTFS 的 ACE 里不许出现"人人可读"那一类主体。读不到 ACL 就判失败，不判通过。
            acl = subprocess.run(["icacls", str(path)], capture_output=True)
            self.assertEqual(acl.returncode, 0, "读不到 ACL 就等于这条性质没被验过，不许当通过")
            text = acl.stdout.decode("gbk", errors="replace").lower()
            for principal in ("everyone", "每个人", "builtin\\users", "authenticated users",
                              "已登录用户", "domain users", "域用户", "guests", "来宾", "anonymous"):
                self.assertNotIn(principal, text, f"口令文件对 {principal} 可读：{text[:300]}")

        # 必配：不带口令的非 GET 一律 401，哪怕 Host 是合法回环
        plain = TestClient(create_app(self.hub), base_url=LOCAL_BASE_URL)
        refused = plain.post("/api/v1/system/quit")
        self.assertEqual(refused.status_code, 401, "没配环境变量时口令就成了装饰")
        self.assertFalse((self.hub.roots["data"] / "desktop.stop").exists(),
                         "被拒的 quit 还是把停止标记写下去了")
        # 而它绝不出现在任何响应**体**里（交给界面的那一份在响应头，由另一条用例钉 HttpOnly）
        for url in ("/api/v1/agent/info", "/api/v1/overview", "/health", "/api/v1/tools",
                    "/api/v1/settings"):
            self.assertNotIn(token, plain.get(url).text, f"{url} 把口令写进了响应体")
        self.assertNotIn(token, refused.text, "401 的响应体把口令交代了")

    def test_local_clients_get_the_same_token_from_the_discovery_files(self):
        """桌面壳 / Agent / MCP 桥读的是发现文件，不是环境变量 —— 那把口令必须真能用。"""
        app = self.fresh_app()
        token = token_of(app)
        self.hub.token = token
        publish_endpoint(self.hub, port=8794, url="http://127.0.0.1:8794/", pid=os.getpid())
        rows = tokens_in_discovery_files(self.hub)
        self.assertEqual(len(rows), 3, f"三处发现文件少写了一处：{rows}")
        for name, value in rows:
            self.assertEqual(value, token, f"{name} 里的口令与服务用的不是同一把")
            self.assertTrue(Path(name).is_file())
            # 权限也得跟着收紧：发现文件里同样是这把口令
            self.assertTrue(stat.S_IMODE(Path(name).stat().st_mode) & stat.S_IRUSR)
        # 拿"只读了那个文件"的凭据去打一次真实的非 GET
        for name, value in rows:
            client = TestClient(app, base_url=LOCAL_BASE_URL,
                                headers={"X-WorkflowHub-Token": value})
            answer = client.post("/api/v1/window/action", json={"action": "focus"})
            self.assertNotEqual(answer.status_code, 401, f"{name} 里那把口令驱动不了写入：{answer.text}")
        # 而目录不在时不为此造目录（打包出去的目录里就没有 `agent/`）
        retire_endpoint(self.hub)
        (self.root / "agent").rmdir()
        publish_endpoint(self.hub, port=8794, url="http://127.0.0.1:8794/", pid=os.getpid())
        self.assertFalse((self.root / "agent").exists(), "为了写发现文件凭空造了一个 agent/")

    def test_the_ui_cookie_is_httponly_strict_and_never_revealed_in_a_body(self):
        """界面没有地方填口令：服务把口令交给它，交的方式必须只给这一个浏览器会话。"""
        app = self.fresh_app()
        client = TestClient(app, base_url=LOCAL_BASE_URL)     # 不带任何默认头：模拟浏览器
        page = client.get("/index.html")
        self.assertEqual(page.status_code, 200, page.text[:200])
        set_cookie = page.headers.get("set-cookie", "")
        self.assertIn("workflowhub_session=", set_cookie, f"没把口令交给界面：{dict(page.headers)}")
        self.assertIn(token_of(app), set_cookie, "Cookie 里带的不是那把共享口令")
        self.assertIn("httponly", set_cookie.lower(), "页面脚本能读到口令")
        self.assertIn("samesite=strict", set_cookie.lower(), "别的站发起的请求会把它带上")
        self.assertNotIn(token_of(app), page.text, "口令出现在页面正文里")
        acted = client.post("/api/v1/window/action", json={"action": "focus"},
                            headers={"Origin": LOCAL_BASE_URL})
        self.assertNotEqual(acted.status_code, 401, f"只带 Cookie 的同源请求被挡住了：{acted.text}")
        # 同机另一个端口的页面白骑这个 Cookie：必须挡住（Cookie 不按端口共享）
        ridden = client.post("/api/v1/window/action", json={"action": "focus"},
                             headers={"Origin": "http://127.0.0.1:9999"})
        self.assertIn(ridden.status_code, (401, 403), "别的本地端口白骑了这个 Cookie")


class DeliveredDefaultsTests(DefaultDenyFixture):
    """交付出去的那两个默认值：审批要开、高风险要关，而且默认档下命令真没跑起来。"""

    def test_approval_mode_is_on_by_default_and_gates_the_command(self):
        self.assertTrue(DEFAULT_SETTINGS["approval_mode"], "代码里的交付默认被改掉了")
        client = self.app_client()
        self.assertTrue(self.hub.catalog.settings["approval_mode"], "新鲜 hub 的默认档不是 True")
        key = self.register(client, "审批默认靶子")
        # 只放开用户那一格 high_risk_enabled；approval_mode 保持交付默认不动
        self.unlock_high_risk()
        self.assertTrue(self.hub.catalog.settings["approval_mode"], "上一步把审批默认顺手改掉了")
        pending = client.post(f"/api/v1/tools/{key}/invoke", json={"inputs": {"argv": []}})
        self.assertEqual(pending.status_code, 200, pending.text)
        self.assertEqual(pending.json()["status"], "awaiting_approval", pending.text[:400])
        self.assertFalse(self.marker.exists(), "还没点头命令就已经跑了 —— 那不是审批，是通知")
        approved = client.post(f"/api/v1/approvals/{pending.json()['approval']['id']}/decide",
                               json={"approve": True})
        self.assertEqual(approved.status_code, 200, approved.text)
        ran = client.post(f"/api/v1/tools/{key}/invoke",
                          json={"inputs": {"argv": [], "approved": True}})
        self.assertEqual(ran.json()["status"], "succeeded", ran.text[:400])
        self.assertTrue(self.marker.exists(), "批准之后还是没跑，那这条用例什么都没证")

    def test_high_risk_is_off_by_default_so_a_registered_command_cannot_run(self):
        self.assertFalse(DEFAULT_SETTINGS["high_risk_enabled"], "代码里的交付默认被改掉了")
        client = self.app_client()
        self.assertFalse(self.hub.catalog.settings["high_risk_enabled"], "新鲜 hub 的默认档不是 False")
        key = self.register(client, "默认档下的绝对路径命令")
        self.assertFalse(self.hub.catalog.is_enabled(key), "默认档下它竟然是可调用的")
        refused = client.post(f"/api/v1/tools/{key}/invoke", json={"inputs": {"argv": []}})
        self.assertEqual(refused.status_code, 422, refused.text)
        self.assertEqual(refused.json()["error"]["code"], "permission_error")
        self.assertFalse(self.marker.exists(), "被拒的调用还是把进程起起来了")
        # 对照：用户真去设置里打开那一格之后，同一条路走得通（默认不是永久锁死）
        self.unlock_high_risk(approval=False)
        ran = client.post(f"/api/v1/tools/{key}/invoke", json={"inputs": {"argv": []}})
        self.assertEqual(ran.json()["status"], "succeeded", ran.text[:400])
        self.assertTrue(self.marker.exists(), "打开开关后仍然跑不起来，那是锁死不是默认关")


class CorsOnMutatingRoutesTests(DefaultDenyFixture):
    """判据 (e)：会改东西的路由上，跨域放行头一个都不许有。"""

    def test_no_access_control_allow_origin_on_any_mutating_route(self):
        app = self.fresh_app()
        client = local_client(self.hub, app=app, keep_defaults=True)
        self.assertFalse(any("CORSMiddleware" in repr(m) for m in app.user_middleware),
                         f"CORS 中间件还挂在栈上：{app.user_middleware}")
        shape = {"name": "x", "command": ["C:\\x\\y.exe"]}
        attempts = [
            ("post", "/api/v1/tools", shape, {"Origin": "http://evil.test"}),
            ("post", "/api/v1/tools", shape, {}),
            ("post", "/api/v1/tools/not-here/invoke", {"inputs": {}}, {"Origin": "http://evil.test"}),
            ("post", "/api/v1/tools/not-here/invoke", {"inputs": {}}, {}),
            ("put", "/api/v1/settings", {"patch": {"high_risk_enabled": True}}, {}),
            ("patch", "/api/v1/tools/not-here/state", {"enabled": True}, {"Origin": "http://evil.test"}),
            ("delete", "/api/v1/tools/not-here", None, {}),
            ("post", "/api/v1/system/quit", None, {"Origin": "http://evil.test"}),
            ("post", "/api/v1/approvals/nope/decide", {"approve": True}, {}),
            ("options", "/api/v1/tools", None, {"Origin": "http://evil.test"}),
        ]
        for verb, path, body, headers in attempts:
            if verb == "options":
                response = client.options(path, headers=headers)
            elif body is None:
                response = getattr(client, verb)(path, headers=headers)
            else:
                response = getattr(client, verb)(path, json=body, headers=headers)
            allow = response.headers.get("access-control-allow-origin")
            self.assertIsNone(allow, f"{verb.upper()} {path} 送了 ACAO 头：{allow}")
            self.assertNotEqual(allow, "*", f"{verb.upper()} {path} 还在送 ACAO:*")
            self.assertIsNone(response.headers.get("access-control-allow-credentials"),
                              f"{verb.upper()} {path} 把带凭据跨源的门开了一点缝")
        # 界面自己是同源的那条路必须仍然走得通（否则这不是修好，是关掉）
        page = client.get("/index.html")
        self.assertEqual(page.status_code, 200, page.text[:200])
        self.assertIsNone(page.headers.get("access-control-allow-origin"))


class HostIsNeverTheRequestsOwnOriginTests(DefaultDenyFixture):
    """判据 (a)(b)：Host 只与字面量回环名单比；拿 Origin 去和请求自己的 Host 比才是那个 bug。"""

    def test_only_the_three_loopback_literals_are_allowed_hosts(self):
        self.assertEqual(set(LOOPBACK_HOSTS), EXPECTED_LOOPBACK_LITERALS,
                         "回环名单被改动了 —— 加一个就等于给外面开一个门")
        self.assertEqual(set(allowed_hosts()), EXPECTED_LOOPBACK_LITERALS,
                         "没有 WORKFLOWHUB_ALLOWED_HOSTS 时不许有任何额外主机")
        self.assertNotIn("*", {host.lower() for host in allowed_hosts()}, "名单里出现通配就是全开")
        for foreign in FOREIGN_HOSTS:
            self.assertTrue(host_problem(foreign, bound_port=8794),
                            f"{foreign} 竟然被当作本机 —— 名单必须只认字面量")
        for literal in ("127.0.0.1:8794", "localhost:8794", "[::1]:8794"):
            self.assertEqual(host_problem(literal, bound_port=8794), "", f"{literal} 应当是合法回环")
        self.assertTrue(host_problem("127.0.0.1:1234", bound_port=8794),
                        "端口不是本服务真实监听的那个也要拒")
        # 唯一的加白出口是显式环境变量，这条也得成立（否则反向代理没法用）
        os.environ["WORKFLOWHUB_ALLOWED_HOSTS"] = "hub.internal:8794"
        try:
            self.assertEqual(host_problem("hub.internal:8794", bound_port=8794), "",
                             "显式加白的主机名应当被认下来")
            self.assertTrue(host_problem("evil.test:8794", bound_port=8794),
                            "加白一个不等于全开")
        finally:
            del os.environ["WORKFLOWHUB_ALLOWED_HOSTS"]

    def test_the_host_verdict_does_not_depend_on_the_requests_own_origin(self):
        """同一条伪造 Host，带 Origin、不带 Origin、Origin 与 Host 逐字相等 —— 结论都得是拒。"""
        client = self.app_client()
        token = token_of(client.app)
        key = self.register(client, "伪造 Host 靶子")
        shapes = [
            {"Host": "evil.test:8794", "Origin": "http://evil.test:8794"},        # 自洽的 rebinding
            {"Host": "evil.test:8794"},                                            # 只伪 Host
            {"Host": "evil.test:8794", "Origin": LOCAL_BASE_URL},                  # Host 假、Origin 真
            {"Host": "evil.test:8794", "Referer": "http://evil.test:8794/page"},
        ]
        self.unlock_high_risk(approval=False)
        for headers in shapes:
            refused = client.post(f"/api/v1/tools/{key}/invoke", json={"inputs": {"argv": []}},
                                  headers={**headers, "X-WorkflowHub-Token": token})
            self.assertEqual(refused.status_code, 403,
                             f"伪造 Host 的调用竟然进来了（{headers.get('Host')} + "
                             f"{headers.get('Origin') or headers.get('Referer') or '无 Origin'}）")
            self.assertEqual(refused.json()["error"]["code"], "permission_error")
            self.assertIn("回环", refused.json()["error"]["message"])
        self.assertFalse(self.marker.exists(), "被拒的请求还是把命令跑起来了")
        # 对照：合法回环 Host 同一条调用走得通（不是"一律拒"）
        ok = client.post(f"/api/v1/tools/{key}/invoke", json={"inputs": {"argv": []}})
        self.assertEqual(ok.status_code, 200, ok.text[:300])
        self.assertTrue(self.marker.exists(), "合法回环 + 口令那条路自己走不通")


class RceChainCrossOriginTests(DefaultDenyFixture):
    """那条链的两步（登记任意绝对命令 → invoke）在跨源那一侧一步都走不通，quit 同理。"""

    def test_register_then_quit_and_settings_cannot_be_driven_from_another_origin(self):
        # 网页拿不到那把口令（读不到磁盘上的发现文件，也读不到别的站的 HttpOnly Cookie），
        # 所以攻击那一侧按"不带口令"来发才贴近现实；带口令的那一种另列一条，看它是不是也被拒。
        browser = TestClient(self.fresh_app(), base_url=LOCAL_BASE_URL)
        token = token_of(browser.app)
        before = set(self.hub.catalog.tools)
        lure = {"name": "网页塞进来的", "risk": "medium",
                "command": [sys.executable, str(self.tool_path)]}
        attempts = [
            # 浏览器里 evil.test 的页面：Host 由浏览器写成真的 127.0.0.1:8794，Origin 暴露了它自己
            ("登记", "/api/v1/tools", lure, {"Origin": "http://evil.test"}),
            ("登记（带端口）", "/api/v1/tools", lure, {"Origin": "http://evil.test:8794"}),
            ("退出服务", "/api/v1/system/quit", None, {"Origin": "http://evil.test"}),
            ("放开高风险", "/api/v1/settings", {"patch": {"high_risk_enabled": True}},
             {"Origin": "http://evil.test"}),
            ("关掉审批", "/api/v1/settings", {"patch": {"approval_mode": False}},
             {"Referer": "http://evil.test/page"}),
        ]
        for label, path, body, headers in attempts:
            response = browser.post(path, json=body, headers=headers) if body is not None \
                else browser.post(path, headers=headers)
            self.assertEqual(response.status_code, 403,
                             f"{label} 竟然被跨源驱动了：{response.text[:300]}")
            self.assertEqual(response.json()["error"]["code"], "permission_error")
            self.assertIsNone(response.headers.get("access-control-allow-origin"),
                              f"{label} 被拒的同时还把读权限送了出去")
        # 就算它不知从哪儿摸到了口令，跨源这一条也照样是 403：两道判据互为兜底，不是二选一
        smuggled = browser.post("/api/v1/tools", json=lure,
                               headers={"Origin": "http://evil.test",
                                        "X-WorkflowHub-Token": token})
        self.assertEqual(smuggled.status_code, 403, smuggled.text[:300])
        self.assertEqual(set(self.hub.catalog.tools), before, "被拒的请求还是登记了一条命令")
        self.assertFalse(self.marker.exists(), "被拒的请求还是把命令跑起来了")
        self.assertFalse((self.hub.roots["data"] / "desktop.stop").exists(),
                         "跨源的 quit 真的把服务停了")
        self.assertIs(self.hub.catalog.settings["high_risk_enabled"], False, "跨源的设置改写生效了")
        self.assertIs(self.hub.catalog.settings["approval_mode"], True, "跨源把审批关掉生效了")
        # 连"invoke 一把已经存在的工具"也走不通：链条第二步同样要同源
        existing = next(k for k, t in self.hub.catalog.tools.items() if t.get("risk") != "high")
        invoked = browser.post(f"/api/v1/tools/{existing}/invoke", json={"inputs": {"dry_run": True}},
                               headers={"Origin": "http://evil.test"})
        self.assertEqual(invoked.status_code, 403, invoked.text[:300])

    def test_a_local_caller_with_the_token_still_drives_the_same_two_steps(self):
        """反面要有对照：本机的正当调用方（读得到口令的那一个）两步都走得通。"""
        client = self.app_client()
        self.unlock_high_risk(approval=False)
        key = self.register(client, "本机 CLI 登记的靶子")
        invoked = client.post(f"/api/v1/tools/{key}/invoke", json={"inputs": {"argv": []}})
        self.assertEqual(invoked.status_code, 200, invoked.text[:400])
        self.assertEqual(invoked.json()["status"], "succeeded", invoked.text[:400])
        self.assertTrue(self.marker.exists(), "登记→调用这条路自己走不通")


if __name__ == "__main__":
    unittest.main()
