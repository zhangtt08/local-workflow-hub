"""Durable tool catalogue: scan results + per-tool state + audit of Agent invocations.

The catalogue is what an Agent reads. Every discovered candidate is turned into a normal
``SkillDefinition`` so the existing validator, runner and executors keep working unchanged.
"""

from __future__ import annotations

import json
import re
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable
from uuid import uuid4

from .discovery import DEFAULT_ROOTS, Candidate, probe_version, run_scan, slug
from .errors import WorkflowHubError
from .persistence import atomic_replace
from .redaction import redact, redact_event
from .models import (
    ErrorCode,
    Permission,
    RiskLevel,
    SkillDefinition,
    ValueType,
    ValueSpec,
)

SCHEMA_VERSION = 3
ID_RE = re.compile(r"^[a-z][a-z0-9-]{1,63}$")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _atomic_write(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8")
    atomic_replace(tmp, path)


#: 交付默认值。两条都是从"能用"改成"默认安全"，改回来的开关写在 README「安全边界」一节，
#: 只在那一处解释，别处不再复述：
#: * ``approval_mode=True`` —— 高风险工具先出审批单，人在窗口里点了头才真跑；
#: * ``high_risk_enabled=False`` —— 扫描到的/手动登记的高风险命令默认**不可调用**，
#:   要用先在设置里打开（`POST /api/v1/tools` 登记的那类任意命令就落在这一档里）。
DEFAULT_SETTINGS: dict[str, Any] = {
    "scan_roots": [str(p) for p in DEFAULT_ROOTS],
    "approval_mode": True,
    "high_risk_enabled": False,
    "max_output_bytes": 200_000,
    "default_timeout_seconds": 120,
    # 并发是明说的策略而不是巧合：默认允许同一个工具被同时调用（这台机器上多数命令互不相干）。
    # 会在同一个目录里写同一批文件的工具，在详情页勾上 exclusive 让它们排队。
    "allow_concurrent": True,
    "telemetry": False,
}

#: 审计一次最多读这么多字节；超过就只索引尾部并在 stats 里如实标出来，
#: 免得一次几个 GB 的 audit.jsonl 把启动与健康检查拖死（这条是这个项目自己踩过的）。
AUDIT_SCAN_LIMIT = 32_000_000
#: 审计文件的**硬上限**：到顶就轮转（改名留底 + 从头再写）。不轮转的话这台机器上
#: 实测会一路涨到几百 MB，而每一次读都要按上面的窗口重算，界面上的"最近活动"越来越慢。
AUDIT_ROTATE_AT = 32_000_000
#: 轮转后留在磁盘上的历史份数（audit.jsonl.1 是上一轮，.2 更早……）
AUDIT_KEEP = 3
#: recent_activity 在内存里保留的行数
AUDIT_ROWS_KEPT = 2_000


class ToolCatalog:
    """Single source of truth for what an Agent may call on this machine."""

    def __init__(self, root: Path, on_change: Callable[[], None] | None = None):
        self.root = Path(root)
        self.path = self.root / "catalog.json"
        self.audit_path = self.root / "audit.jsonl"
        self.on_change = on_change
        self._lock = threading.RLock()
        self.settings: dict[str, Any] = dict(DEFAULT_SETTINGS)
        self.tools: dict[str, dict[str, Any]] = {}
        self.state: dict[str, dict[str, Any]] = {}
        self.approvals: dict[str, dict[str, Any]] = {}
        self.last_scan: dict[str, Any] = {}
        self.scan_errors: list[str] = []
        self._scan_state: dict[str, Any] = {"running": False, "phase": "", "found": 0}
        # ── 审计账本的内存视图 ────────────────────────────────────────────────
        # 一条调用只读它自己那几十字节（_audit_pos 是已消费到的位置），计数却是全量的；
        # 行数只留最近 AUDIT_ROWS_KEPT 条，避免流水涨到几十 MB 时把界面拖死。
        self._audit_lock = threading.RLock()
        self._audit_stamp: tuple[int, int] | None = None
        self._audit_rows_buf: list[dict[str, Any]] = []
        self._audit_index_buf: dict[str, tuple[int, str | None]] = {}
        self._audit_pos = 0
        # 跨轮转的累计计数（当前那一本的计数在 _audit_index_buf 里，两份相加才是总次数）
        self._audit_counts_path = self.root / "audit.counts.json"
        self._audit_counts: dict[str, tuple[int, str | None]] = {}
        self._audit_counts_loaded = False
        # 必须在构造里就初始化：_audit_reset() 只在"文件不存在/被截断"那两条分支上才会跑到。
        # 真实机器上 data/audit.jsonl 是一直存在的，于是第一次 _audit_tail() 直接读这个属性就
        # AttributeError —— 测试里用临时空目录躲过了，凡是读调用计数的端点（含 /api/agent/tools）
        # 在现网全部 500。判据同一条：凡是被 _audit_reset 设过的字段，构造里也要设一遍。
        self._audit_resync = False
        self._audit_windowed = False
        self.load()

    # ── persistence ────────────────────────────────────────────────────────────
    def load(self) -> None:
        with self._lock:
            if not self.path.is_file():
                return
            try:
                raw = json.loads(self.path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                return
            if raw.get("schema_version") != SCHEMA_VERSION:
                self.scan_errors.append(f"旧目录格式（v{raw.get('schema_version')}）已忽略，请重新扫描")
                self.tools, self.state = {}, {}
                return
            self.tools = raw.get("tools", {})
            self.state = raw.get("state", {})
            self.settings = {**DEFAULT_SETTINGS, **raw.get("settings", {})}
            self.approvals = {k: v for k, v in raw.get("approvals", {}).items() if v.get("status") == "pending"}
            self.last_scan = raw.get("last_scan", {})

    def save(self) -> None:
        with self._lock:
            _atomic_write(self.path, {
                "schema_version": SCHEMA_VERSION,
                "updated_at": _now(),
                "settings": self.settings,
                "last_scan": self.last_scan,
                "tools": self.tools,
                "state": self.state,
                "approvals": self.approvals,
            })

    # ── scanning ───────────────────────────────────────────────────────────────
    @property
    def scan_state(self) -> dict[str, Any]:
        return dict(self._scan_state)

    def scan(self, roots: list[str] | None = None, wait: bool = True) -> dict[str, Any]:
        """Rescan the machine. Keeps per-tool state; drops tools that disappeared."""
        with self._lock:
            if self._scan_state["running"]:
                return {"started": False, "reason": "扫描已在进行中"}
            self._scan_state = {"running": True, "phase": "准备", "found": 0}
        target_roots = [Path(r) for r in (roots or self.settings["scan_roots"])]

        def work() -> dict[str, Any]:
            errors: list[str] = []
            found: list[Candidate] = []
            for phase, steps in (("可执行程序与系统命令", ["executables", "system"]), ("已安装应用", ["apps"]),
                                 ("脚本工具", ["scripts"]), ("项目工作流与 Skill 包", ["projects", "skills"])):
                self._scan_state.update(phase=phase)
                try:
                    found.extend(run_scan(target_roots, steps=steps))
                except Exception as exc:  # a bad folder must not kill the scan
                    errors.append(f"{phase}: {type(exc).__name__}: {exc}")
                self._scan_state.update(found=len(found))
            with self._lock:
                fresh = {}
                for cand in found:
                    payload = cand.to_dict()
                    payload["found_at"] = _now()
                    payload["id"] = cand.key if ID_RE.match(cand.key) else f"tool-{slug(cand.key)}"
                    fresh[payload["id"]] = payload
                stale = set(self.tools) - set(fresh)
                manual = {k: v for k, v in self.tools.items() if v.get("kind") == "custom" or k.startswith("custom-")}
                self.tools = {**fresh, **manual}
                for key in stale:
                    self.state.pop(key, None)
                self.scan_errors = errors
                self.last_scan = {"at": _now(), "count": len(self.tools), "new": len(fresh), "gone": len(stale)}
                self.save()
                self._scan_state = {"running": False, "phase": "完成", "found": len(self.tools)}
                if self.on_change:
                    self.on_change()
                return dict(self.last_scan)

        if wait:
            return work()
        threading.Thread(target=work, daemon=True, name="tool-scan").start()
        return {"started": True, "count": len(self.tools)}

    # ── reading ────────────────────────────────────────────────────────────────
    def state_for(self, key: str) -> dict[str, Any]:
        return self.state.setdefault(key, {"enabled": True, "starred": False, "notes": "", "alias": "",
                                           "exclusive": False})

    def is_enabled(self, key: str) -> bool:
        tool = self.tools.get(key)
        if tool is None:
            return False
        state = self.state_for(key)
        if not state.get("enabled", True):
            return False
        if tool.get("risk") == "high" and not self.settings["high_risk_enabled"]:
            return False
        return bool(tool.get("command"))

    def record(self, key: str) -> dict[str, Any] | None:
        tool = self.tools.get(key)
        if tool is None:
            return None
        state = self.state_for(key)
        merged = {**tool, "enabled": self.is_enabled(key), "starred": state.get("starred", False),
                  "notes": state.get("notes", ""), "alias": state.get("alias", ""), "key": key,
                  "exclusive": bool(state.get("exclusive", False)),
                  "invocable": bool(tool.get("command")),
                  "blueprint": not bool(tool.get("command")),
                  "call_hint": (None if tool.get("command") else
                                "蓝图条目：本身不是一条命令，可在详情页接入为本地 Skill 或修正入口")}
        merged["invocations"] = self.invocation_count(key)
        merged["last_invoked"] = self.last_invocation(key)
        return merged

    def list(self, query: str = "", category: str | None = None, kind: str | None = None,
             risk: str | None = None, enabled: bool | None = None, starred: bool | None = None,
             limit: int = 60, offset: int = 0) -> dict[str, Any]:
        with self._lock:
            terms = [t for t in query.lower().split() if t]
            rows: list[tuple[int, dict[str, Any]]] = []
            for key in self.tools:
                row = self.record(key)
                if row is None:
                    continue
                if category and row["category"] != category:
                    continue
                if kind and row["kind"] != kind:
                    continue
                if risk and row["risk"] != risk:
                    continue
                if enabled is not None and bool(row["enabled"]) != enabled:
                    continue
                if starred and not row["starred"]:
                    continue
                score = 0
                if terms:
                    hay = f"{row['title']} {row['id']} {row['description']} {' '.join(row['tags'])} {row['category']}".lower()
                    score = sum(3 if t in hay else 0 for t in terms)
                    if not score:
                        continue
                    if row["title"].lower().startswith(terms[0]):
                        score += 5
                if row["starred"]:
                    score += 2
                if row["invocations"]:
                    score += 1
                rows.append((score, row))
            rows.sort(key=lambda item: (-item[0], item[1]["category"], item[1]["title"].lower()))
            total = len(rows)
            page = [row for _, row in rows[offset:offset + max(1, min(limit, 300))]]
            return {"total": total, "offset": offset, "limit": limit, "items": page,
                    "facets": self.facets()}

    def facets(self) -> dict[str, Any]:
        """目录概览。只碰内存里的 tools/state —— 这里绝不能走 record()，
        否则每条目都会拖一次磁盘与审计统计，1,500 条时健康检查会慢到被判启动失败。"""
        cats: dict[str, int] = {}
        kinds: dict[str, int] = {}
        risks: dict[str, int] = {}
        enabled = 0
        starred = 0
        for key, tool in self.tools.items():
            cats[tool["category"]] = cats.get(tool["category"], 0) + 1
            kinds[tool["kind"]] = kinds.get(tool["kind"], 0) + 1
            risks[tool["risk"]] = risks.get(tool["risk"], 0) + 1
            if self.is_enabled(key):
                enabled += 1
            if (self.state.get(key) or {}).get("starred", False):
                starred += 1
        return {"categories": cats, "kinds": kinds, "risks": risks,
                "enabled": enabled, "starred": starred, "total": len(self.tools)}

    # ── writes ─────────────────────────────────────────────────────────────────
    def set_flag(self, key: str, flag: str, value: bool) -> dict[str, Any]:
        if key not in self.tools:
            raise KeyError(key)
        with self._lock:
            self.state_for(key)[flag] = bool(value)
            self.save()
        return self.record(key) or {}

    def set_note(self, key: str, note: str, alias: str | None = None) -> dict[str, Any]:
        if key not in self.tools:
            raise KeyError(key)
        with self._lock:
            self.state_for(key)["notes"] = note[:2000]
            if alias is not None:
                cleaned = alias.strip()
                if cleaned and not ID_RE.match(cleaned):
                    raise ValueError("别名只能用小写字母、数字和连字符")
                self.state_for(key)["alias"] = cleaned
            self.save()
        return self.record(key) or {}

    def add_custom(self, payload: dict[str, Any]) -> dict[str, Any]:
        name = str(payload.get("name") or "").strip()
        command = [str(x) for x in (payload.get("command") or [])]
        if not name or len(command) < 1:
            raise ValueError("自定义工具需要名称和至少一个命令片段")
        key = f"custom-{slug(name)}-{uuid4().hex[:6]}"
        candidate = Candidate(
            key=key, title=name, kind="custom", command=command,
            source=str(payload.get("source") or "手动登记"), category=str(payload.get("category") or "自定义工具"),
            tags=["custom"] + [str(t) for t in (payload.get("tags") or [])],
            description=str(payload.get("description") or f"手动登记的命令：{' '.join(command)[:120]}")[:220],
            cwd=payload.get("cwd") or None, risk=str(payload.get("risk") or "medium"),
            permissions=["shell"], notes=["手动登记的工具，WorkflowHub 无法验证其行为"],
        )
        with self._lock:
            self.tools[key] = {**candidate.to_dict(), "id": key, "found_at": _now()}
            self.state_for(key)["enabled"] = True
            self.save()
        return self.record(key) or {}

    def remove(self, key: str) -> None:
        with self._lock:
            if self.tools.get(key, {}).get("kind") != "custom":
                raise ValueError("只能删除手动登记的工具；扫描到的工具可通过禁用隐藏")
            self.tools.pop(key, None)
            self.state.pop(key, None)
            self.save()

    def adopt_skill_package(self, key: str, skills_root: Path) -> dict[str, Any]:
        """Copy a discovered ``skills/<id>/`` package into this project so the Python executor can run it."""
        import shutil

        tool = self.tools.get(key)
        if tool is None:
            raise KeyError(key)
        source = Path(str(tool.get("loc") or "")).parent
        if not (source / "skill.yaml").is_file():
            raise WorkflowHubError(ErrorCode.INPUT_ERROR, f"{source} 下没有 skill.yaml")
        target_id = slug(source.name)
        dest = (skills_root / target_id).resolve()
        if not dest.is_relative_to(skills_root.resolve()):
            raise WorkflowHubError(ErrorCode.PERMISSION_ERROR, "目标目录越界")
        if dest.exists():
            raise WorkflowHubError(ErrorCode.VALIDATION_ERROR,
                                   f"skills/{target_id} 已存在，接入会覆盖", {"target": str(dest)})
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copytree(source, dest, ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
        return {"id": target_id, "path": str(dest), "source": str(source)}

    def update_settings(self, patch: dict[str, Any]) -> dict[str, Any]:
        with self._lock:
            for k, v in patch.items():
                if k not in DEFAULT_SETTINGS:
                    continue                      # 未知键由 API 如实报出来，不在这里悄悄收下
                if k == "scan_roots":
                    v = [str(Path(str(x))) for x in v if str(x).strip()]
                if k == "max_output_bytes":
                    v = max(2_000, min(int(v), 2_000_000))
                if k == "default_timeout_seconds":
                    v = max(1, min(float(v), 3600))
                if isinstance(DEFAULT_SETTINGS[k], bool):
                    v = bool(v)
                self.settings[k] = v
            self.save()
            return dict(self.settings)

    def probe(self, key: str) -> dict[str, Any]:
        tool = self.tools.get(key)
        if tool is None:
            raise KeyError(key)
        command = [str(x) for x in tool.get("command") or []]
        if not command:
            return {"ok": False, "output": "", "reason": "该条目没有可执行命令"}
        result = probe_version(command)
        with self._lock:
            self.tools[key]["probe"] = {**result, "at": _now()}
            self.save()
        return {"ok": bool(result["probe_flag"]), **result, "command_line": " ".join(command)}

    # ── Skill contract view ────────────────────────────────────────────────────
    def to_skill(self, key: str) -> SkillDefinition:
        tool = self.tools.get(key)
        if tool is None:
            raise KeyError(key)
        if not tool.get("command"):
            raise WorkflowHubError(ErrorCode.INPUT_ERROR,
                                   f"「{tool.get('title')}」是蓝图条目，没有可执行入口",
                                   {"kind": tool.get("kind"), "hint": "接入为 Skill 包或手动登记命令后再调用"})
        state = self.state_for(key)
        name = state.get("alias") or slug(tool["title"]) or tool["id"]
        args = tool.get("args") or []
        inputs: dict[str, ValueSpec] = {
            "argv": ValueSpec(type=ValueType.ARRAY, required=False, description="按顺序传给命令的参数列表（原样，不经过 shell）"),
            "flags": ValueSpec(type=ValueType.OBJECT, required=False, description="具名参数；脚本类工具会按识别到的参数风格自动拼装"),
            "stdin": ValueSpec(type=ValueType.STRING, required=False, description="写入进程标准输入的内容"),
            "cwd": ValueSpec(type=ValueType.STRING, required=False, description="工作目录，默认使用工具登记的目录"),
            "env": ValueSpec(type=ValueType.OBJECT, required=False, description="额外环境变量（只增不改系统变量）"),
            "timeout_seconds": ValueSpec(type=ValueType.NUMBER, required=False, description="超时秒数"),
            "dry_run": ValueSpec(type=ValueType.BOOLEAN, required=False, description="为真时只返回将要执行的命令行，不实际运行"),
        }
        for spec in args:
            raw = str(spec.get("name") or "").strip()
            token = re.sub(r"^-+", "", raw)
            if not re.fullmatch(r"[A-Za-z0-9_-]{1,40}", token):
                token = slug(token)
            if not token or token in inputs:
                continue
            kind = {"integer": ValueType.INTEGER, "number": ValueType.NUMBER, "boolean": ValueType.BOOLEAN,
                    "array": ValueType.ARRAY}.get(str(spec.get("kind")), ValueType.STRING)
            note = str(spec.get("description") or "来自源码的命令行参数")
            if spec.get("required"):
                # 必填由命令自己判定：WorkflowHub 不拦 --help 这类合法调用
                note = f"{note}（脚本要求必填，缺失时命令会报错）"
            inputs[token] = ValueSpec(type=kind, required=False, description=note[:200])
        permissions = []
        for item in tool.get("permissions") or []:
            try:
                permissions.append(Permission(item))
            except ValueError:
                continue
        skill_id = name if ID_RE.match(name) else f"tool-{slug(tool['id'])}"
        return SkillDefinition(
            id=skill_id if ID_RE.match(skill_id) else "tool-unnamed",
            name=tool["title"],
            version="1.0.0",
            description=(state.get("notes") or tool["description"] or tool["title"])[:600],
            category=tool["category"],
            tags=tool.get("tags") or [],
            inputs=inputs,
            outputs={
                "exit_code": ValueSpec(type=ValueType.INTEGER, required=False, description="进程退出码"),
                "stdout": ValueSpec(type=ValueType.STRING, required=False, description="标准输出（截断到上限）"),
                "stderr": ValueSpec(type=ValueType.STRING, required=False, description="标准错误"),
                "command_line": ValueSpec(type=ValueType.STRING, required=False, description="实际执行的命令行"),
                "cwd": ValueSpec(type=ValueType.STRING, required=False, description="执行时的工作目录"),
                "duration_ms": ValueSpec(type=ValueType.INTEGER, required=False, description="耗时毫秒"),
                "truncated": ValueSpec(type=ValueType.BOOLEAN, required=False, description="输出是否被截断"),
                "dry_run": ValueSpec(type=ValueType.BOOLEAN, required=False, description="是否为只预览未执行"),
                "timeout_seconds": ValueSpec(type=ValueType.NUMBER, required=False, description="本次生效的超时"),
            },
            executor={
                "type": "command",
                "entry": (tool.get("command") or [""])[0],
                "args": (tool.get("command") or [])[1:],
                "cwd": tool.get("cwd"),
                "language": tool.get("language"),
            },
            permissions=permissions or [Permission.SHELL],
            timeout_seconds=float(self.settings["default_timeout_seconds"]),
            risk_level=RiskLevel(tool.get("risk", "low")),
            enabled=self.is_enabled(key),
            metadata={
                "catalog_key": tool["id"],
                "kind": tool["kind"],
                "source": tool.get("source"),
                "path": tool.get("loc"),
                "invocations": self.invocation_count(key),
                "probe": (tool.get("probe") or {}).get("output", ""),
                "arg_specs": args,
                "required_args": [str(a.get("name")) for a in args if a.get("required")],
                "flag_style": {"ps1": "single-dash", "py": "double-dash"}.get(str(tool.get("language")), "double-dash"),
            },
        )

    def enabled_skills(self) -> list[SkillDefinition]:
        out = []
        for key in list(self.tools):
            if not self.is_enabled(key):
                continue
            try:
                out.append(self.to_skill(key))
            except Exception:  # a malformed record must not blank the whole catalogue
                continue
        return out

    def agent_tool_schema(self, only_enabled: bool = True) -> list[dict[str, Any]]:
        """OpenAI / DeepSeek / Anthropic style function schemas, ready to paste into an agent."""
        tools = []
        for key in list(self.tools):
            if only_enabled and not self.is_enabled(key):
                continue
            try:
                skill = self.to_skill(key)
            except (WorkflowHubError, Exception):
                continue
            properties: dict[str, Any] = {}
            for field_name, spec in skill.inputs.items():
                entry: dict[str, Any] = {"type": spec.type.value}
                if spec.type == ValueType.ARRAY:
                    entry["items"] = {"type": "string"}
                if spec.type == ValueType.OBJECT:
                    entry["additionalProperties"] = True
                if spec.description:
                    entry["description"] = spec.description
                if spec.type == ValueType.BOOLEAN:
                    entry["type"] = "boolean"
                properties[field_name] = entry
            tools.append({
                "type": "function",
                "function": {
                    "name": skill.id.replace("-", "_"),
                    "description": f"[{skill.risk_level.value}] {skill.name} — {skill.description}"[:900],
                    "parameters": {"type": "object", "properties": properties,
                                   "required": [k for k, v in skill.inputs.items() if v.required]},
                },
            })
        return tools

    # ── audit + approvals ──────────────────────────────────────────────────────
    def audit(self, event: dict[str, Any]) -> None:
        """写一条审计：**先脱敏再落盘**，到顶就轮转。

        审计文件是给人读的，也会从 /api/v1/activity 与工具详情页返回；env 的值与密钥形状的参数
        只该出现在真正执行的那一份里，不该出现在账本里。

        轮转（第 4 号 NICE：以前这本账只增不减，实测涨到几百 MB 后每次读都要按窗口重算）：
        写到 `AUDIT_ROTATE_AT` 就改名留底、从头再写，最多留 `AUDIT_KEEP` 份。
        **计数不会因为轮转而归零** —— 轮转前把当前文件的每工具计数并进
        `audit.counts.json`（跨轮转的累计值），`invocation_count()` 读的是两份之和。
        """
        record = redact_event({"at": _now(), **event})
        self.audit_path.parent.mkdir(parents=True, exist_ok=True)
        line = json.dumps(record, ensure_ascii=False, default=str) + "\n"
        with self._audit_lock:
            with self.audit_path.open("a", encoding="utf-8") as stream:
                stream.write(line)
            try:
                oversized = self.audit_path.stat().st_size >= AUDIT_ROTATE_AT
            except OSError:
                oversized = False
            if oversized:
                self._audit_rotate_locked()

    def _audit_rotate_locked(self) -> None:
        """轮转审计文件。调用方必须已持有 `_audit_lock`。"""
        # 先把当前这一本的计数并进累计值，再动磁盘 —— 顺序反了就把这一段历史数丢了
        totals = self._audit_rotated_totals()
        for tool, (count, at) in self._audit_index().items():
            previous, last = totals.get(tool, (0, None))
            totals[tool] = (previous + count, max(str(at or ""), str(last or "")) or None)
        self._audit_counts_path.write_text(json.dumps(totals, ensure_ascii=False), encoding="utf-8")
        for index in range(AUDIT_KEEP, 0, -1):        # .2 <- .1 <- 当前那份
            source = self.audit_path if index == 1 else self.audit_path.with_suffix(f".jsonl.{index - 1}")
            target = self.audit_path.with_suffix(f".jsonl.{index}")
            if target.exists():
                target.unlink()
            if source.exists():
                source.replace(target)
        self._audit_reset()                            # 内存视图跟着归零，计数走 totals
        self._audit_counts_loaded = True

    def _audit_rotated_totals(self) -> dict[str, tuple[int, str | None]]:
        """`audit.counts.json` 里"已经被轮转走的那几本"的累计计数。"""
        if self._audit_counts_loaded:
            return self._audit_counts
        self._audit_counts_loaded = True
        try:
            raw = json.loads(self._audit_counts_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            raw = {}
        self._audit_counts = {str(k): (int(v[0] or 0), v[1] if len(v) > 1 else None)
                              for k, v in raw.items() if isinstance(v, (list, tuple))}
        return self._audit_counts

    def _audit_rows(self, limit: int = 400) -> list[dict[str, Any]]:
        return list(reversed(self._audit_tail()))[:limit]

    def _audit_tail(self) -> list[dict[str, Any]]:
        """审计尾部 + 每工具统计：按 (mtime, size) 判脏，脏了只读**新增的那几个字节**。

        两笔账都要算对：

        ① 目录 1,500 条时，每条工具各重读一遍审计文件会把 `/health` 拖到桌面端判定"启动失败"
           （真机踩过，`tests/test_catalog.py` 里有性能门禁）；
        ② 只数最后 400 行，"调用次数"就只是最近窗口而不是总次数 —— 列表里写着 ×N 就得是数出来的 N。
           现在一次调用只多读它那一条记录，计数仍然是全量的，内存里的行数另设上限。
        """
        with self._audit_lock:
            try:
                stat = self.audit_path.stat()
            except OSError:
                self._audit_reset()
                return []
            stamp = (int(stat.st_mtime_ns), int(stat.st_size))
            if self._audit_stamp == stamp:
                return self._audit_rows_buf

            start = self._audit_pos
            if stat.st_size < start:            # 文件被换过/截断过：整本重数
                self._audit_reset()
                start = 0
            if start == 0 and stat.st_size > AUDIT_SCAN_LIMIT:
                # 一上来就几十 MB：为了一次计数读完不值当，退到尾部窗口并如实标 windowed
                start = stat.st_size - AUDIT_SCAN_LIMIT
                self._audit_windowed = True
                # 窗口起点落在文件中间，那一行是半截的：不丢掉就会被当成一条真记录，
                # 计数与"最后一次调用时间"都会错上一条。续读分支不需要丢（pos 停在行首）。
                self._audit_resync = True
            new_rows, leftover = self._audit_read_forward(start, stat.st_size, drop_first=self._audit_resync)
            self._audit_pos = max(start, stat.st_size - leftover)
            self._audit_resync = False          # 现在停在行首了，下一次续读不用再丢半行
            self._audit_stamp = stamp
            for row in new_rows:                # 文件顺序旧→新，末次时间就是最后一次赋值
                self._audit_rows_buf.append(row)
                tool = row.get("tool")
                if tool:
                    count, _ = self._audit_index_buf.get(tool, (0, None))
                    self._audit_index_buf[tool] = (count + 1, row.get("at"))
            del self._audit_rows_buf[:-AUDIT_ROWS_KEPT]   # 只限内存里的行数，计数不受影响
            return self._audit_rows_buf

    def _audit_read_forward(self, start: int, size: int, drop_first: bool = False
                            ) -> tuple[list[dict[str, Any]], int]:
        """读 [start, size) 里的**完整行**，返回 (记录, 还没写完的尾部字节数)。

        按字节走、不按字符：审计是并发追加的，一条记录可能正被写到一半。把半行数进次数、
        或者把它算成已消费的位置，账本就会悄悄错一位并且再也对不上。
        ``drop_first`` 只在从文件中间起读时为真（那一行是半截的）；续读时位置本来就停在行首。
        """
        if size <= start:
            return [], 0
        with self.audit_path.open("rb") as stream:
            stream.seek(start)
            chunk = stream.read(size - start)
        if drop_first:
            cut = chunk.find(b"\n")
            if cut < 0:
                return [], len(chunk)
            chunk = chunk[cut + 1:]
        leftover = 0
        if chunk and not chunk.endswith(b"\n"):
            last = chunk.rfind(b"\n")
            leftover = len(chunk) - (last + 1) if last >= 0 else len(chunk)
            chunk = chunk[:last + 1] if last >= 0 else b""
        out: list[dict[str, Any]] = []
        for line in chunk.decode("utf-8", errors="replace").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                out.append(json.loads(line))
            except json.JSONDecodeError:
                continue
        return out, leftover

    def _audit_reset(self) -> None:
        self._audit_stamp = None
        self._audit_rows_buf = []
        self._audit_index_buf = {}
        self._audit_pos = 0
        self._audit_resync = False
        self._audit_windowed = False

    def _audit_index(self) -> dict[str, tuple[int, str | None]]:
        self._audit_tail()
        return self._audit_index_buf

    def audit_stats(self) -> dict[str, Any]:
        """审计这本账本身：多大、内存里留了几行、计数是不是只看了尾部、轮转过没有。"""
        rows = self._audit_tail()
        try:
            size = int(self.audit_path.stat().st_size)
        except OSError:
            size = 0
        archived = [str(p.name) for p in self.root.glob("audit.jsonl.*")]
        carried = self._audit_rotated_totals()
        return {"bytes": size, "rows_in_memory": len(rows), "tools_counted": len(self._audit_index_buf),
                "windowed": bool(self._audit_windowed),
                "rotate_at_bytes": AUDIT_ROTATE_AT, "rotated_archives": sorted(archived),
                "tools_counted_in_archives": len(carried),
                "counts_file": str(self._audit_counts_path),
                "note": ("审计文件超过 32MB，计数只覆盖尾部窗口" if self._audit_windowed
                         else "调用次数 = 当前这本 + 已轮转那几本的累计值")}

    def invocation_count(self, key: str) -> int:
        current = self._audit_index().get(key, (0, None))[0]
        return current + self._audit_rotated_totals().get(key, (0, None))[0]

    def last_invocation(self, key: str) -> str | None:
        current = self._audit_index().get(key, (0, None))[1]
        carried = self._audit_rotated_totals().get(key, (0, None))[1]
        return max(str(current or ""), str(carried or "")) or None

    def rotated_totals(self) -> dict[str, tuple[int, str | None]]:
        """已被轮转走的那几本里的累计计数（测试与 stats 读这一份）。"""
        return dict(self._audit_rotated_totals())

    def recent_activity(self, limit: int = 40, tool: str | None = None, actor: str | None = None,
                        event_type: str | None = None) -> list[dict[str, Any]]:
        """最近的审计流水。企业用法是"查某把工具/某个调用方最近干了什么"，
        所以过滤在取数之后、截断之前做 —— 不然翻 40 条找 1 条等于没给。"""
        if tool or actor or event_type:
            rows = [r for r in reversed(self._audit_tail())
                    if (not tool or r.get("tool") == tool)
                    and (not actor or r.get("actor") == actor)
                    and (not event_type or r.get("type") == event_type)]
        else:
            rows = self._audit_rows(max(limit, 40))
        return rows[:max(limit, 1)]

    def needs_approval(self, key: str) -> bool:
        if not self.settings["approval_mode"]:
            return False
        tool = self.tools.get(key) or {}
        return tool.get("risk") == "high"

    def create_approval(self, key: str, payload: dict[str, Any], requester: str,
                        preview: dict[str, Any] | None = None) -> dict[str, Any]:
        """审批单落 catalog.json，所以它**先脱敏再存**：批准的人在窗口里看的是要点，不是密钥。

        ``preview`` 里带真正会执行的命令行 —— 只有原始 inputs 的话，批准的人看不到这条命令
        最终长成什么样，审批就退化成"点一下同意"。
        """
        token = uuid4().hex[:12]
        record = {
            "id": token, "tool": key, "payload": redact(dict(payload)), "requested_by": requester,
            "status": "pending", "created_at": _now(), "decided_at": None,
            "title": (self.tools.get(key) or {}).get("title", key),
            "risk": (self.tools.get(key) or {}).get("risk", "unknown"),
            **(preview or {}),
        }
        with self._lock:
            self.approvals[token] = record
            self.save()
        self.audit({"type": "approval_requested", "tool": key, "approval": token, "actor": requester,
                    "command_line": record.get("command_line")})
        return record

    def decide_approval(self, token: str, approve: bool, actor: str = "user") -> dict[str, Any]:
        with self._lock:
            record = self.approvals.get(token)
            if record is None:
                raise KeyError(token)
            record["status"] = "approved" if approve else "rejected"
            record["decided_at"] = _now()
            self.save()
        self.audit({"type": "approval_decided", "tool": record["tool"], "approval": token,
                    "decision": record["status"], "actor": actor})
        return record

    def get_approval(self, token: str) -> dict[str, Any] | None:
        return self.approvals.get(token)

    def pending_approvals(self) -> list[dict[str, Any]]:
        return [v for v in self.approvals.values() if v.get("status") == "pending"]

