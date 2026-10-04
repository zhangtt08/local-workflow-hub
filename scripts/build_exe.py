"""Build the Windows desktop executable (one-dir) with PyInstaller.

    python scripts/build_exe.py            # build into dist-desktop/WorkflowHub/
    python scripts/build_exe.py --check    # verify the prerequisites only

The bundle is self-contained apart from a Chromium/Edge browser on the machine (the app opens a
borderless app-mode window with it) — that dependency is stated in the build output, not hidden.
"""

from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT
LEGACY = ROOT / "dist-desktop"


def prerequisites() -> list[str]:
    problems = []
    if not (ROOT / "frontend" / "dist" / "index.html").is_file():
        problems.append("前端未构建：cd frontend && npm install && npm run build")
    if not (ROOT / "assets" / "app.ico").is_file():
        problems.append("缺少 assets/app.ico：python scripts/make_icon.py <png>")
    try:
        import PyInstaller  # noqa: F401
    except ImportError:
        problems.append("缺少 PyInstaller：python -m pip install pyinstaller")
    return problems


def build() -> int:
    problems = prerequisites()
    if problems:
        print("\n".join(problems))
        return 1
    if LEGACY.exists():
        shutil.rmtree(LEGACY)  # the one-dir build is superseded by a single exe at the root
    work = ROOT / "build"
    command = [sys.executable, "-m", "PyInstaller", "--noconfirm", "--clean",
               "--distpath", str(OUT), "--workpath", str(work / "work"),
               str(ROOT / "scripts" / "WorkflowHub.spec")]
    print(" ".join(command), flush=True)
    log = work / "pyinstaller.log"
    log.parent.mkdir(parents=True, exist_ok=True)
    with log.open("w", encoding="utf-8", errors="replace") as stream:
        done = subprocess.run(command, stdout=stream, stderr=subprocess.STDOUT, check=False)
    if done.returncode != 0:
        print(f"PyInstaller 失败，完整日志：{log}")
        tail = log.read_text(encoding="utf-8", errors="replace").splitlines()[-25:]
        for line in tail:
            print(line)
        return done.returncode
    exe = OUT / "WorkflowHub.exe"
    if not exe.is_file():
        print(f"打包结束但没找到 {exe}")
        return 1
    size = exe.stat().st_size / 1e6
    print(f"完成：{exe}")
    print(f"体积：{size:.1f} MB（单文件，双击即用；首次运行自动扫描并生成同级 data/）")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--check", action="store_true", help="只检查前置条件")
    args = parser.parse_args()
    if args.check:
        problems = prerequisites()
        print("\n".join(problems) if problems else "前置条件齐备")
        return 1 if problems else 0
    return build()


if __name__ == "__main__":
    raise SystemExit(main())
