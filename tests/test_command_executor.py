"""CommandExecutor: how a catalogue tool becomes a real process, and what it refuses to do."""

from __future__ import annotations

import asyncio
import sys
import tempfile
import unittest
from pathlib import Path

from workflowhub.command_executor import CommandExecutor, build_command, command_line, decode_output
from workflowhub.errors import WorkflowHubError
from workflowhub.models import ExecutorSpec, Permission, SkillDefinition, ValueType, ValueSpec
from workflowhub.ports import ExecutionContext


def command_skill(entry: str, args: list[str] | None = None, specs: list[dict] | None = None,
                  style: str = "double-dash", cwd: str | None = None) -> SkillDefinition:
    inputs = {
        "argv": ValueSpec(type=ValueType.ARRAY, required=False),
        "stdin": ValueSpec(type=ValueType.STRING, required=False),
        "dry_run": ValueSpec(type=ValueType.BOOLEAN, required=False),
    }
    for spec in specs or []:
        inputs[spec["name"].lstrip("-").lower().replace(" ", "-")] = ValueSpec(
            type=ValueType.BOOLEAN if spec.get("kind") == "boolean" else ValueType.STRING, required=False)
    return SkillDefinition(
        id="demo-cmd", name="Demo", version="1.0.0", description="test",
        inputs=inputs,
        outputs={"exit_code": ValueSpec(type=ValueType.INTEGER, required=False),
                 "stdout": ValueSpec(type=ValueType.STRING, required=False),
                 "stderr": ValueSpec(type=ValueType.STRING, required=False),
                 "command_line": ValueSpec(type=ValueType.STRING, required=False)},
        executor=ExecutorSpec(type="command", entry=entry, args=args or [], cwd=cwd),
        permissions=[Permission.SHELL],
        metadata={"arg_specs": specs or [], "flag_style": style},
    )


def context(root: Path) -> ExecutionContext:
    return ExecutionContext("trace", "run", "node", root, frozenset({Permission.SHELL}))


class BuildCommandTests(unittest.TestCase):
    def test_argv_is_appended_verbatim(self):
        skill = command_skill(sys.executable)
        tokens, _ = build_command(skill, {"argv": ["-c", "print('a b')"]})
        self.assertEqual(tokens, [sys.executable, "-c", "print('a b')"])

    def test_named_flags_use_declared_style(self):
        skill = command_skill(sys.executable, specs=[{"name": "input", "kind": "string"},
                                                    {"name": "draft", "kind": "boolean"}])
        tokens, _ = build_command(skill, {"flags": {"input": "a.md", "draft": True}})
        self.assertEqual(tokens[1:], ["--input", "a.md", "--draft"])
        single = command_skill("C:\\ps.ps1", specs=[{"name": "FrontendUrl", "kind": "string"}], style="single-dash")
        tokens, _ = build_command(single, {"flags": {"frontend-url": "http://x"}})
        self.assertEqual(tokens[1:], ["-FrontendUrl", "http://x"])

    def test_rejects_relative_entry_and_nul_bytes(self):
        with self.assertRaises(WorkflowHubError):
            build_command(command_skill("notepad"), {})
        with self.assertRaises(WorkflowHubError):
            build_command(command_skill(sys.executable), {"argv": ["has\x00nul"]})
        with self.assertRaises(WorkflowHubError):
            build_command(command_skill(sys.executable), {"flags": "not-a-dict"})

    def test_non_string_parameters_are_serialised_not_rejected(self):
        skill = command_skill(sys.executable, specs=[{"name": "count", "kind": "integer"}])
        tokens, _ = build_command(skill, {"argv": ["-x", 3.5, True], "flags": {"count": 7}})
        self.assertEqual(tokens[1:], ["-x", "3.5", "true", "--count", "7"])

    def test_command_line_quotes_spaces(self):
        self.assertEqual(command_line(["a b", "c"]), '"a b" c')


class DecodingTests(unittest.IsolatedAsyncioTestCase):
    def test_utf8_wins_then_gbk_rescues_chinese_console_output(self):
        self.assertEqual(decode_output("你好".encode("utf-8")), "你好")
        self.assertEqual(decode_output("无法初始化设备 PRN".encode("gbk")), "无法初始化设备 PRN")
        self.assertIn("broken", decode_output(b"\xff\xfe broken"))

    async def test_chinese_console_output_survives_the_round_trip(self):
        with tempfile.TemporaryDirectory() as folder:
            skill = command_skill("C:\Windows\System32\cmd.exe")
            result = await CommandExecutor().execute(skill, {"argv": ["/c", "echo 中文输出"]},
                                                     context(Path(folder)), 20)
            self.assertIn("中文输出", result.outputs["stdout"])


class ExecutionTests(unittest.IsolatedAsyncioTestCase):
    async def test_dry_run_launches_nothing(self):
        with tempfile.TemporaryDirectory() as folder:
            skill = command_skill(sys.executable, args=["-c", "open(r'x.txt','w').write('boom')"])
            result = await CommandExecutor().execute(skill, {"dry_run": True}, context(Path(folder)), 30)
            self.assertTrue(result.outputs["dry_run"])
            self.assertIsNone(result.outputs["exit_code"])
            self.assertFalse((Path(folder) / "boom").exists())
            self.assertFalse((Path.cwd() / "x.txt").exists())

    async def test_real_run_captures_stdout_and_artifact(self):
        with tempfile.TemporaryDirectory() as folder:
            skill = command_skill(sys.executable)
            result = await CommandExecutor().execute(skill, {"argv": ["-c", "print('你好 workflow')"]},
                                                     context(Path(folder)), 30)
            self.assertEqual(result.status.value, "succeeded")
            self.assertEqual(result.outputs["exit_code"], 0)
            self.assertIn("你好 workflow", result.outputs["stdout"])
            self.assertEqual(len(result.artifacts), 1)
            self.assertIn("你好 workflow", Path(result.artifacts[0].uri).read_text(encoding="utf-8"))

    async def test_stdin_env_and_cwd_are_applied(self):
        with tempfile.TemporaryDirectory() as folder:
            code = ("import sys,os;"
                    "print(sys.stdin.read().strip() + os.environ['HUB_DEMO'] + os.path.basename(os.getcwd()))")
            skill = command_skill(sys.executable)
            result = await CommandExecutor().execute(
                skill, {"argv": ["-c", code], "stdin": "in:", "env": {"HUB_DEMO": "|env|"}, "cwd": folder},
                context(Path(folder)), 30)
            self.assertEqual(result.outputs["stdout"].strip(), f"in:|env|{Path(folder).name}")

    async def test_nonzero_exit_is_a_structured_failure(self):
        with tempfile.TemporaryDirectory() as folder:
            skill = command_skill(sys.executable)
            result = await CommandExecutor().execute(skill, {"argv": ["-c", "import sys;sys.exit(4)"]},
                                                     context(Path(folder)), 30)
            self.assertEqual(result.status.value, "failed")
            self.assertEqual(result.error.code.value, "executor_error")
            self.assertIn("4", result.error.message)
            self.assertEqual(result.outputs["exit_code"], 4)

    async def test_timeout_kills_the_process(self):
        with tempfile.TemporaryDirectory() as folder:
            skill = command_skill(sys.executable)
            result = await CommandExecutor().execute(
                skill, {"argv": ["-c", "import time;time.sleep(30)"]}, context(Path(folder)), 1.5)
            self.assertEqual(result.error.code.value, "timeout")

    async def test_missing_executable_reports_launch_failure(self):
        with tempfile.TemporaryDirectory() as folder:
            ghost = str(Path(folder) / "definitely-not-here.exe")
            skill = command_skill(ghost)
            result = await CommandExecutor().execute(skill, {}, context(Path(folder)), 5)
            self.assertEqual(result.status.value, "failed")
            self.assertIn("无法启动命令", result.error.message)

    async def test_output_is_capped(self):
        with tempfile.TemporaryDirectory() as folder:
            skill = command_skill(sys.executable)
            executor = CommandExecutor(max_output_bytes=500)
            code = "print('x'*9000)"
            result = await executor.execute(skill, {"argv": ["-c", code]}, context(Path(folder)), 30)
            self.assertTrue(result.outputs["truncated"])
            self.assertLessEqual(len(result.outputs["stdout"]), 500)


if __name__ == "__main__":
    unittest.main()
