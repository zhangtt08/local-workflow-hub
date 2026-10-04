"""Executor for real machine commands discovered by the catalogue.

Commands run without a shell (``exec`` semantics), so Agent-supplied parameters cannot be
turned into shell injection. ``dry_run`` returns the exact command line before anything runs.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import subprocess
from pathlib import Path
from typing import Any
from uuid import uuid4

from .errors import WorkflowHubError
from .models import Artifact, ErrorCode, ExecutionError, NodeStatus, SkillDefinition, SkillResult, utc_now
from .ports import ExecutionContext

GENERIC_INPUTS = {"argv", "flags", "stdin", "cwd", "env", "timeout_seconds", "dry_run"}
MAX_ARGV_ITEMS = 128
MAX_ARG_CHARS = 4000
#: 绝对上限：节点/契约之外，任何一次命令都不许跑过这个数（system_max_timeout 只会比它更小）
MAX_TIMEOUT_SECONDS = 3600.0
#: 收割超时：管道被一个还活着的孙进程占着时，communicate() 会一直挂 —— 本机实测 3s 的超时
#: 拖到 120.6s 才返回。到点就放弃读管道，宁可少一份输出，也不能让调用方一直等。
REAP_GRACE_SECONDS = 2.0


def arg_key(value: str) -> str:
    """Normalise a CLI parameter name to a comparable key: --Frontend-Url / FrontendUrl → frontend-url."""
    text = re.sub(r"^-+", "", str(value).strip())
    text = re.sub(r"(?<=[a-z0-9])(?=[A-Z])", "-", text)
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")


def _slug(value: str) -> str:
    return arg_key(value)


def _clean_sequence(values: Any, label: str) -> list[str]:
    if values is None:
        return []
    if isinstance(values, str):
        values = [values]
    if not isinstance(values, (list, tuple)):
        raise WorkflowHubError(ErrorCode.INPUT_ERROR, f"{label} 需要字符串数组")
    out: list[str] = []
    for item in values:
        text = item if isinstance(item, str) else json.dumps(item, ensure_ascii=False)
        if "\x00" in text:
            raise WorkflowHubError(ErrorCode.INPUT_ERROR, f"{label} 含非法空字符")
        out.append(text[:MAX_ARG_CHARS])
    return out[:MAX_ARGV_ITEMS]


def _arg_specs(skill: SkillDefinition) -> list[dict[str, Any]]:
    raw = skill.metadata.get("arg_specs")
    return [item for item in raw if isinstance(item, dict)] if isinstance(raw, list) else []


def _match_spec(skill: SkillDefinition, field: str) -> dict[str, Any] | None:
    wanted = _slug(field)
    for spec in _arg_specs(skill):
        if _slug(str(spec.get("name") or "")) == wanted:
            return spec
    return None


def _flag(skill: SkillDefinition, field: str) -> str:
    spec = _match_spec(skill, field)
    name = str((spec or {}).get("name") or field)
    if name.startswith("-"):
        return name
    style = str(skill.metadata.get("flag_style") or "double-dash")
    return f"-{name}" if style == "single-dash" else f"--{name}"


def _render(flag: str, value: Any, boolean: bool) -> list[str]:
    if value is None:
        return []
    if isinstance(value, (list, tuple)):
        out: list[str] = []
        for item in value:
            out.extend(_render(flag, item, boolean))
        return out
    if isinstance(value, bool):
        return [flag] if value else []
    if boolean:
        return [flag] if str(value).lower() in {"true", "1", "yes"} else []
    if isinstance(value, dict):
        value = json.dumps(value, ensure_ascii=False)
    return [flag, str(value)]


def build_command(skill: SkillDefinition, inputs: dict[str, Any]) -> tuple[list[str], dict[str, Any]]:
    """Deterministic command line plus the options that wrap it."""
    base = [skill.executor.entry, *skill.executor.args]
    if not str(base[0] or "").strip():
        raise WorkflowHubError(ErrorCode.EXECUTOR_ERROR, "命令入口为空，无法执行")
    if not Path(str(base[0])).is_absolute():
        raise WorkflowHubError(ErrorCode.EXECUTOR_ERROR, f"命令必须登记为绝对路径：{base[0]}")

    tokens = list(base) + _clean_sequence(inputs.get("argv"), "argv")
    flags = inputs.get("flags") or {}
    if not isinstance(flags, dict):
        raise WorkflowHubError(ErrorCode.INPUT_ERROR, "flags 需要对象")
    provided = {**{k: v for k, v in inputs.items() if k not in GENERIC_INPUTS}, **flags}
    for field, value in provided.items():
        spec = _match_spec(skill, field)
        tokens.extend(_render(_flag(skill, field), value, bool(spec and spec.get("kind") == "boolean")))
    return tokens, {
        "cwd": inputs.get("cwd"), "env": inputs.get("env") or {}, "stdin": inputs.get("stdin"),
        "timeout": inputs.get("timeout_seconds"), "dry_run": bool(inputs.get("dry_run")),
    }


def command_line(tokens: list[str]) -> str:
    return " ".join(token if token and " " not in token else f'"{token}"' for token in tokens)


def decode_output(data: bytes) -> str:
    """Chinese Windows consoles answer in CP936 while most tools emit UTF-8; try both before mojibake."""
    if not data:
        return ""
    for encoding in ("utf-8", "gbk", "cp936"):
        try:
            return data.decode(encoding)
        except UnicodeDecodeError:
            continue
    return data.decode("utf-8", errors="replace")


def check_timeout_range(requested: Any) -> float:
    """一次调用能要的秒数：只有一条判据，REST/MCP/CLI/executor 都走这里。"""
    try:
        wanted = float(requested)
    except (TypeError, ValueError) as exc:
        raise WorkflowHubError(
            ErrorCode.INPUT_ERROR,
            f"timeout_seconds 需要数字秒数，收到：{requested!r}",
            {"hint": "例如 30；留空则用设置里的默认超时"},
        ) from exc
    if wanted <= 0 or wanted > MAX_TIMEOUT_SECONDS:
        raise WorkflowHubError(
            ErrorCode.INPUT_ERROR,
            f"timeout_seconds 必须在 1～{MAX_TIMEOUT_SECONDS:g} 秒之间，收到：{wanted:g}",
            {"hint": "长任务请改成后台调用（wait=false）轮询，而不是把单次超时拉到跑不完"},
        )
    return wanted


def resolve_timeout(options: dict[str, Any], ceiling: float, default: float) -> tuple[float, str | None]:
    """调用方要的超时是请求，不是天花板；上限来自 runner 算出的 system ceiling。

    以前这里写的是 ``min(请求, 工具默认, 3600)``，而目录里每条工具的默认都是设置里的
    ``default_timeout_seconds``（120s）—— 于是 Agent 明确要 600s，实际只拿到 120s，
    还被记成"命令超过 120s 未结束，已强制终止"。默认值就该只当默认值。
    """
    cap = min(float(ceiling or default), MAX_TIMEOUT_SECONDS)
    requested = options.get("timeout")
    if requested in (None, ""):
        return cap, None
    wanted = check_timeout_range(requested)
    if wanted > cap:
        return cap, f"timeout_seconds={wanted:g}s 超过本次上限 {cap:g}s，按 {cap:g}s 执行"
    return wanted, None


def kill_process_tree(process: asyncio.subprocess.Process) -> bool:
    """把命令连同它自己派生的子孙一起收掉。

    ``process.kill()`` 在 Windows 上只杀直接子进程：`npm.cmd` 下面的 node、
    `powershell -File x.ps1` 下面的 python 都会活下来继续写文件、占端口、占管道。
    这条函数用在超时、取消、异常三条退出路径上，必须是同步且有界的 —— 取消进行中的
    协程里再 await 一个可能被二次取消的清理步骤，等于清理本身也没了。
    """
    if process is None or process.returncode is not None:
        return False
    pid = process.pid
    try:
        if os.name == "nt":
            subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"],
                           stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                           stderr=subprocess.DEVNULL, timeout=5, check=False)
        else:
            os.killpg(os.getpgid(pid), 9)
    except Exception:
        try:  # taskkill 缺席 / 没有自己的进程组：退回只杀直接子进程，仍然要杀
            process.kill()
        except Exception:
            return False
    return True


class CommandExecutor:
    """Runs a discovered machine command with bounded output and a hard timeout."""

    def __init__(self, max_output_bytes: int = 200_000):
        self.max_output_bytes = max_output_bytes

    async def _drain(self, process: asyncio.subprocess.Process) -> tuple[bytes, bytes]:
        """收割管道里的剩余输出；有孙进程占着句柄时按 REAP_GRACE_SECONDS 放弃。"""
        try:
            stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=REAP_GRACE_SECONDS)
            return stdout or b"", stderr or b""
        except (asyncio.TimeoutError, asyncio.CancelledError, ProcessLookupError, RuntimeError):
            return b"", b""

    async def execute(self, skill: SkillDefinition, inputs: dict[str, Any], context: ExecutionContext,
                      timeout: float) -> SkillResult:
        started = utc_now()
        tokens, options = build_command(skill, inputs)
        line = command_line(tokens)
        effective_timeout, clamp_note = resolve_timeout(options, timeout, skill.timeout_seconds)

        if options["dry_run"]:
            return SkillResult(
                status=NodeStatus.SUCCEEDED,
                outputs={"exit_code": None, "stdout": "", "stderr": "", "command_line": line,
                         "dry_run": True, "timeout_seconds": effective_timeout,
                         **({"timeout_note": clamp_note} if clamp_note else {})},
                logs=[f"dry_run: {line}"] + ([clamp_note] if clamp_note else []),
                started_at=started, finished_at=utc_now(),
            )

        extra_env = options["env"]
        if not isinstance(extra_env, dict):
            raise WorkflowHubError(ErrorCode.INPUT_ERROR, "env 需要对象")
        env = dict(os.environ)
        for key, value in extra_env.items():
            if not isinstance(key, str) or not key or "=" in key:
                raise WorkflowHubError(ErrorCode.INPUT_ERROR, f"非法环境变量名：{key!r}")
            env[key] = str(value)
        env.setdefault("PYTHONUTF8", "1")
        env.setdefault("PYTHONIOENCODING", "utf-8")

        cwd = options["cwd"] or skill.executor.cwd or str(Path(tokens[0]).parent)
        cwd_path = Path(cwd)
        if not cwd_path.is_dir():
            # 以前这里悄悄退回用户主目录：命令在错的目录里跑"成功"了，产物落在别处，
            # 排查的人要过很久才发现跑错了地方。目录不存在就直说，并且说清怎么改。
            raise WorkflowHubError(
                ErrorCode.INPUT_ERROR,
                f"工作目录不存在或不是目录：{cwd}",
                {"cwd": str(cwd), "requested": str(options["cwd"] or ""),
                 "hint": "cwd 要么留空（用工具登记的目录），要么指到真实存在的目录；"
                         "工具登记的目录消失通常是那本目录被移动了，可在工具库重新扫描"},
            )
        context.artifact_root.mkdir(parents=True, exist_ok=True)

        try:
            process = await asyncio.create_subprocess_exec(
                *tokens,
                stdin=asyncio.subprocess.PIPE if options["stdin"] else asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
                cwd=str(cwd_path), env=env,
            )
        except (OSError, ValueError) as exc:
            return SkillResult(
                status=NodeStatus.FAILED,
                error=ExecutionError(code=ErrorCode.EXECUTOR_ERROR,
                                     message=f"无法启动命令：{exc}",
                                     retryable=isinstance(exc, PermissionError),
                                     details={"command_line": line, "cwd": str(cwd_path),
                                              "hint": "确认入口路径还在（工具库里可重新扫描），"
                                                      "并且该文件不是被占用或拦截的安装包"}),
                logs=[line], started_at=started, finished_at=utc_now(),
            )

        stdin_bytes = str(options["stdin"]).encode("utf-8") if options["stdin"] else None
        timed_out = False
        try:
            try:
                stdout, stderr = await asyncio.wait_for(process.communicate(stdin_bytes),
                                                        timeout=effective_timeout)
            except asyncio.TimeoutError:
                timed_out = True
                kill_process_tree(process)
                stdout, stderr = await self._drain(process)
        except asyncio.CancelledError:
            # 客户端断开、界面点了取消、服务在关停 —— 都不能把子进程留在机器上。
            kill_process_tree(process)
            raise
        except BaseException:
            kill_process_tree(process)
            raise
        finally:
            if process.returncode is None:
                kill_process_tree(process)

        cap = self.max_output_bytes
        raw_out = decode_output(stdout)
        raw_err = decode_output(stderr)
        out_text, err_text = raw_out[:cap], raw_err[:cap]
        artifacts = []
        for label, text in (("stdout", out_text), ("stderr", err_text)):
            if not text.strip():
                continue
            path = context.artifact_root / f"{label}-{uuid4().hex[:8]}.txt"
            path.write_text(text, encoding="utf-8")
            artifacts.append(Artifact(id=uuid4().hex, uri=str(path), media_type="text/plain",
                                      node_run_id=context.node_run_id, size_bytes=path.stat().st_size))
        exit_code = process.returncode
        outputs = {
            "exit_code": exit_code, "stdout": out_text, "stderr": err_text, "command_line": line,
            "cwd": str(cwd_path), "duration_ms": int((utc_now() - started).total_seconds() * 1000),
            "truncated": len(raw_out) > cap or len(raw_err) > cap,
            "timeout_seconds": effective_timeout,
            **({"timeout_note": clamp_note} if clamp_note else {}),
        }
        logs = [line, *out_text.splitlines()[:200]]
        if timed_out:
            logs.append(f"timeout: 超过 {effective_timeout:g}s，已连同子进程一起终止")
            return SkillResult(
                status=NodeStatus.FAILED, outputs=outputs, artifacts=artifacts, logs=logs,
                error=ExecutionError(
                    code=ErrorCode.TIMEOUT, retryable=True,
                    message=f"命令超过 {effective_timeout:g}s 未结束，"
                            f"已连同它自己派生的进程一起终止（pid {process.pid}）",
                    details={"command_line": line, "cwd": str(cwd_path),
                             "timeout_seconds": effective_timeout,
                             "partial_stderr": err_text[-800:], "partial_stdout": out_text[-800:],
                             "hint": "这条命令还在跑说明它要么真的慢、要么在等你看不见的输入。"
                                     "先带 dry_run 复核 argv；确认它需要交互输入就用 stdin 传进去，"
                                     "或者把 timeout_seconds 提到能接受的范围"}),
                started_at=started, finished_at=utc_now(),
            )
        if exit_code not in (0, None):
            return SkillResult(
                status=NodeStatus.FAILED, outputs=outputs, artifacts=artifacts, logs=logs,
                error=ExecutionError(code=ErrorCode.EXECUTOR_ERROR, retryable=False,
                                     message=f"命令退出码 {exit_code}（不是 0 就是失败，别当成没跑）",
                                     details={"command_line": line, "cwd": str(cwd_path),
                                              "stderr": err_text[:1200] or "（标准错误是空的）",
                                              "hint": "先看 stderr 这一段原文：命令自己说为什么不过。"
                                                      "空 stderr + 退出码 1 常见于参数被命令自己的校验拒掉，"
                                                      "可带 dry_run 复核命令行，或加 --help 看它要什么"}),
                started_at=started, finished_at=utc_now(),
            )
        return SkillResult(status=NodeStatus.SUCCEEDED, outputs=outputs, artifacts=artifacts,
                           logs=logs, started_at=started, finished_at=utc_now())
