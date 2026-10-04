import asyncio
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from pydantic import ValidationError

from workflowhub.bindings import resolve_binding
from workflowhub.bootstrap import PROJECT_ROOT, build_hub
from workflowhub.dag import DAG
from workflowhub.errors import WorkflowHubError
from workflowhub.executors import ExecutorRouter, PythonExecutor
from workflowhub.models import (
    BindingType, ErrorCode, ExecutionError, InputBinding, NodeRun, NodeStatus, Permission,
    RetryPolicy, RunStatus, SkillDefinition, SkillResult, ValueSpec, ValueType,
    WorkflowDefinition, WorkflowEdge, WorkflowNode,
)
from workflowhub.persistence import FileRunRepository, FileWorkflowRepository, JsonlEventBus, atomic_replace
from workflowhub.ports import ExecutionContext
from workflowhub.registry import DirectorySkillRegistry
from workflowhub.runner import WorkflowRunner
from workflowhub.state import transition_node
from workflowhub.validator import WorkflowValidator


def example() -> WorkflowDefinition:
    return WorkflowDefinition.model_validate_json((PROJECT_ROOT / "examples" / "text_to_file.json").read_text(encoding="utf-8"))


class ContractTests(unittest.TestCase):
    def test_skill_validation(self):
        registry = DirectorySkillRegistry(PROJECT_ROOT / "skills")
        registry.refresh()
        raw = registry.get_version("echo", "1.0.0").model_dump()
        raw["version"] = "latest"
        with self.assertRaises(ValidationError):
            SkillDefinition.model_validate(raw)
        raw["version"] = "1.0.0"
        raw["unexpected"] = 1
        with self.assertRaises(ValidationError):
            SkillDefinition.model_validate(raw)

    def test_discovery_and_atomic_refresh(self):
        registry = DirectorySkillRegistry(PROJECT_ROOT / "skills")
        self.assertEqual(registry.refresh(), 3)
        self.assertEqual([s.id for s in registry.list()], ["echo", "file-writer", "text-transform"])
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            skill_dir = root / "echo"
            skill_dir.mkdir()
            (skill_dir / "handler.py").write_text("pass", encoding="utf-8")
            (skill_dir / "skill.yaml").write_text((PROJECT_ROOT / "skills" / "echo" / "skill.yaml").read_text(encoding="utf-8"), encoding="utf-8")
            local = DirectorySkillRegistry(root)
            self.assertEqual(local.refresh(), 1)
            older = skill_dir / "versions" / "0.9.0"
            older.mkdir(parents=True)
            (older / "handler.py").write_text('import json,sys\ndata=json.load(sys.stdin)\njson.dump({"outputs":{"text":data["text"]}},sys.stdout)', encoding="utf-8")
            (older / "skill.yaml").write_text((skill_dir / "skill.yaml").read_text(encoding="utf-8").replace("version: 1.0.0", "version: 0.9.0"), encoding="utf-8")
            self.assertEqual(local.refresh(), 2)
            self.assertIsNotNone(local.get_version("echo", "0.9.0"))
            old_result = asyncio.run(PythonExecutor(root).execute(
                local.get_version("echo", "0.9.0"), {"text": "old"},
                ExecutionContext("trace", "run", "node", root / "artifacts", frozenset()), 5,
            ))
            self.assertEqual(old_result.outputs["text"], "old")
            (skill_dir / "skill.yaml").write_text("id: echo\nversion: bad", encoding="utf-8")
            with self.assertRaises(ValidationError):
                local.refresh()
            self.assertIsNotNone(local.get_version("echo", "1.0.0"))

    def test_dag_cycle_order_and_ready(self):
        workflow = example()
        dag = DAG(workflow)
        self.assertEqual(dag.topological_order(), ["input", "transform", "write"])
        self.assertEqual(dag.ready_nodes(set()), ["input"])
        self.assertEqual(dag.ready_nodes({"input"}), ["transform"])
        workflow.edges.append(WorkflowEdge(source="write", target="input"))
        with self.assertRaisesRegex(ValueError, "cycle"):
            DAG(workflow)

    def test_input_binding(self):
        values = {"text": "hello"}
        outputs = {"a": {"text": "WORLD"}}
        context = {"trace_id": "t1"}
        self.assertEqual(resolve_binding(InputBinding(type=BindingType.LITERAL, value=42), values, outputs, context), 42)
        self.assertEqual(resolve_binding(InputBinding(type=BindingType.WORKFLOW_INPUT, name="text"), values, outputs, context), "hello")
        self.assertEqual(resolve_binding(InputBinding(type=BindingType.NODE_OUTPUT, node_id="a", field="text"), values, outputs, context), "WORLD")
        self.assertEqual(resolve_binding(InputBinding(type=BindingType.CONTEXT, name="trace_id"), values, outputs, context), "t1")

    def test_workflow_validation(self):
        registry = DirectorySkillRegistry(PROJECT_ROOT / "skills")
        registry.refresh()
        validator = WorkflowValidator(registry, {"python"}, {Permission.FILESYSTEM_WRITE})
        workflow = example()
        self.assertTrue(validator.validate(workflow).valid)
        workflow.nodes[1].inputs["text"] = InputBinding(type=BindingType.LITERAL, value=123)
        self.assertFalse(validator.validate(workflow).valid)
        workflow = example()
        workflow.edges.pop(0)
        self.assertIn("direct dependency edge", "; ".join(validator.validate(workflow).errors))
        self.assertFalse(WorkflowValidator(registry, {"python"}, set()).validate(example()).valid)

    def test_published_version_immutable(self):
        with tempfile.TemporaryDirectory() as folder:
            repo = FileWorkflowRepository(Path(folder))
            workflow = example()
            repo.save_version(workflow)
            workflow.name = "edited"
            with self.assertRaisesRegex(ValueError, "immutable"):
                repo.save_version(workflow)
            self.assertNotEqual(repo.get_version("text-to-file", 1).name, workflow.name)

    def test_node_state_machine(self):
        registry = DirectorySkillRegistry(PROJECT_ROOT / "skills")
        registry.refresh()
        node = NodeRun(id="run-node", node_id="echo", skill_id="echo", skill_version="1.0.0", skill_definition_snapshot=registry.get_version("echo", "1.0.0"))
        transition_node(node, NodeStatus.READY)
        transition_node(node, NodeStatus.RUNNING)
        transition_node(node, NodeStatus.SUCCEEDED)
        with self.assertRaisesRegex(ValueError, "Invalid Node transition"):
            transition_node(node, NodeStatus.RUNNING)


class RunnerTests(unittest.IsolatedAsyncioTestCase):
    async def test_successful_workflow_and_trace(self):
        with tempfile.TemporaryDirectory() as folder:
            hub = build_hub(Path(folder))
            run = await hub.runner.run(example(), {"text": "Hello"})
            self.assertEqual(run.status, RunStatus.SUCCEEDED)
            self.assertEqual(run.workflow_version_id, "text-to-file:1")
            self.assertEqual([n.status for n in run.node_runs.values()], [NodeStatus.SUCCEEDED] * 3)
            self.assertEqual(Path(run.outputs["file_path"]).read_text(encoding="utf-8"), "HELLO")
            self.assertEqual(hub.runs.get(run.id).definition_snapshot.name, example().name)
            self.assertEqual(len(run.node_runs["write"].result.artifacts), 1)
            events = (Path(folder) / "events.jsonl").read_text(encoding="utf-8")
            self.assertIn(run.trace_id, events)

    async def test_failed_node_is_structured(self):
        with tempfile.TemporaryDirectory() as folder:
            hub = build_hub(Path(folder))
            workflow = example()
            workflow.nodes[2].inputs["filename"] = InputBinding(type=BindingType.LITERAL, value="../outside.txt")
            run = await hub.runner.run(workflow, {"text": "hello"})
            self.assertEqual(run.status, RunStatus.FAILED)
            self.assertEqual(run.node_runs["write"].result.error.code, ErrorCode.INPUT_ERROR)
            self.assertEqual(run.node_runs["write"].skill_version, "1.0.0")
            self.assertFalse((Path(folder) / "outside.txt").exists())

    async def test_retry_policy(self):
        class Registry:
            def __init__(self, skill):
                self.skill = skill
            def get_version(self, skill_id, version):
                return self.skill if (skill_id, version) == ("echo", "1.0.0") else None
            def list(self):
                return [self.skill]

        class FlakyExecutor:
            def __init__(self):
                self.calls = 0
            async def execute(self, skill, inputs, context, timeout):
                self.calls += 1
                if self.calls == 1:
                    return SkillResult(status=NodeStatus.FAILED, error=ExecutionError(code=ErrorCode.TIMEOUT, message="temporary"))
                return SkillResult(status=NodeStatus.SUCCEEDED, outputs={"text": inputs["text"]})

        source = DirectorySkillRegistry(PROJECT_ROOT / "skills")
        source.refresh()
        skill = source.get_version("echo", "1.0.0").model_copy(update={"retry_policy": RetryPolicy(max_attempts=2, retry_on=[ErrorCode.TIMEOUT])})
        registry = Registry(skill)
        flaky = FlakyExecutor()
        router = ExecutorRouter()
        router.register("python", flaky)
        workflow = WorkflowDefinition(
            id="retry", name="Retry", version=1, inputs={"text": ValueSpec(type=ValueType.STRING)},
            outputs={"text": InputBinding(type=BindingType.NODE_OUTPUT, node_id="echo", field="text")},
            nodes=[WorkflowNode(id="echo", skill_id="echo", skill_version="1.0.0", inputs={"text": InputBinding(type=BindingType.WORKFLOW_INPUT, name="text")})],
        )
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            runner = WorkflowRunner(registry, router, FileRunRepository(root / "runs"), JsonlEventBus(root / "events.jsonl"), root / "artifacts", set())
            run = await runner.run(workflow, {"text": "ok"})
            self.assertEqual(run.status, RunStatus.SUCCEEDED)
            self.assertEqual(run.node_runs["echo"].attempt, 2)
            self.assertEqual(flaky.calls, 2)

    async def test_review_request_waits_instead_of_failing(self):
        class ReviewExecutor:
            async def execute(self, skill, inputs, context, timeout):
                return SkillResult(
                    status=NodeStatus.WAITING_REVIEW,
                    outputs={"text": inputs["text"]},
                    error=ExecutionError(code=ErrorCode.USER_REVIEW_REQUIRED, message="Check the text"),
                )

        registry = DirectorySkillRegistry(PROJECT_ROOT / "skills")
        registry.refresh()
        router = ExecutorRouter()
        router.register("python", ReviewExecutor())
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            runner = WorkflowRunner(registry, router, FileRunRepository(root / "runs"), JsonlEventBus(root / "events.jsonl"), root / "artifacts", {Permission.FILESYSTEM_WRITE})
            run = await runner.run(example(), {"text": "review me"})
            self.assertEqual(run.status, RunStatus.WAITING)
            self.assertEqual(run.node_runs["input"].status, NodeStatus.WAITING_REVIEW)
            self.assertEqual(run.node_runs["transform"].status, NodeStatus.PENDING)
            self.assertEqual(len(run.review_tasks), 1)
            self.assertIsNone(run.finished_at)


class AtomicWriteTests(unittest.TestCase):
    """Windows 上的瞬时 rename 锁不能把已经成功的命令记成失败（真机踩过 WinError 5）。"""

    def test_atomic_replace_retries_transient_lock(self):
        with tempfile.TemporaryDirectory() as folder:
            tmp = Path(folder) / "a.tmp"
            target = Path(folder) / "a.json"
            tmp.write_text("{}", encoding="utf-8")
            attempts = []
            real = Path.replace

            def flaky(self, dst):
                attempts.append(1)
                if len(attempts) < 3:
                    raise PermissionError(13, "占用")
                return real(self, dst)

            with mock.patch.object(Path, "replace", flaky):
                atomic_replace(tmp, target, schedule=(0,) * 5)
            self.assertEqual(len(attempts), 3, "应当重试而不是第一次就放弃")
            self.assertTrue(target.is_file() and not tmp.exists())

    def test_atomic_reports_permanent_lock_without_losing_payload(self):
        with tempfile.TemporaryDirectory() as folder:
            tmp = Path(folder) / "a.tmp"
            target = Path(folder) / "a.json"
            tmp.write_text("{}", encoding="utf-8")
            with mock.patch.object(Path, "replace", side_effect=PermissionError(13, "占用")):
                with self.assertRaises(WorkflowHubError) as caught:
                    atomic_replace(tmp, target, schedule=(0, 0))
            self.assertTrue(caught.exception.detail.retryable)
            self.assertIn("一直被占用", caught.exception.detail.message)
            self.assertTrue(tmp.is_file(), "写不进去时临时文件必须留着，别把结果丢掉")


if __name__ == "__main__":
    unittest.main()
