"""Composition root: the only place that knows which adapters are live."""

from __future__ import annotations

import os
import sys
from dataclasses import dataclass, field
from pathlib import Path
from uuid import uuid4

from .catalog import ToolCatalog
from .command_executor import CommandExecutor
from .executors import ExecutorRouter, PythonExecutor
from .models import Permission, SkillDefinition
from .persistence import FileRunRepository, FileWorkflowRepository, JsonlEventBus
from .registry import DirectorySkillRegistry
from .runner import WorkflowRunner

PROJECT_ROOT = Path(__file__).resolve().parents[2]
# PyInstaller one-file: read-only shipped resources live in the unpacked bundle, not next to the exe.
BUNDLE_ROOT = Path(getattr(sys, "_MEIPASS", PROJECT_ROOT))

# A desktop app on the user's own machine may use everything the user may use; the catalogue
# still labels risk and can require approval per tool.
GRANTED_PERMISSIONS = {p for p in Permission}


def resource_dir(name: str) -> Path:
    """examples/ and friends: next to the project (or exe) first, then inside the bundle."""
    for candidate in (PROJECT_ROOT / name, BUNDLE_ROOT / name):
        if candidate.is_dir():
            return candidate
    return PROJECT_ROOT / name


def skills_root() -> Path:
    """Hand-written Skill packages live in the repo, or beside the exe when frozen."""
    override = os.environ.get("WORKFLOWHUB_SKILLS")
    return Path(override).resolve() if override else PROJECT_ROOT / "skills"


def data_root() -> Path:
    override = os.environ.get("WORKFLOWHUB_DATA")
    return Path(override).resolve() if override else PROJECT_ROOT / "data"


def static_root() -> Path | None:
    """Bundled UI: PyInstaller drops it next to the exe, development uses the Vite build."""
    candidates = []
    if getattr(sys, "frozen", False):
        candidates.append(Path(sys._MEIPASS) / "frontend" / "dist")  # type: ignore[attr-defined]
        candidates.append(Path(sys.executable).parent / "frontend" / "dist")
    candidates += [PROJECT_ROOT / "frontend" / "dist"]
    for candidate in candidates:
        if (candidate / "index.html").is_file():
            return candidate
    return None


class CompositeRegistry:
    """Hand-written Skill packages plus every enabled tool found on this machine."""

    def __init__(self, directory: DirectorySkillRegistry, catalog: ToolCatalog):
        self.directory = directory
        self.catalog = catalog
        self._cache: dict[str, SkillDefinition] = {}
        self.refresh()

    def refresh(self) -> int:
        self.directory.refresh()
        skills = {skill.id: skill for skill in self.directory.list()}
        for skill in self.catalog.enabled_skills():
            skills[skill.id] = skill
        self._cache = skills
        return len(skills)

    @property
    def count(self) -> int:
        return len(self._cache)

    def list(self) -> list[SkillDefinition]:
        return sorted(self._cache.values(), key=lambda s: (s.category, s.id))

    def get(self, skill_id: str) -> SkillDefinition | None:
        return self._cache.get(skill_id)

    def get_version(self, skill_id: str, version: str) -> SkillDefinition | None:
        skill = self._cache.get(skill_id)
        return skill if skill and skill.version == version else None

    def search(self, query: str, top_k: int = 20) -> list[SkillDefinition]:
        terms = [t for t in query.lower().split() if t]
        scored: list[tuple[int, SkillDefinition]] = []
        for skill in self._cache.values():
            hay = f"{skill.id} {skill.name} {skill.description} {' '.join(skill.tags)} {skill.category}".lower()
            score = sum(3 if term in hay else 0 for term in terms)
            if terms and score == 0:
                continue
            scored.append((score, skill))
        scored.sort(key=lambda item: (-item[0], item[1].id))
        return [skill for _, skill in scored[:top_k]]


@dataclass
class Hub:
    registry: CompositeRegistry
    catalog: ToolCatalog
    directory: DirectorySkillRegistry
    workflows: FileWorkflowRepository
    runs: FileRunRepository
    events: JsonlEventBus
    runner: WorkflowRunner
    executors: ExecutorRouter
    artifact_root: Path
    roots: dict[str, Path] = field(default_factory=dict)
    #: 每次启动一个身份；桌面端用它确认"应答的确实是我刚起的那个服务"
    instance: str = field(default_factory=lambda: uuid4().hex[:12])

    def version(self) -> str:
        from . import __version__

        return __version__


def override_used() -> bool:
    return bool(os.environ.get("WORKFLOWHUB_SKILLS"))


def seed_missing(source: Path, target: Path) -> None:
    """A packaged exe ships skills/; keep user-added folders, seed the ones that vanished."""
    import shutil

    for folder in source.iterdir():
        if folder.is_dir() and (folder / "skill.yaml").is_file():
            dest = target / folder.name
            if not dest.exists():
                shutil.copytree(folder, dest, ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))


def build_hub(root: Path | None = None) -> Hub:
    base = Path(root or data_root())
    skills = skills_root()
    bundled = resource_dir("skills")
    if override_used() and skills.is_dir() and bundled.is_dir():
        seed_missing(bundled, skills)
    directory = DirectorySkillRegistry(skills)
    catalog = ToolCatalog(base / "catalog")
    if not catalog.tools and os.environ.get("WORKFLOWHUB_AUTOSCAN", "1") != "0":
        # 首次运行（含打包后的 exe）必须先看到本机工具，而不是一个空目录
        catalog.scan()
    registry = CompositeRegistry(directory, catalog)

    executors = ExecutorRouter()
    executors.register("python", PythonExecutor(skills))
    executors.register("command", CommandExecutor(int(catalog.settings["max_output_bytes"])))

    workflows = FileWorkflowRepository(base / "workflows")
    runs = FileRunRepository(base / "runs")
    events = JsonlEventBus(base / "events.jsonl")
    artifacts = base / "artifacts"
    runner = WorkflowRunner(registry, executors, runs, events, artifacts, GRANTED_PERMISSIONS,
                            system_max_timeout=3600)
    hub = Hub(registry, catalog, directory, workflows, runs, events, runner, executors, artifacts,
              {"project": PROJECT_ROOT, "data": base, "skills": skills})
    catalog.on_change = registry.refresh
    return hub
