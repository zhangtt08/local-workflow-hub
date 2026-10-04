"""Atomic discovery of local Skill contracts; no Skill is executed here."""

from __future__ import annotations

from pathlib import Path

import yaml

from .models import SkillDefinition


class DirectorySkillRegistry:
    def __init__(self, root: Path):
        self.root = root.resolve()
        self._skills: dict[tuple[str, str], SkillDefinition] = {}

    def refresh(self) -> int:
        found: dict[tuple[str, str], SkillDefinition] = {}
        manifests = list(self.root.glob("*/skill.yaml")) + list(self.root.glob("*/versions/*/skill.yaml"))
        for manifest in sorted(manifests):
            folder = manifest.parent.resolve()
            raw = yaml.safe_load(manifest.read_text(encoding="utf-8"))
            skill = SkillDefinition.model_validate(raw)
            skill_root = (self.root / skill.id).resolve()
            if skill_root.parent != self.root:
                raise ValueError(f"Skill folder escapes registry root: {skill_root}")
            if folder != skill_root and folder != skill_root / "versions" / skill.version:
                raise ValueError(f"Manifest location must match Skill id/version: {manifest}")
            entry = (folder / skill.executor.entry).resolve()
            if not entry.is_relative_to(folder) or not entry.is_file():
                raise ValueError(f"Invalid executor entry for {skill.id}: {skill.executor.entry}")
            key = (skill.id, skill.version)
            if key in found:
                raise ValueError(f"Duplicate Skill version: {key}")
            found[key] = skill
        self._skills = found
        return len(found)

    def get_version(self, skill_id: str, version: str) -> SkillDefinition | None:
        return self._skills.get((skill_id, version))

    def get(self, skill_id: str) -> SkillDefinition | None:
        versions = [s for (sid, _), s in self._skills.items() if sid == skill_id]
        return max(versions, key=lambda s: tuple(map(int, s.version.split("."))), default=None)

    def list(self) -> list[SkillDefinition]:
        return sorted(self._skills.values(), key=lambda s: (s.id, tuple(map(int, s.version.split(".")))))

    def search(self, query: str, top_k: int = 10, category: str | None = None) -> list[SkillDefinition]:
        terms = query.lower().split()
        matches = []
        for skill in self._skills.values():
            if category and skill.category != category:
                continue
            haystack = f"{skill.id} {skill.name} {skill.description} {' '.join(skill.tags)}".lower()
            score = sum(term in haystack for term in terms)
            if score or not terms:
                matches.append((score, skill))
        return [s for _, s in sorted(matches, key=lambda item: (-item[0], item[1].id))[:top_k]]
