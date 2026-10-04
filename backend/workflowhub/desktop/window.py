"""Borderless window control for the app-mode browser window (pure Win32, no CDP).

Chromium paints its own ~31px app bar inside the client area and clearing ``WS_CAPTION`` does not
remove it on this machine, so the bar is clipped out with a window region and the window is placed
``bar`` px higher than the rectangle the user sees. The bar height is measured from the page's own
viewport instead of being hard-coded, so it stays right under DPI scaling.
"""

from __future__ import annotations

import ctypes as C
import ctypes.wintypes as WT
import time
from pathlib import Path
from typing import Any

user32 = C.WinDLL("user32", use_last_error=True)
gdi32 = C.WinDLL("gdi32", use_last_error=True)
kernel32 = C.WinDLL("kernel32", use_last_error=True)

GWL_STYLE = -16
WS_CAPTION = 0x00C00000
WS_THICKFRAME = 0x00040000
WS_SYSMENU = 0x00080000
WS_MINIMIZEBOX = 0x00020000
WS_MAXIMIZEBOX = 0x00010000
SWP_FRAMECHANGED = 0x0020
SWP_NOZORDER = 0x0004
SWP_NOACTIVATE = 0x0010
SW_RESTORE = 9
SW_SHOW = 5
SW_MINIMIZE = 6
SW_HIDE = 0
WM_SETICON = 0x0080
ICON_SMALL = 0
ICON_BIG = 1
IMAGE_ICON = 3
LR_LOADFROMFILE = 0x0010
LR_DEFAULTSIZE = 0x0040
SPI_GETWORKAREA = 0x0030
PW_RENDERFULLCONTENT = 0x0002

WNDENUMPROC = C.WINFUNCTYPE(C.c_bool, WT.HWND, C.c_void_p)
ZERO_ARG_TYPES = {
    "GetWindowLongW": [WT.HWND, C.c_int],
    "SetWindowLongW": [WT.HWND, C.c_int, C.c_ssize_t],
    "SetWindowPos": [WT.HWND, WT.HWND, C.c_int, C.c_int, C.c_int, C.c_int, WT.UINT],
    "GetWindowRect": [WT.HWND, C.POINTER(WT.RECT)],
    "GetClientRect": [WT.HWND, C.POINTER(WT.RECT)],
    "SetWindowRgn": [WT.HWND, WT.HANDLE, C.c_bool],
    "GetForegroundWindow": [],
    "SetForegroundWindow": [WT.HWND],
    "BringWindowToTop": [WT.HWND],
    "SetActiveWindow": [WT.HWND],
    "IsWindow": [WT.HWND],
    "IsWindowVisible": [WT.HWND],
    "IsIconic": [WT.HWND],
    "GetWindowTextW": [WT.HWND, C.c_wchar_p, C.c_int],
    "GetWindowTextLengthW": [WT.HWND],
    "GetWindowThreadProcessId": [WT.HWND, C.POINTER(WT.DWORD)],
    "ShowWindow": [WT.HWND, C.c_int],
    "GetDpiForWindow": [WT.HWND],
    "PrintWindow": [WT.HWND, WT.HANDLE, WT.UINT],
    "SendMessageW": [WT.HWND, WT.UINT, WT.WPARAM, WT.LPARAM],
    "PostMessageW": [WT.HWND, WT.UINT, WT.WPARAM, WT.LPARAM],
    "SystemParametersInfoW": [WT.UINT, WT.UINT, C.c_void_p, WT.UINT],
    "SetProcessDpiAwarenessContext": [C.c_void_p],
}
for _name, _types in ZERO_ARG_TYPES.items():
    func = getattr(user32, _name)
    func.argtypes = _types
    if _name in {"IsWindow", "IsWindowVisible", "IsIconic", "SetWindowRgn", "PrintWindow", "ShowWindow"}:
        func.restype = C.c_bool
    elif _name in {"GetWindowLongW"}:
        func.restype = C.c_ssize_t
    elif _name in {"GetWindowTextLengthW", "GetDpiForWindow"}:
        func.restype = C.c_int
    else:
        func.restype = C.c_int
user32.GetWindowLongW.restype = C.c_ssize_t
user32.SetWindowLongW.restype = C.c_ssize_t
user32.DefWindowProcW.restype = C.c_ssize_t
gdi32.CreateRectRgn.restype = WT.HANDLE
gdi32.CreateRectRgn.argtypes = [C.c_int, C.c_int, C.c_int, C.c_int]
gdi32.CreateRoundRectRgn.restype = WT.HANDLE
gdi32.CreateRoundRectRgn.argtypes = [C.c_int, C.c_int, C.c_int, C.c_int, C.c_int, C.c_int]
gdi32.DeleteObject.argtypes = [C.c_void_p]
# GetDC / ReleaseDC / LoadImageW live in user32, not gdi32
user32.GetDC.restype = WT.HANDLE
user32.GetDC.argtypes = [WT.HWND]
user32.ReleaseDC.restype = C.c_int
user32.ReleaseDC.argtypes = [WT.HWND, WT.HANDLE]
user32.LoadImageW.restype = C.c_void_p
user32.LoadImageW.argtypes = [C.c_void_p, C.c_wchar_p, C.c_uint, C.c_int, C.c_int, C.c_uint]
user32.AttachThreadInput.restype = WT.BOOL
user32.AttachThreadInput.argtypes = [WT.DWORD, WT.DWORD, WT.BOOL]
GetDC = user32.GetDC
ReleaseDC = user32.ReleaseDC
LoadImage = user32.LoadImageW
gdi32.CreateCompatibleDC.restype = WT.HANDLE
gdi32.CreateCompatibleDC.argtypes = [WT.HANDLE]
gdi32.CreateCompatibleBitmap.restype = WT.HANDLE
gdi32.CreateCompatibleBitmap.argtypes = [WT.HANDLE, C.c_int, C.c_int]
gdi32.SelectObject.restype = C.c_void_p
gdi32.SelectObject.argtypes = [WT.HANDLE, C.c_void_p]
gdi32.DeleteDC.argtypes = [WT.HANDLE]
gdi32.GetDIBits.restype = C.c_int
gdi32.GetDIBits.argtypes = [WT.HANDLE, WT.HANDLE, WT.UINT, WT.UINT, C.c_void_p, C.c_void_p, WT.UINT]
kernel32.OpenProcess.restype = WT.HANDLE
kernel32.OpenProcess.argtypes = [WT.DWORD, WT.BOOL, WT.DWORD]
kernel32.CloseHandle.argtypes = [WT.HANDLE]
kernel32.CloseHandle.restype = WT.BOOL
kernel32.QueryFullProcessImageNameW.restype = WT.BOOL
kernel32.QueryFullProcessImageNameW.argtypes = [WT.HANDLE, WT.DWORD, C.c_wchar_p, C.POINTER(WT.DWORD)]
PROCESS_QUERY_LIMITED_INFORMATION = 0x1000

CORNER_RADIUS = 18


def make_dpi_aware() -> None:
    try:
        user32.SetProcessDpiAwarenessContext(C.c_void_p(-4))  # PerMonitorV2
    except Exception:
        try:
            C.windll.shcore.SetProcessDpiAwareness(2)
        except Exception:
            pass


def process_image(pid: int) -> str:
    handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not handle:
        return ""
    try:
        size = WT.DWORD(4096)
        buffer = C.create_unicode_buffer(size.value)
        if kernel32.QueryFullProcessImageNameW(handle, 0, buffer, C.byref(size)):
            return buffer.value
    finally:
        kernel32.CloseHandle(handle)
    return ""


kernel32.CloseHandle.argtypes = [WT.HANDLE]


def _hwnd_int(value: Any) -> int:
    """ctypes hands HWNDs back as c_void_p objects whose int() is bytes — read .value instead."""
    if value is None:
        return 0
    raw = getattr(value, "value", value)
    if isinstance(raw, bytes):
        return int.from_bytes(raw, "little", signed=False)
    return int(raw)


def _h(value: Any) -> WT.HWND:
    return WT.HWND(_hwnd_int(value))


def window_rect(hwnd: int) -> tuple[int, int, int, int]:
    rect = WT.RECT()
    user32.GetWindowRect(_h(hwnd), C.byref(rect))
    return rect.left, rect.top, rect.right, rect.bottom


def client_rect(hwnd: int) -> tuple[int, int]:
    rect = WT.RECT()
    user32.GetClientRect(_h(hwnd), C.byref(rect))
    return rect.right, rect.bottom


def work_area() -> tuple[int, int, int, int]:
    rect = WT.RECT()
    if user32.SystemParametersInfoW(SPI_GETWORKAREA, 0, C.byref(rect), 0):
        return rect.left, rect.top, rect.right, rect.bottom
    return 0, 0, 1920, 1080


def find_window(pid: int, title_hint: str = "", browser_exe: str = "") -> int | None:
    """Visible top-level window owned by ``pid`` (or by the same browser image) with a big client."""
    hint = title_hint.lower()
    exe_low = browser_exe.lower()
    ranked: list[tuple[int, int, int]] = []

    def cb(hwnd, _lparam):
        if not user32.IsWindowVisible(_h(hwnd)):
            return True
        window_pid = WT.DWORD()
        user32.GetWindowThreadProcessId(_h(hwnd), C.byref(window_pid))
        owned = window_pid.value == pid
        if not owned:
            if not exe_low:
                return True
            image = process_image(window_pid.value)
            if image.lower() != exe_low:
                return True
        length = user32.GetWindowTextLengthW(_h(hwnd))
        buffer = C.create_unicode_buffer(max(length, 0) + 1)
        user32.GetWindowTextW(_h(hwnd), buffer, max(length, 0) + 1)
        cw, ch = client_rect(_h(hwnd))
        if cw < 320 or ch < 240:
            return True
        score = 0
        if owned:
            score += 40
        if hint and hint in buffer.value.lower():
            score += 30
        score += min(cw // 100, 20)
        ranked.append((score, _hwnd_int(hwnd), cw * ch))
        return True

    user32.EnumWindows(WNDENUMPROC(cb), None)
    if not ranked:
        return None
    ranked.sort(reverse=True)
    return ranked[0][1]


class WindowController:
    """Owns one app-mode window: measures Chromium's bar, clips it, maps page gestures to Win32."""

    def __init__(self, pid: int, title_hint: str = "WorkflowHub", browser_exe: str = ""):
        self.pid = pid
        self.title_hint = title_hint
        self.browser_exe = browser_exe
        self.hwnd: int | None = None
        self.bar = 31
        self.dpr = 1.0
        self.visible: tuple[int, int, int, int] = (0, 0, 0, 0)
        self.restored: tuple[int, int, int, int] | None = None
        self.maximized = False
        self.minimized = False
        self.rounded = True

    def wait(self, timeout: float = 12.0) -> int | None:
        deadline = time.time() + timeout
        while time.time() < deadline:
            self.hwnd = find_window(self.pid, self.title_hint, self.browser_exe)
            if self.hwnd:
                return self.hwnd
            time.sleep(0.12)
        return None

    def is_alive(self) -> bool:
        return bool(self.hwnd) and bool(user32.IsWindow(_h(self.hwnd)))

    def attach(self, hwnd: int) -> None:
        self.hwnd = hwnd
        self.visible = self._read_visible()

    def _read_visible(self) -> tuple[int, int, int, int]:
        left, top, right, bottom = window_rect(self.hwnd)
        return left, top + self.bar, right, bottom

    def measure(self, inner_height: float, device_pixel_ratio: float) -> dict[str, Any]:
        """Compute the bar from the page's own viewport, then clip it away."""
        self.dpr = max(0.5, float(device_pixel_ratio or 1.0))
        cw, ch = client_rect(self.hwnd)
        bar = int(round(ch - inner_height * self.dpr))
        self.bar = max(0, min(bar, 120))
        self.visible = self._read_visible()
        self._apply(self.visible, keep_region=True)
        return self.state()

    def _apply(self, rect: tuple[int, int, int, int], keep_region: bool = False) -> None:
        left, top, right, bottom = rect
        width = max(360, right - left)
        height = max(240, bottom - top)
        style = user32.GetWindowLongW(_h(self.hwnd), GWL_STYLE)
        stripped = style & ~(WS_CAPTION | WS_THICKFRAME | WS_SYSMENU | WS_MAXIMIZEBOX | WS_MINIMIZEBOX)
        if stripped != style:
            user32.SetWindowLongW(_h(self.hwnd), GWL_STYLE, stripped)
            user32.SetWindowPos(_h(self.hwnd), 0, 0, 0, 0, 0,
                                SWP_FRAMECHANGED | SWP_NOZORDER | SWP_NOACTIVATE)
        user32.SetWindowPos(_h(self.hwnd), 0, left, top - self.bar, width, height + self.bar,
                           SWP_NOZORDER | SWP_NOACTIVATE)
        if not keep_region:
            self.refresh_region()
        else:
            self.refresh_region()

    def refresh_region(self) -> None:
        cw, ch = client_rect(self.hwnd)
        radius = CORNER_RADIUS if (self.rounded and not self.maximized) else 0
        if radius:
            region = gdi32.CreateRoundRectRgn(0, self.bar, cw + 1, ch + 1, radius, radius)
        else:
            region = gdi32.CreateRectRgn(0, self.bar, cw, ch)
        user32.SetWindowRgn(_h(self.hwnd), region, True)
        gdi32.DeleteObject(region)

    def set_icon(self, icon_path: str | Path) -> bool:
        path = str(Path(icon_path))
        if not Path(path).is_file():
            return False
        small = LoadImage(None, path, IMAGE_ICON, 16, 16, LR_LOADFROMFILE)
        big = LoadImage(None, path, IMAGE_ICON, 32, 32, LR_LOADFROMFILE | LR_DEFAULTSIZE)
        user32.SendMessageW(_h(self.hwnd), WM_SETICON, ICON_SMALL, C.c_ssize_t(small or 0).value)
        user32.SendMessageW(_h(self.hwnd), WM_SETICON, ICON_BIG, C.c_ssize_t(big or 0).value)
        return bool(small or big)

    def state(self) -> dict[str, Any]:
        left, top, right, bottom = self.visible
        return {"hwnd": self.hwnd, "x": left, "y": top, "width": right - left, "height": bottom - top,
                "bar": self.bar, "dpr": self.dpr, "maximized": self.maximized,
                "minimized": self.minimized, "alive": self.is_alive(),
                "work_area": list(work_area())}

    # ── gestures from the UI ─────────────────────────────────────────────────
    def act(self, action: str, payload: dict[str, Any] | None = None) -> dict[str, Any]:
        payload = payload or {}
        if not self.is_alive():
            return {"ok": False, "reason": "窗口尚未接管", "state": self.state()}
        if action == "drag":
            dx = int(round(float(payload.get("dx", 0)) * self.dpr))
            dy = int(round(float(payload.get("dy", 0)) * self.dpr))
            if self.maximized:
                self.unmaximize(keep_ratio=float(payload.get("ratio", 0.5)))
            left, top, right, bottom = self.visible
            self.visible = (left + dx, top + dy, right + dx, bottom + dy)
            self._apply(self.visible)
        elif action == "resize":
            self.resize(payload)
        elif action == "minimize":
            user32.ShowWindow(_h(self.hwnd), SW_MINIMIZE)
            self.minimized = True
        elif action == "restore":
            if self.minimized:
                user32.ShowWindow(_h(self.hwnd), SW_RESTORE)
                self.minimized = False
            elif self.maximized:
                self.unmaximize()
        elif action == "maximize":
            self.maximize()
        elif action == "toggle-maximize":
            (self.unmaximize if self.maximized else self.maximize)()
        elif action == "move-to":
            width = self.visible[2] - self.visible[0]
            height = self.visible[3] - self.visible[1]
            self.visible = (int(payload["x"]), int(payload["y"]), int(payload["x"]) + width, int(payload["y"]) + height)
            self._apply(self.visible)
        elif action == "snap":
            self.snap(str(payload.get("side", "right")))
        elif action == "focus":
            self.focus()
        elif action == "close":
            self.close()
        else:
            return {"ok": False, "reason": f"未知窗口动作：{action}", "state": self.state()}
        return {"ok": True, "action": action, "state": self.state()}

    def maximize(self) -> None:
        if not self.maximized:
            self.restored = self.visible
        wa_left, wa_top, wa_right, wa_bottom = work_area()
        self.visible = (wa_left, wa_top, wa_right, wa_bottom)
        self.maximized = True
        self.minimized = False
        self._apply(self.visible)

    def unmaximize(self, keep_ratio: float = 0.5) -> None:
        target = self.restored or self._default_rect()
        if self.maximized:
            left, _top, right, _bottom = self.visible
            width = target[2] - target[0]
            anchor = left + int((right - left) * max(0.0, min(1.0, keep_ratio)))
            target = (anchor - int(width * keep_ratio), target[1], anchor - int(width * keep_ratio) + width, target[3])
        self.maximized = False
        self.visible = target
        self._apply(target)

    def _default_rect(self) -> tuple[int, int, int, int]:
        wa_left, wa_top, wa_right, wa_bottom = work_area()
        width = min(1180, wa_right - wa_left)
        height = min(760, wa_bottom - wa_top)
        left = wa_left + ((wa_right - wa_left) - width) // 2
        top = wa_top + ((wa_bottom - wa_top) - height) // 2
        return (left, top, left + width, top + height)

    def resize(self, payload: dict[str, Any]) -> None:
        edges = str(payload.get("edges", ""))
        dx = int(round(float(payload.get("dx", 0)) * self.dpr))
        dy = int(round(float(payload.get("dy", 0)) * self.dpr))
        if self.maximized:
            self.unmaximize(keep_ratio=0.5)
        left, top, right, bottom = self.visible
        if "l" in edges:
            left += dx
        if "r" in edges:
            right += dx
        if "t" in edges:
            top += dy
        if "b" in edges:
            bottom += dy
        if right - left < 420:
            if "l" in edges:
                left = right - 420
            else:
                right = left + 420
        if bottom - top < 320:
            if "t" in edges:
                top = bottom - 320
            else:
                bottom = top + 320
        self.visible = (left, top, right, bottom)
        self._apply(self.visible)

    def snap(self, side: str) -> None:
        wa_left, wa_top, wa_right, wa_bottom = work_area()
        half = (wa_right - wa_left) // 2
        rects = {
            "left": (wa_left, wa_top, wa_left + half, wa_bottom),
            "right": (wa_right - half, wa_top, wa_right, wa_bottom),
            "top": (wa_left, wa_top, wa_right, wa_top + (wa_bottom - wa_top) // 2),
            "bottom": (wa_left, wa_bottom - (wa_bottom - wa_top) // 2, wa_right, wa_bottom),
        }
        if side not in rects:
            return
        self.maximized = False
        self.restored = rects[side]
        self.visible = rects[side]
        self._apply(self.visible)

    def focus(self) -> bool:
        """Bring the app window forward; Windows blocks this across processes without the trick."""
        if not self.is_alive():
            return False
        hwnd = _h(self.hwnd)
        foreground = user32.GetForegroundWindow()
        current = _hwnd_int(user32.GetWindowThreadProcessId(hwnd, None))
        previous = _hwnd_int(user32.GetWindowThreadProcessId(foreground, None))
        attached = False
        if current and previous and current != previous:
            attached = bool(user32.AttachThreadInput(previous, current, True))
        user32.ShowWindow(hwnd, SW_RESTORE) if self.minimized else None
        user32.BringWindowToTop(hwnd)
        result = user32.SetForegroundWindow(hwnd)
        if attached:
            user32.AttachThreadInput(previous, current, False)
        user32.SetActiveWindow(hwnd)
        return bool(result)

    def hide(self) -> None:
        if self.is_alive():
            user32.ShowWindow(_h(self.hwnd), SW_HIDE)

    def close(self) -> None:
        if self.is_alive():
            user32.PostMessageW(_h(self.hwnd), 0x0010, 0, 0)  # WM_CLOSE

    def capture(self, path: str | Path) -> bool:
        """PrintWindow the real OS window so the borderless claim can be inspected as pixels."""
        if not self.is_alive():
            return False
        cw, ch = client_rect(self.hwnd)
        if cw <= 2 or ch <= 2:
            return False
        hdc = GetDC(_h(self.hwnd))
        mem = gdi32.CreateCompatibleDC(hdc)
        bitmap = gdi32.CreateCompatibleBitmap(hdc, cw, ch)
        gdi32.SelectObject(mem, bitmap)
        user32.PrintWindow(_h(self.hwnd), mem, PW_RENDERFULLCONTENT)

        class BITMAPINFOHEADER(C.Structure):
            _fields_ = [("biSize", C.c_uint), ("biWidth", C.c_int), ("biHeight", C.c_int),
                        ("biPlanes", C.c_ushort), ("biBitCount", C.c_ushort), ("biCompression", C.c_uint),
                        ("biSizeImage", C.c_uint), ("biXPelsPerMeter", C.c_int), ("biYPelsPerMeter", C.c_int),
                        ("biClrUsed", C.c_uint), ("biClrImportant", C.c_uint)]

        class BITMAPINFO(C.Structure):
            _fields_ = [("bmiHeader", BITMAPINFOHEADER), ("bmiColors", C.c_uint * 3)]

        info = BITMAPINFO()
        info.bmiHeader.biSize = C.sizeof(BITMAPINFOHEADER)
        info.bmiHeader.biWidth = cw
        info.bmiHeader.biHeight = -ch
        info.bmiHeader.biPlanes = 1
        info.bmiHeader.biBitCount = 32
        buffer = C.create_string_buffer(cw * ch * 4)
        got = gdi32.GetDIBits(hdc, bitmap, 0, ch, buffer, C.byref(info), 0)
        gdi32.DeleteDC(mem)
        gdi32.DeleteObject(bitmap)
        ReleaseDC(_h(self.hwnd), hdc)
        if not got:
            return False
        from PIL import Image

        Image.frombuffer("RGBA", (cw, ch), buffer.raw, "raw", "BGRA", 0, 1).convert("RGB").save(str(path))
        return True
