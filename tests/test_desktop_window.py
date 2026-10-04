"""Desktop window helpers that can be proven without a live window (Windows-only bits guarded)."""

from __future__ import annotations

import ctypes as C
import os
import socket
import sys
import tempfile
import threading
import unittest
from pathlib import Path

IS_WINDOWS = sys.platform == "win32"

if IS_WINDOWS:
    from workflowhub.desktop import launcher
    from workflowhub.desktop.window import (WindowController, _h, _hwnd_int, client_rect,
                                            find_window, process_image, work_area)
    from workflowhub.desktop.launcher import bundled_asset, browser_candidates, chromium_from_playwright


@unittest.skipUnless(IS_WINDOWS, "无边框窗口控制只在 Windows 上生效")
class HandleTests(unittest.TestCase):
    def test_hwnd_coercion_accepts_int_object_and_none(self):
        self.assertEqual(_hwnd_int(None), 0)
        self.assertEqual(_hwnd_int(1234), 1234)
        self.assertEqual(_hwnd_int(_h(4321)), 4321)
        self.assertEqual(_h(0).value or 0, 0)

    def test_work_area_is_a_real_screen_region(self):
        left, top, right, bottom = work_area()
        self.assertEqual((left, top), (0, 0))
        self.assertGreater(right, 800)
        self.assertGreater(bottom, 600)

    def test_find_window_ignores_foreign_processes(self):
        self.assertIsNone(find_window(2_147_483_646, "workflowhub-no-such-window"))

    def test_process_image_reports_paths_for_live_pids(self):
        kernel32 = C.windll.kernel32
        me = kernel32.GetCurrentProcess()
        pid = C.windll.kernel32.GetCurrentProcessId()
        self.assertTrue(process_image(pid).lower().endswith(".exe"))
        self.assertEqual(process_image(2_147_483_646), "")
        del me

    def test_browser_candidates_exist(self):
        candidates = browser_candidates()
        self.assertTrue(candidates, "至少要有一个可用的窗口化浏览器")
        for exe in candidates:
            self.assertTrue(Path(exe).is_file(), exe)

    def test_icon_asset_is_shipped(self):
        icon = bundled_asset("app.ico")
        self.assertIsNotNone(icon)
        self.assertGreater(icon.stat().st_size, 1000)

    def test_controller_geometry_model(self):
        controller = WindowController(pid=0)
        controller.hwnd = 1
        controller.bar = 31
        controller.dpr = 1.0
        controller.visible = (100, 100, 900, 700)
        state = controller.state()
        self.assertEqual(state["width"], 800)
        self.assertEqual(state["height"], 600)
        self.assertFalse(state["alive"])
        default = controller._default_rect()
        self.assertGreater(default[2] - default[0], 400)

    def test_unknown_action_is_reported_not_raised(self):
        controller = WindowController(pid=0)
        result = controller.act("teleport")
        self.assertFalse(result["ok"])
        self.assertTrue(result["reason"])
        controller.hwnd = 1
        controller.is_alive = lambda: True  # type: ignore[method-assign]
        unknown = controller.act("teleport")
        self.assertFalse(unknown["ok"])
        self.assertIn("未知窗口动作", unknown["reason"])

    def test_chromium_fallback_path_shape(self):
        found = chromium_from_playwright()
        if found:
            self.assertTrue(Path(found).is_file())
            self.assertIn("chromium", found.lower())


@unittest.skipUnless(IS_WINDOWS, "Profile 目录策略只在 Windows 桌面上有意义")
class ProfileTests(unittest.TestCase):
    """Profile 必须复用同一个：按随机端口命名的老实现每双击一次就多一份 40～70 MB 的 Edge profile。"""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.data = Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def test_default_is_one_shared_profile(self):
        self.assertEqual(launcher.profile_dir(self.data, 1234), self.data / "browser-profile")
        self.assertEqual(launcher.profile_dir(self.data, 5678), self.data / "browser-profile")

    def test_forced_second_instance_still_isolates(self):
        os.environ["WORKFLOWHUB_FORCE"] = "1"
        try:
            self.assertEqual(launcher.profile_dir(self.data, 5678), self.data / "browser-profile-5678")
        finally:
            del os.environ["WORKFLOWHUB_FORCE"]

    def test_dead_legacy_profile_becomes_the_shared_one(self):
        with socket.socket() as probe:
            probe.bind(("127.0.0.1", 0))
            dead_port = probe.getsockname()[1]
        self.assertFalse(launcher.port_alive(dead_port), "没人应答的端口应当判为已死")
        old = self.data / f"browser-profile-{dead_port}"
        (old / "Default").mkdir(parents=True)
        (old / "Default" / "Cookies").write_text("保住登录态", encoding="utf-8")
        chosen = launcher.profile_dir(self.data, 4321)
        self.assertEqual(chosen, self.data / "browser-profile")
        self.assertFalse(old.exists(), "端口已死的旧 profile 不该再留下")
        self.assertEqual((chosen / "Default" / "Cookies").read_text(encoding="utf-8"), "保住登录态",
                         "老用户的登录态要迁过来，不能当垃圾删掉")

    def test_live_profile_is_never_touched(self):
        from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer

        with socket.socket() as probe:
            probe.bind(("127.0.0.1", 0))
            live_port = probe.getsockname()[1]
        server = ThreadingHTTPServer(("127.0.0.1", live_port), SimpleHTTPRequestHandler)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        try:
            self.assertTrue(launcher.port_alive(live_port), "有实例应答的端口必须判为活着")
            busy = self.data / f"browser-profile-{live_port}"
            (busy / "Default").mkdir(parents=True)
            (busy / "Default" / "Cookies").write_text("在用", encoding="utf-8")
            launcher.profile_dir(self.data, 4321)
            self.assertTrue(busy.is_dir(), "正在被别的实例使用的 profile 绝不能删")
        finally:
            server.shutdown()
            server.server_close()


    def test_trim_removes_regenerable_but_keeps_login_state(self):
        profile = self.data / "browser-profile"
        for name, size in (("ProvenanceData/x", 2000), ("component_crx_cache", 3000),
                           ("Edge Entity Extraction", 1000), ("Default/Cache/deep", 1500)):
            target = profile / name
            target.mkdir(parents=True, exist_ok=True)
            (target / "blob").write_bytes(b"z" * size)
        (profile / "Default").mkdir(parents=True, exist_ok=True)
        (profile / "Default" / "Cookies").write_text("登录态", encoding="utf-8")
        freed = launcher.trim_profile(profile)
        self.assertGreaterEqual(freed, 7500, "组件字典与缓存都该计入回收量")
        self.assertFalse((profile / "ProvenanceData").exists())
        self.assertFalse((profile / "component_crx_cache").exists())
        self.assertFalse((profile / "Default" / "Cache").exists())
        self.assertEqual((profile / "Default" / "Cookies").read_text(encoding="utf-8"), "登录态",
                         "清缓存绝不能连带删掉登录态")
        self.assertEqual(launcher.trim_profile(profile), 0, "第二次应当什么都不删")


if __name__ == "__main__":
    unittest.main()
