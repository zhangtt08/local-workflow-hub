"""Machine-wide capability discovery: executables on PATH, installed apps, script tools,
project scripts and Skill folders on disk. All scanners are read-only; nothing here executes
a discovered command beyond an optional bounded ``--version`` probe.
"""

from __future__ import annotations

import json
import os
import re
import winreg
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]

EXECUTABLE_SUFFIXES = (".exe", ".com", ".cmd", ".bat")
SCRIPT_SUFFIXES = (".py", ".ps1", ".bat", ".cmd", ".sh", ".js", ".mjs")
SKIP_DIRS = {
    "node_modules", ".git", "__pycache__", ".venv", "venv", "env", "dist", "build", ".next",
    "target", "out", ".cache", "appdata", "library", "windows", "system32", "syswow64",
    "program files", "program files (x86)", "programdata", "$recycle.bin", "ms-playwright",
    ".npm", ".nuget", "site-packages", "dist-packages", "resources", "public", "vendor",
}
HIGH_RISK_NAMES = {
    "cmd", "powershell", "pwsh", "bash", "sh", "zsh", "python", "python3", "py", "pythonw",
    "node", "deno", "reg", "regedit", "takeown", "icacls", "diskpart", "format", "shutdown",
    "del", "rd", "erase", "cipher", "vssadmin", "wevtutil", "schtasks", "sc", "netsh", "runas",
    "wmic", "psexec", "mshta", "rundll32", "certutil",
}
MEDIUM_RISK_NAMES = {"curl", "wget", "ssh", "scp", "npm", "npx", "pip", "git", "docker", "kubectl"}
DELETE_PATTERNS = re.compile(r"(remove-item|\bdel\b|\berase\b|rm\s+-rf|rmdir|diskpart|format\s|vssadmin|cipher\s)", re.I)

# Windows ships these outside PATH on some machines; they are the tools agents ask for most.
BUILTIN_COMMANDS = {
    "curl": "HTTP 请求 / 文件下载",
    "tar": "打包与解包 zip/tar",
    "ping": "网络连通性测试",
    "nslookup": "DNS 查询",
    "ipconfig": "本机网络配置",
    "systeminfo": "系统信息",
    "tasklist": "进程列表",
    "powercfg": "电源与性能计划",
    "where": "查找命令位置",
    "robocopy": "高速复制 / 镜像同步",
    "msinfo32": "系统摘要",
    "control": "打开控制面板项",
    "notepad": "记事本",
    "explorer": "打开资源管理器路径",
}


def launch_path() -> list[str]:
    """PATH as a double-clicked desktop app sees it: registry Environment keys, not this shell."""
    entries: list[str] = []

    def read(root: int, key: str) -> None:
        try:
            with winreg.OpenKey(root, key) as handle:
                value, _ = winreg.QueryValueEx(handle, "Path")
            expanded = os.path.expandvars(value)
            entries.extend(part for part in expanded.split(os.pathsep) if part)
        except OSError:
            pass

    read(winreg.HKEY_LOCAL_MACHINE, r"SYSTEM\CurrentControlSet\Control\Session Manager\Environment")
    read(winreg.HKEY_CURRENT_USER, r"Environment")
    entries.extend(os.environ.get("PATH", "").split(os.pathsep))
    out: list[str] = []
    seen: set[str] = set()
    for raw in entries:
        item = raw.strip().strip('"')
        if not item:
            continue
        low = item.lower().rstrip("\\")
        if low in seen:
            continue
        seen.add(low)
        out.append(item)
    return out


def slug(*parts: str) -> str:
    text = "-".join(p for p in parts if p)
    text = re.sub(r"[^a-zA-Z0-9]+", "-", text).strip("-").lower()
    return re.sub(r"-{2,}", "-", text)[:64] or "tool"


def _read_head(path: Path, limit: int = 240_000) -> str:
    try:
        with path.open("r", encoding="utf-8", errors="replace") as stream:
            return stream.read(limit)
    except OSError:
        return ""


def _docstring_first_line(text: str) -> str:
    match = re.search(r'^[\'"]{3}\s*(?:\n)?([^\n"]{6,200})', text, re.M)
    if match:
        return match.group(1).strip()
    for line in text.splitlines()[:40]:
        stripped = line.strip()
        if stripped.startswith("#") and not stripped.startswith("#!") and len(stripped) > 6:
            return stripped.lstrip("# ").strip()
    return ""


@dataclass
class ArgSpec:
    name: str
    kind: str = "string"
    required: bool = False
    description: str = ""

    def to_dict(self) -> dict[str, object]:
        return {"name": self.name, "kind": self.kind, "required": self.required, "description": self.description}


@dataclass
class Candidate:
    """A tool that was found on this machine, before it becomes a Skill contract."""

    key: str
    title: str
    kind: str
    command: list[str]
    source: str
    description: str = ""
    cwd: str | None = None
    category: str = "general"
    tags: list[str] = field(default_factory=list)
    args: list[ArgSpec] = field(default_factory=list)
    risk: str = "low"
    permissions: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    loc: str | None = None
    language: str | None = None
    extra: dict[str, object] = field(default_factory=dict)

    def to_dict(self) -> dict[str, object]:
        return {
            "key": self.key, "title": self.title, "kind": self.kind, "command": self.command,
            "source": self.source, "description": self.description, "cwd": self.cwd,
            "category": self.category, "tags": self.tags, "args": [a.to_dict() for a in self.args],
            "risk": self.risk, "permissions": self.permissions, "notes": self.notes,
            "loc": self.loc, "language": self.language, "extra": self.extra,
        }


def risk_for(command: list[str], description: str = "") -> str:
    head = os.path.basename(command[0]).lower().rsplit(".", 1)[0] if command else ""
    joined = " ".join(command) + " " + description
    if head in HIGH_RISK_NAMES or DELETE_PATTERNS.search(joined):
        return "high"
    if head in MEDIUM_RISK_NAMES:
        return "medium"
    return "low"


def long_path(value: str) -> str:
    """Windows hands back 8.3 short paths from temp locations; agents deserve the real name."""
    if not value or not IS_WINDOWS:
        return value
    try:
        import ctypes

        windll = ctypes.windll.kernel32
        buffer = ctypes.create_unicode_buffer(260)
        size = windll.GetLongPathNameW(value, buffer, 260)
        if 0 < size < 260:
            return buffer.value
    except Exception:
        pass
    return value


IS_WINDOWS = os.name == "nt"


# ── 1. 可执行程序（PATH + 常见安装目录） ────────────────────────────────────────
def scan_executables(path_dirs: list[str] | None = None, limit_per_dir: int = 400) -> list[Candidate]:
    dirs = path_dirs if path_dirs is not None else launch_path()
    found: dict[str, Candidate] = {}
    for directory in dirs:
        base = Path(directory)
        try:
            if not base.is_dir():
                continue
            children = list(base.iterdir())
        except OSError:
            continue
        shown = 0
        for child in children:
            if shown >= limit_per_dir:
                break
            if child.is_dir():
                continue
            low = child.name.lower()
            if child.suffix.lower() in EXECUTABLE_SUFFIXES or (os.name == "nt" and child.suffix == ""):
                if child.suffix.lower() not in EXECUTABLE_SUFFIXES and child.suffix:
                    continue
                name = low.rsplit(".", 1)[0]
                if name in {"uninstall", "vcredist", "setup", "installer", "helper", "notification"} or len(name) < 2:
                    continue
                if name in found:
                    continue
                found[name] = Candidate(
                    key=f"cli-{name}", title=child.name, kind="cli",
                    command=[str(child)], source=str(base), category="命令行工具",
                    tags=["cli", "executable"],
                    description=f"PATH 上的可执行程序，可由命令行参数直接驱动：{child.name}",
                    permissions=["shell"], loc=str(child),
                )
                shown += 1
    return list(found.values())


def scan_builtins() -> list[Candidate]:
    out: list[Candidate] = []
    dirs = launch_path() + [r"C:\Windows\System32"]
    for name, purpose in BUILTIN_COMMANDS.items():
        located = None
        for directory in dirs:
            for suffix in ("", ".exe"):
                probe = Path(directory) / (name + suffix)
                if probe.is_file():
                    located = str(probe)
                    break
            if located:
                break
        if located:
            out.append(Candidate(
                key=f"sys-{name}", title=name, kind="cli", command=[located],
                source="Windows 内置", category="系统命令", tags=["system", name],
                description=f"{purpose}（Windows 内置命令 {name}）", permissions=["shell"], loc=located,
            ))
    return out


# ── 2. 已安装应用（注册表卸载项） ───────────────────────────────────────────────
UNINSTALL_KEYS = (
    (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall"),
    (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\WOW6432Node\Microsoft\Windows\CurrentVersion\Uninstall"),
    (winreg.HKEY_CURRENT_USER, r"SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall"),
)


def _reg_value(handle: int, name: str) -> str:
    try:
        value, _ = winreg.QueryValueEx(handle, name)
        if isinstance(value, bytes):
            return value.decode("utf-8", "replace")
        return str(value)
    except OSError:
        return ""


def scan_installed_apps() -> list[Candidate]:
    out: dict[str, Candidate] = {}
    for root, key in UNINSTALL_KEYS:
        try:
            with winreg.OpenKey(root, key) as parent:
                names = [winreg.EnumKey(parent, i) for i in range(winreg.QueryInfoKey(parent)[0])]
        except OSError:
            continue
        for name in names:
            try:
                with winreg.OpenKey(root, f"{key}\\{name}") as handle:
                    display = _reg_value(handle, "DisplayName")
                    if not display:
                        continue
                    version = _reg_value(handle, "DisplayVersion")
                    location = _reg_value(handle, "InstallLocation")
                    icon = _reg_value(handle, "DisplayIcon")
                    uninstall = _reg_value(handle, "UninstallString")
                    publisher = _reg_value(handle, "Publisher")
            except OSError:
                continue
            exe = ""
            if location and Path(location).is_dir():
                try:
                    stem = slug(display)[:12]
                    for child in Path(location).glob("*.exe"):
                        if stem.split("-")[0] in child.name.lower() or child.name.lower() == f"{name.lower()}.exe":
                            exe = str(child)
                            break
                        exe = exe or str(child)
                except OSError:
                    pass
            if not exe and icon:
                cleaned = icon.split(",")[0].strip('"')
                if cleaned.lower().endswith(".exe") and Path(cleaned).is_file():
                    exe = cleaned
            key_name = f"app-{slug(display)}"
            if key_name in out:
                continue
            out[key_name] = Candidate(
                key=key_name, title=display, kind="app",
                command=[exe] if exe else [], source=f"{publisher or '未知发行商'} · 注册表",
                category="已安装应用", tags=["app", "installed"],
                description=f"{display}{' ' + version if version else ''}；安装位置：{location or '未声明'}",
                cwd=location or None,
                permissions=["shell"],
                loc=exe or None,
                extra={"uninstall": uninstall, "version": version, "publisher": publisher},
                notes=["卸载命令已登记但不暴露给 Agent"] if uninstall else [],
                risk="medium" if not exe else risk_for([exe or "app"], display),
            )
    return list(out.values())


# ── 3. 脚本工具 ────────────────────────────────────────────────────────────────
def _py_args(text: str) -> list[ArgSpec]:
    specs: list[ArgSpec] = []
    for match in re.finditer(r"add_argument\(\s*[\"'](-{1,2}[A-Za-z0-9_\-]+)[\"']", text):
        token = match.group(1)
        tail = text[match.end():match.end() + 260]
        required = bool(re.search(r"required\s*=\s*True", tail))
        help_match = re.search(r"help\s*=\s*[\"']([^\"']{2,120})", tail)
        nargs_int = re.search(r"nargs\s*=\s*([0-9]+)", tail)
        kind = "integer" if nargs_int else ("array" if "nargs" in tail else "string")
        specs.append(ArgSpec(name=token.lstrip("-"), kind=kind, required=required,
                             description=help_match.group(1) if help_match else ""))
    for match in re.finditer(r"add_parser\(\s*[\"']([A-Za-z0-9_\-]{2,20})[\"']", text):
        specs.append(ArgSpec(name=match.group(1), kind="string", description="子命令"))
    return specs[:24]


def _ps1_args(text: str) -> list[ArgSpec]:
    match = re.search(r"param\s*\((.{0,1200}?)\)\s*\r?\n", text, re.S | re.I)
    if not match:
        return []
    specs = []
    for token in re.finditer(r"\[([^\]]+)\]\s*\$([A-Za-z_][A-Za-z0-9_]*)", match.group(1)):
        type_name = token.group(1).replace(" ", "")
        kind = "integer" if "int" in type_name else ("boolean" if "switch" in type_name.lower() else "array" if "[" in type_name else "string")
        specs.append(ArgSpec(name=token.group(2), kind=kind, description=f"type {type_name}"))
    return specs[:24]


def _js_args(text: str) -> list[ArgSpec]:
    return [ArgSpec(name=m.group(1), kind="string", description="命令行位置参数")
            for m in re.finditer(r"process\.argv\[(\d)\]", text)][:6]


def scan_script_tools(roots: list[Path], max_depth: int = 4, limit: int = 900) -> list[Candidate]:
    out: list[Candidate] = []
    for root in roots:
        root = root.resolve() if isinstance(root, Path) else Path(root)
        if not root.is_dir():
            continue
        count = 0
        stack: list[tuple[Path, int]] = [(root, 0)]
        while stack and count < limit:
            current, depth = stack.pop()
            try:
                children = sorted(current.iterdir(), key=lambda p: (p.is_file(), p.name.lower()))
            except OSError:
                continue
            for child in children:
                if child.is_dir():
                    if depth + 1 >= max_depth:
                        continue
                    low = child.name.lower()
                    if low in SKIP_DIRS or low.startswith("."):
                        continue
                    stack.append((child, depth + 1))
                    continue
                suffix = child.suffix.lower()
                if suffix not in SCRIPT_SUFFIXES:
                    continue
                if slug(child.stem).count("-") > 6 or child.stem.lower() in {"test", "conftest", "setup", "__init__", "wsgi", "asgi", "env"}:
                    continue
                count += 1
                text = _read_head(child)
                if not text:
                    continue
                runner, entry = _script_invocation(child, text)
                if runner is None:
                    continue
                name = slug(child.parent.name if child.parent != root else "", child.stem)
                if not name:
                    continue
                description = _docstring_first_line(text)
                args = {"py": _py_args, "ps1": _ps1_args, "js": _js_args, "mjs": _js_args}.get(suffix.strip("."), lambda t: [])(text)
                kind = "script"
                out.append(Candidate(
                    key=f"script-{name}", title=child.name, kind=kind, command=entry,
                    source=long_path(str(child.parent)), category=_guess_category(child, text),
                    tags=[suffix.strip("."), "script"] + ([f"{a.name}" for a in args[:4]]),
                    description=(description or f"{child.name} 未写说明，可直接运行")[:220],
                    cwd=long_path(str(child.parent)), args=args, language=suffix.strip("."),
                    permissions=["shell", "filesystem_read"] + (["filesystem_write"] if "open(" in text and "'w'" in text else []),
                    loc=long_path(str(child)), risk=risk_for(entry, child.name),
                    extra={"runner": runner, "bytes": child.stat().st_size},
                    notes=[f"位于 {long_path(str(child.parent))}"] if str(child.parent) != str(root) else [],
                ))
                if count >= limit:
                    break
    return out


def _script_invocation(path: Path, text: str) -> tuple[str | None, list[str]]:
    suffix = path.suffix.lower()
    if suffix == ".py":
        if not re.search(r"argparse|click|sys\.argv|__main__|input\(|^#!.*python", text, re.M):
            return None, []
        return "python", [sys.executable, str(path)]
    if suffix == ".ps1":
        if not re.search(r"param\s*\(|Write-|Get-|Set-|Invoke-|foreach", text, re.I):
            return None, []
        return "powershell", ["powershell.exe", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(path)]
    if suffix in {".bat", ".cmd"}:
        return "cmd", ["cmd.exe", "/c", str(path)]
    if suffix == ".sh":
        shell = next((p for p in (r"C:\Program Files\Git\bin\sh.exe", r"C:\Program Files\Git\usr\bin\sh.exe") if Path(p).is_file()), None)
        return (shell, [shell, str(path)]) if shell else (None, [])
    if suffix in {".js", ".mjs"}:
        if "process.argv" not in text and "require(" not in text and "import " not in text:
            return None, []
        node = _which("node.exe") or _which("node")
        return ("node", [node, str(path)]) if node else (None, [])
    return None, []


GENERIC_PROJECT_NAMES = {"frontend", "backend", "src", "app", "web", "api", "server", "client",
                         "scripts", "tools", "lib", "project"}


def _project_label(current: Path, root: Path) -> str:
    """A folder named "frontend" is not a project name; walk up until one is."""
    for part in reversed(current.relative_to(root).parts if current != root else ()):
        if part.lower() not in GENERIC_PROJECT_NAMES:
            return part
    return current.name


def _guess_category(path: Path, text: str) -> str:
    blob = (str(path.parent) + " " + text[:3000]).lower()
    rules = [
        ("数据处理", ("pandas", "csv", "dataframe", "excel", "openpyxl")),
        ("网络与抓取", ("requests", "httpx", "selenium", "playwright", "scrap", "download")),
        ("媒体", ("ffmpeg", "moviepy", "pillow", "image", "audio", "video")),
        ("部署与构建", ("docker", "build", "deploy", "webpack", "vite", "electron")),
        ("自动化", ("pyautogui", "schedule", "watchdog", "batch", "automation", "keyboard")),
        ("AI 与模型", ("openai", "deepseek", "llm", "embedding", "agent", "model")),
        ("系统维护", ("registry", "service", "powercfg", "driver", "cleanup")),
        ("报告与文档", ("reportlab", "markdown", "docx", "pdf", "jinja")),
    ]
    for name, keys in rules:
        if any(k in blob for k in keys):
            return name
    return "脚本工具"


# ── 4. 项目内的工作流入口（package.json scripts / Makefile / workflow yaml） ────
def scan_project_workflows(roots: list[Path], max_depth: int = 3, limit: int = 300) -> list[Candidate]:
    out: list[Candidate] = []
    for root in roots:
        root = Path(root)
        if not root.is_dir():
            continue
        stack: list[tuple[Path, int]] = [(root, 0)]
        while stack and len(out) < limit:
            current, depth = stack.pop()
            try:
                children = sorted(current.iterdir(), key=lambda p: (p.is_file(), p.name.lower()))
            except OSError:
                continue
            dirs = [c for c in children if c.is_dir() and c.name.lower() not in SKIP_DIRS and not c.name.startswith(".")]
            if depth + 1 <= max_depth:
                stack.extend((d, depth + 1) for d in dirs)
            project = _project_label(current, root)
            manifest = current / "package.json"
            if manifest.is_file():
                try:
                    data = json.loads(manifest.read_text(encoding="utf-8"))
                except (OSError, json.JSONDecodeError):
                    data = {}
                for script_name, command in (data.get("scripts") or {}).items():
                    npm = _which("npm.cmd") or _which("npm")
                    if not npm:
                        break
                    package = str(data.get("name") or "").strip() or project
                    out.append(Candidate(
                        key=f"npm-{slug(package, script_name)}", title=f"{package} · npm {script_name}",
                        kind="project-script", command=[npm, "run", script_name], cwd=str(current),
                        source=f"{current.name}/package.json", category="项目工作流",
                        tags=["npm", package], description=f"npm run {script_name} → {str(command)[:180]}",
                        permissions=["shell"], risk=risk_for(str(command).split(), package),
                        extra={"package_manager": "npm", "raw": command},
                    ))
            makefile = current / "Makefile"
            if makefile.is_file():
                text = _read_head(makefile, 40_000)
                make = _which("make.exe") or _which("mingw32-make.exe")
                for target in re.findall(r"^([A-Za-z0-9_.-]{3,24}):\s*(?:\S.*)?$", text, re.M):
                    if target in {"all", "clean"} or not make:
                        continue
                    out.append(Candidate(
                        key=f"make-{slug(project, target)}", title=f"{project} · make {target}",
                        kind="project-script", command=[make, target], cwd=str(current),
                        source=f"{project}/Makefile", category="项目工作流", tags=["make", project],
                        description=f"make {target}", permissions=["shell"], risk="medium",
                    ))
            for workflows_dir in (current / ".github" / "workflows",):
                if workflows_dir.is_dir():
                    for yml in workflows_dir.glob("*.y*ml"):
                        out.append(Candidate(
                            key=f"ci-{slug(project, yml.stem)}", title=f"{project} · {yml.name}",
                            kind="workflow-blueprint", command=[], source=str(yml),
                            category="CI 工作流", tags=["github-actions", project],
                            description="GitHub Actions 工作流定义，可作为 WorkflowHub DAG 的编排参考",
                            loc=str(yml), notes=["蓝图：本地不可直接执行，需按步骤重建"],
                        ))
    return out


# ── 5. Skill 文件夹（本项目与机器上其它 agent 项目） ─────────────────────────────
def scan_skill_dirs(roots: list[Path]) -> list[Candidate]:
    out: list[Candidate] = []
    for root in roots:
        root = Path(root)
        if not root.is_dir():
            continue
        for manifest in list(root.glob("*/skill.yaml")) + list(root.glob("skills/*/skill.yaml")):
            try:
                raw = manifest.read_text(encoding="utf-8")
            except OSError:
                continue
            meta = dict(re.findall(r"^(name|description|id):\s*(.+)$", raw, re.M))
            folder = manifest.parent
            out.append(Candidate(
                key=f"skill-{slug(folder.parent.name, folder.name)}", title=folder.name, kind="skill",
                command=[], source=str(folder), category="Skill 包", tags=["skill", folder.name],
                description=(meta.get("description") or meta.get("name") or f"{folder.name} Skill 包")[:220],
                cwd=str(folder), loc=str(manifest),
                extra={"manifest": manifest.name}, notes=["含 handler.py，可用 Python Executor 直接编排"],
            ))
    return out


def _which(name: str) -> str | None:
    for directory in launch_path():
        probe = Path(directory) / name
        if probe.is_file():
            return str(probe)
    return None


def probe_version(command: list[str], timeout: float = 3.0) -> dict[str, object]:
    """Best-effort ``--version``/``-V`` probe with a hard byte cap. Never used to decide trust."""
    for flag in ("--version", "-V", "-version"):
        try:
            done = subprocess.run(  # noqa: S603 - command comes from the local machine scan
                command + [flag], capture_output=True, timeout=timeout, check=False, shell=False,
            )
        except (OSError, subprocess.TimeoutExpired):
            continue
        if done.returncode == 0 and (done.stdout or done.stderr):
            text = (done.stdout or done.stderr).decode("utf-8", "replace").strip()
            return {"probe_flag": flag, "output": text[:400]}
    return {"probe_flag": None, "output": ""}


DEFAULT_ROOTS = [
    PROJECT_ROOT,
    Path(r"C:\Users\Administrator\Desktop\项目"),
    Path(r"C:\Users\Administrator\Documents"),
]


def run_scan(roots: list[Path] | None = None, steps: list[str] | None = None) -> list[Candidate]:
    roots = roots or DEFAULT_ROOTS
    steps = steps or ["executables", "system", "apps", "scripts", "projects", "skills"]
    candidates: list[Candidate] = []
    if "executables" in steps:
        candidates += scan_executables()
    if "system" in steps:
        candidates += scan_builtins()
    if "apps" in steps:
        candidates += scan_installed_apps()
    if "scripts" in steps:
        candidates += scan_script_tools(roots)
    if "projects" in steps:
        candidates += scan_project_workflows(roots)
    if "skills" in steps:
        candidates += scan_skill_dirs(roots)
    seen: set[str] = set()
    unique: list[Candidate] = []
    for item in candidates:
        if item.key in seen:
            continue
        seen.add(item.key)
        if item.risk == "low":
            item.risk = risk_for(item.command, item.description)
        item.command = [long_path(part) for part in item.command]
        item.loc = long_path(item.loc) if item.loc else None
        item.cwd = long_path(item.cwd) if item.cwd else None
        unique.append(item)
    return unique
