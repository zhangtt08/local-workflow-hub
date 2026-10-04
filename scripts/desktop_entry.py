"""Frozen-executable entry: data lives next to the exe, the window opens without a console.

``--windowed`` builds have **no** stdout/stderr (they are ``None``), so the first thing this does is
install safe streams and a crash log; otherwise any ``print`` or uvicorn log line kills the app with
PyInstaller's "unhandled exception" dialog and nothing is written anywhere.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path


def _paths() -> tuple[Path, Path]:
    exe_dir = Path(sys.executable).resolve().parent if getattr(sys, "frozen", False) else Path.cwd()
    data = Path(os.environ.get("WORKFLOWHUB_DATA") or exe_dir / "data")
    data.mkdir(parents=True, exist_ok=True)
    return exe_dir, data


def _desktop_flags(argv: list[str]) -> dict[str, object]:
    """exe 也认这几个开关（``--port/--host/--browser/--url/--headless-check/--start``）。

    双击不带参数时走默认值；端口被抢时用户不需要重新打包，只要用一条命令行把端口钉住。
    """
    flags: dict[str, object] = {"start": "maximized"}
    for index, item in enumerate(argv):
        value = argv[index + 1] if index + 1 < len(argv) else ""
        if item == "--port" and value.isdigit():
            flags["port"] = int(value)
        elif item in ("--host", "--browser", "--url", "--start") and value:
            flags[item[2:]] = value
        elif item == "--headless-check":
            flags["headless_check"] = True
    return flags


def _harden_stdio(data: Path) -> Path:
    """Give the app real stdout/stderr even when Windows started it without a console."""
    log = data / "desktop.log"
    for name in ("stdout", "stderr"):
        if getattr(sys, name, None) is None:
            try:
                stream = log.open("a", encoding="utf-8", buffering=1)
            except OSError:
                stream = open(os.devnull, "a", encoding="utf-8")  # noqa: SIM115
            setattr(sys, name, stream)
    return log


def _report_crash(data: Path, exc: BaseException) -> None:
    import traceback

    text = "".join(traceback.format_exception(type(exc), exc, exc.__traceback__))
    try:
        (data / "desktop-error.log").write_text(text, encoding="utf-8")
    except OSError:
        pass
    try:
        print(f"WorkflowHub 启动失败，日志见 {data / 'desktop-error.log'}\n{text}", file=sys.stderr)
    except Exception:  # noqa: BLE001 — never let the reporter be the crash
        pass


def main() -> int:
    data = Path.cwd() / "data"
    try:
        exe_dir, data = _paths()
        _harden_stdio(data)
        if getattr(sys, "frozen", False):
            os.environ.setdefault("WORKFLOWHUB_DATA", str(data))
            bundle = Path(getattr(sys, "_MEIPASS", exe_dir))
            for name in ("skills", "examples"):
                target, source = exe_dir / name, bundle / name
                if source.is_dir() and not target.exists():
                    import shutil

                    shutil.copytree(source, target)
            if (exe_dir / "skills").is_dir():
                os.environ.setdefault("WORKFLOWHUB_SKILLS", str(exe_dir / "skills"))
            if "--mcp" in sys.argv[1:]:
                from workflowhub.mcp_server import serve_stdio

                return serve_stdio()
            from workflowhub.desktop.launcher import run_desktop

            return run_desktop(**_desktop_flags(sys.argv[1:]))
        from workflowhub.__main__ import main as cli_main

        return cli_main(["desktop"])
    except BaseException as exc:  # noqa: BLE001 — a desktop app must leave a trace, not a dialog
        _report_crash(data, exc)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
