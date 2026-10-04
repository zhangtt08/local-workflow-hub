"""Second-launch and no-window fallback: the desktop app must never die on a profile lock."""

from __future__ import annotations

import json
import socket
import tempfile
import threading
import unittest
import urllib.request
from pathlib import Path

from fastapi.testclient import TestClient

from workflowhub.api import create_app
from workflowhub.bootstrap import build_hub
from workflowhub.desktop.launcher import clear_stop_marker, existing_instance, focus_instance, serve_ready, wait_for_server

from local_client import local_client


class NoopResponder(threading.Thread):
    """假装是 WorkflowHub、其实不是的本地端口：这台机器上真有这么一层会吞连接的东西。"""

    def __init__(self, port: int):
        super().__init__(daemon=True)
        self.port = port
        self.sock = socket.socket()
        self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.sock.bind(("127.0.0.1", port))
        self.sock.listen(1)

    def run(self) -> None:
        try:
            while True:
                conn, _ = self.sock.accept()
                conn.recv(65536)
                body = json.dumps({"status": "ok", "instance": "not-the-hub"}).encode()
                head = (b"HTTP/1.1 200 OK\r\ncontent-type: application/json\r\n"
                        b"content-length: " + str(len(body)).encode() + b"\r\nconnection: close\r\n\r\n")
                conn.sendall(head + body)
                conn.close()
        except OSError:
            pass

    def stop(self) -> None:
        self.sock.close()


class ReadinessTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.hub = build_hub(Path(self._tmp.name))

    def tearDown(self):
        self._tmp.cleanup()

    def _free_port(self) -> int:
        with socket.socket() as probe:
            probe.bind(("127.0.0.1", 0))
            return int(probe.getsockname()[1])

    def test_health_carries_this_instance(self):
        client = local_client(self.hub)
        self.assertEqual(client.get("/health").json()["instance"], self.hub.instance)
        self.assertNotEqual(self.hub.instance, build_hub(Path(tempfile.mkdtemp())).instance)

    def test_readiness_rejects_a_foreign_answer_on_the_same_port(self):
        port = self._free_port()
        fake = NoopResponder(port)
        fake.start()
        try:
            self.assertTrue(wait_for_server(port, "", timeout=3)[0], "不带身份时必须认为可用")
            ready, why = wait_for_server(port, self.hub.instance, timeout=3)
            self.assertFalse(ready, "冒名应答必须判为不可用")
            self.assertIn("不属于本次实例", why)
        finally:
            fake.stop()

    def test_serve_ready_actually_serves_on_windows(self):
        server, thread, port, why, _ = serve_ready(create_app(self.hub), self.hub.instance,
                                                   "127.0.0.1", attempts=4)
        self.assertIsNotNone(server, f"应当能起一个可用端口：{why}")
        try:
            opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
            with opener.open(f"http://127.0.0.1:{port}/api/health", timeout=5) as response:
                payload = json.loads(response.read().decode("utf-8"))
            self.assertEqual(payload["data"]["project"], "WorkflowHub")
            self.assertEqual(payload["data"]["tools"], len(self.hub.catalog.tools))
        finally:
            server.should_exit = True
            thread.join(timeout=5)


class SingleInstanceTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.data = Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def test_no_marker_means_no_instance(self):
        self.assertIsNone(existing_instance(self.data))

    def test_dead_marker_is_ignored_not_fatal(self):
        (self.data / "desktop.json").write_text(json.dumps({"port": 1, "pid": 1}), encoding="utf-8")
        self.assertIsNone(existing_instance(self.data))

    def test_garbage_marker_is_ignored(self):
        (self.data / "desktop.json").write_text("{not json", encoding="utf-8")
        self.assertIsNone(existing_instance(self.data))

    def test_focus_on_dead_instance_returns_false(self):
        self.assertFalse(focus_instance({"port": 1}))

    def test_quit_endpoint_writes_the_stop_marker(self):
        with tempfile.TemporaryDirectory() as folder:
            hub = build_hub(Path(folder))
            client = local_client(hub)
            marker = hub.roots["data"] / "desktop.stop"
            self.assertFalse(marker.exists())
            result = client.post("/api/v1/system/quit").json()
            self.assertEqual(result["status"], "stopping")
            self.assertTrue(marker.is_file(), "浏览器模式的退出按钮必须留下可轮询的停止标记")

    def test_stale_stop_marker_is_cleared_for_the_next_launch(self):
        (self.data / "desktop.stop").write_text("quit", encoding="utf-8")
        clear_stop_marker(self.data)
        self.assertFalse((self.data / "desktop.stop").exists(),
                         "残留标记会让下一次的浏览器兜底实例一启动就自杀")


class EntryFlagsTests(unittest.TestCase):
    """exe 转出去的开关必须真的存在对应参数，否则故障提示就成了假出口。"""

    def _entry(self):
        import importlib.util

        path = Path(__file__).resolve().parents[1] / "scripts" / "desktop_entry.py"
        spec = importlib.util.spec_from_file_location("desktop_entry", path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module

    def test_flags_map_onto_run_desktop_parameters(self):
        import inspect

        entry = self._entry()
        from workflowhub.desktop.launcher import run_desktop

        params = set(inspect.signature(run_desktop).parameters)
        for argv in (["--port", "53121"], ["--headless-check"], ["--browser", "msedge.exe", "--start", "windowed"],
                     ["--junk", "x", "--port", "abc"]):
            flags = entry._desktop_flags(argv)
            self.assertTrue(set(flags) <= params, f"{argv} → {flags} 里有 run_desktop 不认的参数")
        self.assertEqual(entry._desktop_flags(["--port", "53121"])["port"], 53121)
        self.assertTrue(entry._desktop_flags(["--headless-check"])["headless_check"])
        self.assertNotIn("port", entry._desktop_flags(["--port", "abc"]), "非数字端口应当忽略")


if __name__ == "__main__":
    unittest.main()
