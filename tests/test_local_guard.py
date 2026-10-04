"""本机边界（共享验收标准 a–e）—— 这一组就是"任何网页都能驱动这台机器"那条 BLOCKER 的反面。

判据一条条对上：
(a) 只绑 127.0.0.1；
(b) Host 必须是字面量回环 + 实际端口 —— 拿 Origin 和请求自己的 Host 比才是那个 bug；
(c) Origin/Referer 不同源就拒，并且返回 JSON 错误体；
(d) 非 GET 要共享口令（timing-safe），口令首次启动自动生成并落盘；
(e) 状态变更路由绝不送 `Access-Control-Allow-Origin: *`。

每条都配了"合法路径同时仍然走得通"的对照 —— 只测拒绝不测放行，会把界面自己也一起关掉。
桌面端与界面自己走的就是那条路：回环 Host + 服务发给它的 Cookie。
"""
from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from fastapi.testclient import TestClient

from local_client import LOCAL_BASE_URL, local_client, token_of
from test_discovery import fixtures
from workflowhub.api import create_app
from workflowhub.bootstrap import build_hub
from workflowhub.catalog import DEFAULT_SETTINGS
from workflowhub.local_guard import (TOKEN_FILE, ensure_token, host_problem, origin_problem,
                                     require_within, split_host, token_matches)
from workflowhub.errors import WorkflowHubError


def raw_client(hub, base_url: str = LOCAL_BASE_URL) -> TestClient:
    """**不带**任何默认头的回环客户端：用来构造"缺口令/错 Host"这些反面形状。"""
    return TestClient(create_app(hub), base_url=base_url)


class FakeWindow:
    """桌面窗口控制器的替身：`capture()` 只有桌面模式才存在，越界测试需要它存在。"""

    def __init__(self):
        self.called: list[Path] = []

    def capture(self, path):
        self.called.append(Path(path))
        Path(path).write_bytes(b"PNG")
        return True

    def state(self):
        return {"hwnd": 1}


class GuardUnitTests(unittest.TestCase):
    """Host/Origin 那两把尺本身：不依赖 HTTP，形状判错就是在生产上判错。"""

    def test_split_host_handles_ipv6_and_bare_names(self):
        self.assertEqual(split_host("[::1]:8794"), ("::1", 8794))
        self.assertEqual(split_host("127.0.0.1:8794"), ("127.0.0.1", 8794))
        self.assertEqual(split_host("localhost"), ("localhost", None))
        self.assertEqual(split_host("evil.test:8794"), ("evil.test", 8794))

    def test_rebinding_host_is_refused_even_when_origin_matches_it(self):
        # 这正是被修掉的那个 bug 的形状：页面把 Host 写成它自己的域名，
        # "Origin 等于 Host"于是成立 —— 判据必须看 Host 的字面量，而不是两者是否相等。
        problem = host_problem("evil.test:8794", bound_port=8794)
        self.assertTrue(problem, "Host 判据必须独立于 Origin")
        self.assertIn("回环", problem)
        # 连"Origin 与 Host 相等"这一条成立时也不能放行：两个都是外来域名而已
        self.assertTrue(origin_problem("http://evil.test:8794", "evil.test:8794"))
        self.assertEqual(host_problem("127.0.0.1:8794", bound_port=8794), "")
        self.assertEqual(host_problem("localhost:8794", bound_port=8794), "")
        self.assertEqual(host_problem("[::1]:8794", bound_port=8794), "")
        self.assertEqual(host_problem("127.0.0.1:9999", bound_port=8794),
                         "Host 端口 9999 不是本服务监听的端口 8794")
        self.assertIn("回环", host_problem("0.0.0.0:8794", bound_port=8794))
        self.assertIn("Host", host_problem("", bound_port=8794))

    def test_origin_must_equal_the_loopback_host_port(self):
        self.assertEqual(origin_problem("http://127.0.0.1:8794", "127.0.0.1:8794"), "")
        self.assertTrue(origin_problem("http://evil.test:8794", "evil.test:8794"),
                        "Origin 与 Host 相等不等于安全：两个都可能是外来域名")
        self.assertTrue(origin_problem("http://127.0.0.1:9999", "127.0.0.1:8794"))
        self.assertTrue(origin_problem("http://localhost:8794", "127.0.0.1:8794"))
        self.assertEqual(origin_problem("", "127.0.0.1:8794"), "", "不送 Origin 的命令行调用方由口令那条判")

    def test_token_compare_is_timing_safe_and_empty_token_is_open(self):
        self.assertTrue(token_matches("abc", "abc"))
        self.assertFalse(token_matches("abd", "abc"))
        self.assertFalse(token_matches("", "abc"))

    def test_require_within_blocks_traversal_and_allows_inside(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            inside = root / "a" / "x.png"
            inside.parent.mkdir(parents=True)
            inside.write_text("x", encoding="utf-8")
            self.assertEqual(require_within(str(inside), [root]), inside.resolve())
            for escaping in (str(root / ".." / "evil.png"), str(Path(folder).parent / "outside.png")):
                with self.assertRaises(WorkflowHubError) as caught:
                    require_within(escaping, [root])
                self.assertEqual(caught.exception.detail.code.value, "permission_error")


class TokenBootstrapTests(unittest.TestCase):
    def test_first_start_generates_and_persists_the_token(self):
        with tempfile.TemporaryDirectory() as folder:
            data = Path(folder) / "data"
            data.mkdir(parents=True)
            token, source = ensure_token(data)
            self.assertGreaterEqual(len(token), 32, "口令短到能被猜到就等于没有")
            self.assertEqual(Path(source).name, TOKEN_FILE)
            self.assertTrue((data / TOKEN_FILE).is_file())
            again, _ = ensure_token(data)
            self.assertEqual(again, token, "第二次启动换了口令，之前所有调用方都白配了")
            disk = json.loads((data / TOKEN_FILE).read_text(encoding="utf-8"))
            self.assertEqual(disk["token"], token)
            self.assertNotIn("secret", json.dumps(disk).lower())

    def test_environment_variable_wins_and_nothing_is_written(self):
        import os

        with tempfile.TemporaryDirectory() as folder:
            data = Path(folder) / "data"
            data.mkdir(parents=True)
            os.environ["WORKFLOWHUB_TOKEN"] = "operator-issued-token"
            try:
                token, source = ensure_token(data)
                self.assertEqual(token, "operator-issued-token")
                self.assertEqual(source, "env:WORKFLOWHUB_TOKEN")
                self.assertFalse((data / TOKEN_FILE).exists(), "运维发牌时不该顺手落一份副本")
            finally:
                del os.environ["WORKFLOWHUB_TOKEN"]


class HttpBoundaryTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls._tmp = tempfile.TemporaryDirectory()
        cls.root = Path(cls._tmp.name)
        cls.machines = cls.root / "machine"
        cls.machines.mkdir()
        fixtures(cls.machines)
        cls.hub = build_hub(cls.root / "data")
        cls.hub.catalog.scan([str(cls.machines)])
        cls.hub.registry.refresh()
        cls.client = local_client(cls.hub, keep_defaults=True)   # 交付默认那一档，别先解锁
        cls.app = cls.client.app
        cls.token = token_of(cls.app)

    @classmethod
    def tearDownClass(cls):
        cls._tmp.cleanup()

    # ── (b) Host 判据 ──────────────────────────────────────────────────────────
    def test_rebound_host_is_refused_on_get_and_post(self):
        for method, path, body in (("GET", "/api/v1/tools", None),
                                   ("POST", "/api/v1/tools", {"name": "x", "command": ["C:\\x\\y.exe"]}),
                                   ("GET", "/health", None),
                                   ("GET", "/index.html", None)):
            # 用 `client.request(verb, …)` 而不是 `getattr(client, verb)(…, json=…)`：
            # 后者对 GET 根本不存在 json 这个参数（httpx 的 get() 不接），一条从没跑过的
            # 测试语句而已 —— 判据要落在同一条路径上，方法名不该替它说话。
            response = self.client.request(method, path, json=body,
                                           headers={"Host": "evil.test:8794",
                                                    "Origin": "http://evil.test:8794",
                                                    "X-WorkflowHub-Token": self.token})
            self.assertEqual(response.status_code, 403, f"{method.upper()} {path} 竟然接受了伪造 Host")
            self.assertEqual(response.headers["content-type"].split(";")[0], "application/json",
                             "拒绝要返 JSON 错误体（标准第 (c) 条后半）")
            self.assertEqual(response.json()["error"]["code"], "permission_error")
            self.assertIn("回环", response.json()["error"]["message"])

    def test_localhost_and_ipv6_loopback_hosts_are_accepted(self):
        # `http://[::1]:8794` 这种 base_url 在本机的 starlette TestClient 上会先炸在
        # 它自己的 netloc 解析上（`netloc.split(":", 1)` 把 `[::1]` 切坏，ValueError），
        # 与被测判据无关。真实 uvicorn 收到的就是 Host 头那一个字面量，所以这里按 Host 头喂：
        # 断言没变 —— IPv6 回环仍然必须被当作本机放行。
        for label, base_url, headers in (
                ("localhost", "http://localhost:8794", {}),
                ("[::1]", LOCAL_BASE_URL, {"Host": "[::1]:8794"}),
                ("127.0.0.1", LOCAL_BASE_URL, {})):
            client = TestClient(self.app, base_url=base_url, headers=headers)
            self.assertEqual(client.get("/api/v1/tools").status_code, 200,
                             f"{label} 应当被当作本机：{headers or base_url}")

    def test_host_without_the_listening_port_is_refused(self):
        # 桌面端每次双击都是随机端口：判据取的是 ASGI scope 里那个**真实**端口
        response = self.client.get("/api/v1/tools", headers={"Host": "127.0.0.1"})
        self.assertEqual(response.status_code, 403)
        self.assertIn("端口", response.json()["error"]["message"])

    # ── (c) Origin / Referer 判据 ─────────────────────────────────────────────
    def test_foreign_origin_on_register_tool_is_rejected_and_nothing_is_registered(self):
        before = set(self.hub.catalog.tools)
        response = self.client.post("/api/v1/tools",
                                    json={"name": "从网页里塞进来的", "command": [str(Path(__file__))],
                                          "risk": "medium"},
                                    headers={"Origin": "http://evil.test",
                                             "X-WorkflowHub-Token": self.token})
        self.assertEqual(response.status_code, 403)
        self.assertEqual(response.json()["error"]["code"], "permission_error")
        self.assertEqual(set(self.hub.catalog.tools), before, "被拒的请求还是把工具登记进去了")

    def test_referer_is_judged_too_and_the_legit_same_origin_still_works(self):
        bad = self.client.post("/api/v1/tools", json={"name": "x", "command": ["C:\\x\\y.exe"]},
                               headers={"Referer": "http://evil.test/page",
                                        "X-WorkflowHub-Token": self.token})
        self.assertEqual(bad.status_code, 403)
        good = self.client.post("/api/v1/tools", json={"name": "同源登记的",
                                                       "command": ["C:\\Windows\\System32\\cmd.exe"]},
                                headers={"Origin": "http://127.0.0.1:8794",
                                         "X-WorkflowHub-Token": self.token})
        self.assertEqual(good.status_code, 200, f"合法的回环+口令那条路被挡住了：{good.json()}")

    # ── (d) 共享口令 ──────────────────────────────────────────────────────────
    def test_register_then_invoke_without_token_is_refused(self):
        """BLOCKER 1 的正身：登记一条命令再跑它，全程不带口令就必须一步都走不通。"""
        # 必须用 raw_client：`self.client` 是 local_client 造的那个**已经带着口令默认头**的
        # 客户端，拿它当"无口令"客户端等于自己给自己发牌 —— 之前这里量到的是 200。
        anonymous = raw_client(self.hub)
        registered = anonymous.post("/api/v1/tools",
                                    json={"name": "无口令登记", "command": ["C:\\Windows\\System32\\cmd.exe"]})
        self.assertEqual(registered.status_code, 401, "缺口令的 POST 竟然登记成功了")
        self.assertEqual(registered.json()["error"]["code"], "permission_error")
        self.assertNotIn("id", registered.json(), "401 的响应体不该带出一条新登记的记录")

        # 已经存在的工具也一样：拿夹具里那条低风险的 note-writer 来试 invoke
        key = next(k for k, t in self.hub.catalog.tools.items() if t.get("risk") != "high")
        invoke = anonymous.post(f"/api/v1/tools/{key}/invoke", json={"inputs": {"dry_run": True}})
        self.assertEqual(invoke.status_code, 401)
        with_token = self.client.post(f"/api/v1/tools/{key}/invoke", json={"inputs": {"dry_run": True}})
        self.assertEqual(with_token.status_code, 200, "带对口令就是同一条路，必须走得通")

    def test_wrong_token_is_refused_even_over_a_loopback_host(self):
        response = self.client.post("/api/v1/system/quit", headers={"X-WorkflowHub-Token": "guessed"})
        self.assertEqual(response.status_code, 401)

    def test_reads_do_not_need_a_token_so_discovery_stays_zero_config(self):
        self.assertEqual(self.client.get("/api/v1/agent/info").status_code, 200)
        self.assertEqual(self.client.get("/api/v1/tools").status_code, 200)
        info = self.client.get("/api/v1/agent/info").json()
        self.assertTrue(info["auth"]["required"], "agent/info 必须说实话：口令是必配的")

    def test_the_ui_path_works_with_only_the_cookie_the_server_itself_set(self):
        """界面（浏览器里的页面）没有地方填口令：口令由服务在回环请求上发给它。"""
        client = raw_client(self.hub)
        page = client.get("/index.html")
        self.assertIn(client.cookies.get("workflowhub_session", ""), (str(self.token), "", None))
        cookie = client.cookies.get("workflowhub_session")
        self.assertEqual(cookie, self.token, "Cookie 里带的就是那把共享口令")
        with_cookie = client.post("/api/v1/tools/not-here/invoke", json={"inputs": {}},
                                  headers={"Origin": "http://127.0.0.1:8794"})
        self.assertNotEqual(with_cookie.status_code, 401, "只带 Cookie 的同源请求应当过口令这一关")
        # 同一台机器上另一个端口的页面：Cookie 按域名共享而不按端口共享 —— 这条要挡住
        ridden = client.post("/api/v1/tools/not-here/invoke", json={"inputs": {}},
                             headers={"Origin": "http://127.0.0.1:9999"})
        self.assertIn(ridden.status_code, (401, 403), "别的本地端口白骑这个 Cookie 就是旁路")

    # ── (e) CORS ──────────────────────────────────────────────────────────────
    def test_no_wildcard_cors_header_anywhere(self):
        for method, path, body in (("GET", "/api/v1/tools", None),
                                   ("POST", "/api/v1/system/quit", None)):
            response = self.client.request(method, path, json=body,
                                           headers={"X-WorkflowHub-Token": self.token})
            allow = response.headers.get("access-control-allow-origin")
            self.assertNotEqual(allow, "*", f"{path} 还在送 ACAO:*")
        options = self.client.request("POST", "/api/v1/tools",
                                      json={"name": "x", "command": ["C:\\x.exe"]},
                                      headers={"Origin": "http://evil.test"})
        self.assertNotEqual(options.headers.get("access-control-allow-origin"), "*")

    # ── 交付默认（BLOCKER 1 的另一半） ────────────────────────────────────────
    def test_delivered_defaults_are_approval_on_and_high_risk_off(self):
        self.assertTrue(DEFAULT_SETTINGS["approval_mode"])
        self.assertFalse(DEFAULT_SETTINGS["high_risk_enabled"])
        self.assertTrue(self.hub.catalog.settings["approval_mode"])
        self.assertFalse(self.hub.catalog.settings["high_risk_enabled"])
        high = next(k for k, t in self.hub.catalog.tools.items() if t.get("risk") == "high")
        self.assertFalse(self.hub.catalog.is_enabled(high),
                         "默认档下高风险工具必须是不可调用，而不是只是界面里灰着")
        response = self.client.post(f"/api/v1/tools/{high}/invoke", json={"inputs": {}},
                                    headers={"X-WorkflowHub-Token": self.token})
        self.assertEqual(response.status_code, 422)
        self.assertEqual(response.json()["error"]["code"], "permission_error")

    # ── BLOCKER 3：/window/capture 的路径围堵 ─────────────────────────────────
    def test_capture_cannot_write_outside_the_data_dir(self):
        with tempfile.TemporaryDirectory() as folder:
            outside = Path(folder) / "startup-shell-lure.exe"
            fake = FakeWindow()
            self.app.state.window = fake
            try:
                response = self.client.get("/api/v1/window/capture", params={"path": str(outside)},
                                           headers={"X-WorkflowHub-Token": self.token})
                self.assertEqual(response.status_code, 422, "任意路径写点又回来了")
                self.assertEqual(response.json()["error"]["code"], "permission_error")
                self.assertFalse(outside.exists(), "被拒的请求还是把文件写到外面了")
                self.assertEqual(fake.called, [])
                # 放行那一半得在**同一个**桌面现场上量：以前这里先把 window 清空了才发第二条，
                # 于是拿到的是"非桌面模式"的 200，`KeyError: path` —— 围堵过头还是没过头都没判到。
                inside = self.client.get("/api/v1/window/capture",
                                         params={"path": str(self.hub.roots["data"] / "shot.png")},
                                         headers={"X-WorkflowHub-Token": self.token})
            finally:
                self.app.state.window = None
            self.assertEqual(inside.status_code, 200, f"围堵过头，data/ 里面也写不了了：{inside.json()}")
            self.assertTrue(Path(inside.json()["path"]).is_file())
            self.assertEqual(len(fake.called), 1, "窗口该只被叫去写 data/ 里那一张")

    def test_files_endpoint_still_contains_and_reports_the_same_way(self):
        outside = str(Path(self.hub.roots["project"]).parent / "Windows" / "win.ini")
        response = self.client.get("/api/v1/files", params={"path": outside})
        self.assertEqual(response.status_code, 422)
        self.assertEqual(response.json()["error"]["code"], "permission_error")

    # ── NICE 4：请求体上限 ────────────────────────────────────────────────────
    def test_oversized_body_is_refused_before_it_is_read(self):
        import os

        os.environ["WORKFLOWHUB_MAX_BODY_BYTES"] = "4096"
        try:
            app = create_app(self.hub)
            client = TestClient(app, base_url=LOCAL_BASE_URL,
                                headers={"X-WorkflowHub-Token": token_of(app)})
            # 判据是"超过上限"，那现场就得真的超过上限：原来这里发的正好是 4096 字节，
            # 等于按自己的声明放行（量到的是路由的 422，不是闸门的 413）。
            oversized = client.post("/api/v1/tools/whatever/invoke",
                                    content=b"x" * 5000,
                                    headers={"content-type": "application/json"})
            self.assertEqual(oversized.status_code, 413,
                             f"超过上限的体没被挡住：{oversized.status_code}")
            self.assertEqual(oversized.json()["error"]["details"]["max_body_bytes"], 4096)
            # 对照：闸门不是"一律拒 POST"—— 上限以内的体照样走到路由那一层。
            inside = client.post("/api/v1/tools/whatever/invoke", json={"inputs": {}})
            self.assertNotEqual(inside.status_code, 413, "上限以内的请求被同一条闸门一起挡了")
        finally:
            del os.environ["WORKFLOWHUB_MAX_BODY_BYTES"]


class EndpointDiscoveryTests(unittest.TestCase):
    """口令要写在"本机的正当调用方读得到、网页读不到"的那几个文件里。"""

    def test_publish_endpoint_writes_all_three_and_retire_cleans_them(self):
        from workflowhub.local_guard import publish_endpoint, retire_endpoint

        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            hub = build_hub(root / "data")
            hub.token = "discovered-token"
            hub.roots["project"] = root            # 让 agent/ 落在临时目录里，别碰仓库
            (root / "agent").mkdir()
            payload = publish_endpoint(hub, port=8794, url="http://127.0.0.1:8794/", pid=4242)
            self.assertEqual(payload["token"], "discovered-token")
            for name in ("data/desktop.json", "data/agent-endpoint.json", "agent/.endpoint"):
                written = root / name
                self.assertTrue(written.is_file(), f"{name} 没写：桌面端/Agent 就无法零配置连上")
                self.assertIn("discovered-token", written.read_text(encoding="utf-8"))
            retire_endpoint(hub)
            self.assertFalse((root / "data" / "desktop.json").exists())
            self.assertFalse((root / "agent" / ".endpoint").exists())

    def test_retire_only_touches_what_this_run_published(self):
        """没发布过就不许删：仓库里那份 `agent/.endpoint` 可能是别的实例写的。"""
        from workflowhub.local_guard import retire_endpoint

        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            hub = build_hub(root / "data")
            hub.roots["project"] = root
            (root / "agent").mkdir()
            other = root / "agent" / ".endpoint"
            other.write_text('{"token":"别人的实例"}', encoding="utf-8")
            retire_endpoint(hub)
            self.assertTrue(other.is_file(), "这个实例从没写过它，收尾把它删了就是替别人清场")


class CorruptFileReportingTests(unittest.TestCase):
    """NICE 4：坏文件要报出来，不能悄悄少一条。"""

    def test_unreadable_run_and_workflow_files_are_named(self):
        with tempfile.TemporaryDirectory() as folder:
            hub = build_hub(Path(folder) / "data")
            runs = hub.roots["data"] / "runs"
            runs.mkdir(parents=True, exist_ok=True)
            (runs / "bad.json").write_text("{oops", encoding="utf-8")
            workflows = hub.roots["data"] / "workflows" / "broken"
            workflows.mkdir(parents=True, exist_ok=True)
            (workflows / "1.json").write_text('{"not": "a workflow"}', encoding="utf-8")
            client = TestClient(create_app(hub), base_url=LOCAL_BASE_URL)
            runs_report = client.get("/api/v1/runs").json()
            self.assertTrue(any("bad.json" in row["path"] for row in runs_report["skipped"]),
                            runs_report)
            self.assertTrue(runs_report["skipped"][0]["reason"])
            flow_report = client.get("/api/v1/workflows").json()
            self.assertTrue(any("broken" in row["path"] for row in flow_report["skipped"]), flow_report)


if __name__ == "__main__":
    unittest.main()
