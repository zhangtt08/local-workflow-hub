# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller spec for the WorkflowHub desktop app (one-dir: starts fast, inspectable)."""

import os
from pathlib import Path

ROOT = Path(SPECPATH).resolve().parent  # noqa: F821
BLOCK_CIPHER = None

a = Analysis(  # noqa: F821
    [str(ROOT / "scripts" / "desktop_entry.py")],
    pathex=[str(ROOT / "backend")],
    binaries=[],
    datas=[
        (str(ROOT / "frontend" / "dist"), "frontend/dist"),
        (str(ROOT / "assets" / "app.ico"), "assets"),
        (str(ROOT / "assets" / "app.png"), "assets"),
        (str(ROOT / "skills"), "skills"),
        (str(ROOT / "examples"), "examples"),
    ],
    hiddenimports=[
        "workflowhub", "workflowhub.api", "workflowhub.bootstrap", "workflowhub.catalog",
        "workflowhub.command_executor", "workflowhub.discovery", "workflowhub.services",
        "workflowhub.mcp_server", "workflowhub.desktop.launcher", "workflowhub.desktop.window",
        "uvicorn.logging", "uvicorn.loops.auto", "uvicorn.loops.asyncio",
        "uvicorn.protocols.http.auto", "uvicorn.protocols.http.h11_impl",
        "uvicorn.protocols.websockets.auto", "uvicorn.protocols.websockets.websockets_impl",
        "uvicorn.lifespan.on", "uvicorn.lifespan.off", "python_multipart",
    ],
    hookspath=[],
    runtime_hooks=[],
    excludes=["tkinter", "playwright", "PyQt5", "PySide2", "numpy", "pandas", "pytest",
              "setuptools", "pip", "test", "unittest"],
    noarchive=False,
)

pyz = PYZ(a.pure)  # noqa: F821

exe = EXE(  # noqa: F821
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name="WorkflowHub",
    debug=False,
    strip=False,
    upx=False,
    console=False,
    icon=str(ROOT / "assets" / "app.ico"),
)
