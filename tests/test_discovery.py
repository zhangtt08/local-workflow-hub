"""Discovery scanners: read-only, fixture-driven, no machine-wide scanning here."""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

from workflowhub import discovery as d

PY_TOOL = '''#!/usr/bin/env python3
"""把报告 Markdown 转成 PDF。"""
import argparse

parser = argparse.ArgumentParser()
parser.add_argument("--input", required=True, help="输入的 markdown 文件")
parser.add_argument("--out", help="输出目录")
parser.add_argument("--draft", action="store_true", help="草稿模式")
args = parser.parse_args()
print(args.input)
'''

PS_TOOL = '''param(
    [string]$FrontendUrl,
    [int]$TimeoutSec,
    [switch]$Force
)
Write-Output "waiting for $FrontendUrl"
'''

PKG = json.dumps({"name": "demo-frontend", "scripts": {
    "dev": "vite", "build:prod": "vite build && echo done"}}, indent=2)


def fixtures(root: Path) -> None:
    (root / "tools").mkdir(parents=True, exist_ok=True)
    (root / "tools" / "report_tool.py").write_text(PY_TOOL, encoding="utf-8")
    (root / "tools" / "wait_service.ps1").write_text(PS_TOOL, encoding="utf-8")
    (root / "frontend").mkdir(exist_ok=True)
    (root / "frontend" / "package.json").write_text(PKG, encoding="utf-8")
    (root / "node_modules").mkdir(exist_ok=True)
    (root / "node_modules" / "hidden.py").write_text("import argparse\nargparse\n", encoding="utf-8")
    (root / "skills" / "note-writer").mkdir(parents=True)
    (root / "skills" / "note-writer" / "skill.yaml").write_text(
        "id: note-writer\nname: Note Writer\nversion: 1.0.0\ndescription: 写一条笔记\n", encoding="utf-8")
    (root / "skills" / "note-writer" / "handler.py").write_text("pass\n", encoding="utf-8")


class SlugAndRiskTests(unittest.TestCase):
    def test_slug_is_contract_safe(self):
        self.assertEqual(d.slug("Report Tool.py"), "report-tool-py")
        self.assertEqual(d.slug("中文 工具"), "tool")  # non-ASCII collapses; callers must still get an id

    def test_risk_classification(self):
        self.assertEqual(d.risk_for(["C:\\Windows\\System32\\WindowsPowerShell\\v1.0\\powershell.exe"]), "high")
        self.assertEqual(d.risk_for(["C:\\x\\curl.exe"]), "medium")
        self.assertEqual(d.risk_for(["C:\\x\\npm.cmd", "install"]), "medium")
        self.assertEqual(d.risk_for(["C:\\x\\ffmpeg.exe"]), "low")
        self.assertEqual(d.risk_for(["C:\\x\\cleaner.exe", "/s", "rmdir"]), "high")
        self.assertEqual(d.risk_for(["C:\\x\\a.exe"], "会执行 remove-item 清理"), "high")


class LaunchPathTests(unittest.TestCase):
    def test_launch_path_dedupes_and_expands(self):
        entries = d.launch_path()
        self.assertTrue(entries, "PATH 解析结果不应为空")
        self.assertEqual(len(entries), len(set(e.lower().rstrip("\\") for e in entries)))
        self.assertTrue(any("python" in e.lower() or "windows" in e.lower() for e in entries))


class ScriptScanTests(unittest.TestCase):
    def setUp(self):
        self._dir = tempfile.mkdtemp()
        self.root = Path(d.long_path(self._dir))
        fixtures(self.root)

    def tearDown(self):
        import shutil

        shutil.rmtree(self.root, ignore_errors=True)

    def test_finds_python_tool_with_args(self):
        rows = {c.key: c for c in d.scan_script_tools([self.root])}
        tool = rows["script-tools-report-tool"]
        self.assertEqual(tool.command[0], sys.executable)
        self.assertEqual(tool.language, "py")
        self.assertEqual(tool.description, "把报告 Markdown 转成 PDF。")
        names = {a.name for a in tool.args}
        self.assertIn("input", names)
        self.assertTrue(next(a for a in tool.args if a.name == "input").required)
        self.assertEqual(tool.cwd, str(self.root / "tools"))

    def test_finds_powershell_params(self):
        rows = {c.key: c for c in d.scan_script_tools([self.root])}
        tool = rows["script-tools-wait-service"]
        self.assertEqual(tool.command[:2], ["powershell.exe", "-NoProfile"])
        self.assertEqual({a.name for a in tool.args}, {"FrontendUrl", "TimeoutSec", "Force"})

    def test_skips_dependency_folders(self):
        keys = [c.key for c in d.scan_script_tools([self.root])]
        self.assertFalse([k for k in keys if "hidden" in k])

    def test_project_scripts_and_skill_packages(self):
        projects = {c.key: c for c in d.scan_project_workflows([self.root])}
        self.assertIn("npm-demo-frontend-dev", projects)
        build = projects["npm-demo-frontend-build-prod"]
        self.assertEqual(build.command[1:3], ["run", "build:prod"])
        self.assertEqual(build.cwd, str(self.root / "frontend"))
        skills = {c.key: c for c in d.scan_skill_dirs([self.root])}
        matched = [k for k in skills if k.endswith("-note-writer")]
        self.assertEqual(len(matched), 1, f"Skill 包应被识别：{list(skills)}")
        self.assertEqual(skills[matched[0]].loc, str(self.root / "skills" / "note-writer" / "skill.yaml"))

    def test_scan_is_read_only(self):
        before = {p.name for p in self.root.rglob("*") if p.is_file()}
        d.run_scan([self.root], steps=["scripts", "projects", "skills"])
        after = {p.name for p in self.root.rglob("*") if p.is_file()}
        self.assertEqual(before, after)


class ExecutableScanTests(unittest.TestCase):
    def test_executables_come_from_given_dirs_only(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            (root / "mytool.exe").write_text("MZ", encoding="latin-1")
            (root / "notes.txt").write_text("x", encoding="utf-8")
            (root / "subdir").mkdir()
            rows = d.scan_executables(path_dirs=[str(root)])
            self.assertEqual([r.key for r in rows], ["cli-mytool"])
            self.assertEqual(rows[0].command, [str(root / "mytool.exe")])

    def test_builtins_only_report_what_exists(self):
        rows = d.scan_builtins()
        for row in rows:
            self.assertTrue(Path(row.loc).is_file(), f"{row.key} 声称存在但文件不在")


if __name__ == "__main__":
    unittest.main()
