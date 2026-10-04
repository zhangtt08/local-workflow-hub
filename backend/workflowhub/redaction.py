"""把密钥挡在落盘与返回之前 —— 执行用的那一份不受影响。

Agent 可以把密钥放在 ``env`` / ``argv`` / ``flags`` 里传进来。这些东西必须**原样**交给进程
（否则工具跑不起来），但不能留在运行记录、审计流水或审批单里：那些文件是给人读的，
也会从 ``/api/v1/runs``、``/api/v1/activity``、``/api/v1/approvals`` 返回，还会被复制进
界面里"给 Agent 的调用片段"。本机实测（2026-10-04）：改之前 ``env`` 的值会原样出现在
``data/runs/<id>.json`` 与 ``GET /api/v1/runs/{id}`` 的响应里。

规则，故意做得保守（宁可多遮，不可漏遮）：

1. ``env`` 的值一律遮；变量名保留 —— 排查时要知道传了哪些键，而不是它们等于什么。
2. 名字里带 token/secret/password/key/cookie/authorization… 的字段，其**标量值**遮。
   结构（dict / list）不整块替换，只遮叶子：运行记录里 ``inputs`` 混着契约与绑定结构，
   把 dict 换成字符串会让 ``WorkflowRun`` 读不回来，宁可精确一点也不能把记录读坏。
3. 命令行字符串里的 ``--password=x`` 与 ``--password x`` 两种写法，遮值不遮旗标。
4. ``stdin`` 不遮也不截断：它是命令的输入数据，运行记录留着原文才能"按同样输入重跑"。
   要传密钥请走 ``env``，别拼在 stdin 或 argv 里 —— 这条写在 agent 指南里。

命令自己的 stdout/stderr 里如果出现密钥，本模块无从判断（同一条边界写在指南里）。
"""

from __future__ import annotations

import re
from typing import Any

MASK = "***（已脱敏）"

#: 字段名里出现这些词就当敏感
_SECRET_NAME = re.compile(
    r"(?:^|[^a-z0-9])(?:token|secret|password|passwd|pwd|credential|credentials|cookie|cookies"
    r"|authorization|auth|apikey|api_key|api-key|access_key|accesskey|access-token|private_key"
    r"|client_secret|session_key|session_id|bearer|privatekey)(?:$|[^a-z0-9])",
    re.I,
)
#: 命令行里的密钥旗标（`-p` 太歧义所以不收）
_SECRET_FLAG = r"-{1,2}(?:password|passwd|pwd|secret|token|api[-_]?key|access[-_]?token|auth|bearer)"
_SECRET_ASSIGN = re.compile(rf"(?P<flag>{_SECRET_FLAG})(?P<eq>[=:])(?P<value>\S+)", re.I)
_SECRET_SPACED = re.compile(rf"(?P<flag>{_SECRET_FLAG})[ \t]+(?P<value>\"[^\"]*\"|\S+)", re.I)
#: `Authorization: Bearer <token>` 这类"认证方案 + 值"的写法：旗标是 Bearer/Basic/Token 本身，
#: 前面那条 `--token 值` 的模式抓不到它（Authorization 后面紧跟的是冒号，值在冒号的下一段），
#: 于是 token 会原样留在审计里。先按方案遮，再按旗标遮。
_SECRET_SCHEME = re.compile(r"(?P<flag>\b(?:Bearer|Basic|Digest|Token)\s+)(?P<value>[^\s\"']+)", re.I)

#: 这些键的内容是"命令的输入/输出数据"，不按字段名判敏感
PASSTHROUGH_KEYS = {"stdin", "stdout", "stderr", "partial_stdout", "partial_stderr",
                    "text", "content", "body", "logs", "output", "file_path", "path"}

_MAX_DEPTH = 8


def is_secret_name(name: Any) -> bool:
    return bool(_SECRET_NAME.search(str(name or "")))


def redact_command_line(line: Any) -> Any:
    """遮掉命令行里的密钥值，保留旗标本身（审计要能看出"用了 --password"）。"""
    if not isinstance(line, str) or not line:
        return line
    # 顺序要紧：Bearer 先遮，否则 `Authorization:` 那条只会吃掉冒号后面第一个词（Bearer），
    # 真正的 token 留在原地。
    text = _SECRET_SCHEME.sub(lambda m: f"{m.group('flag')}{MASK}", line)
    text = _SECRET_ASSIGN.sub(lambda m: f"{m.group('flag')}{m.group('eq')}{MASK}", text)
    return _SECRET_SPACED.sub(lambda m: f"{m.group('flag')} {MASK}", text)


def redact(value: Any, *, key: Any = None, secret: bool = False, depth: int = 0) -> Any:
    if depth > _MAX_DEPTH:
        return "<嵌套过深，未展开>"
    if isinstance(value, dict):
        out: dict[str, Any] = {}
        for name, item in value.items():
            if str(name) == "env":
                # 环境变量：名字留着，值全遮（变量名本身几乎不是秘密，值才是）
                out[str(name)] = {str(k): (MASK if _is_meaningful(v) else v) if not isinstance(v, (dict, list))
                                  else redact(v, key=k, secret=True, depth=depth + 1)
                                  for k, v in item.items()}
                continue
            out[str(name)] = redact(item, key=name, secret=secret or is_secret_name(name), depth=depth + 1)
        return out
    if isinstance(value, (list, tuple)):
        return [redact(item, key=key, secret=secret, depth=depth + 1) for item in value]
    if key is not None and str(key) in PASSTHROUGH_KEYS:
        return value
    if value is None or value == "":
        return value
    if secret:
        return MASK
    if isinstance(value, str):
        return redact_command_line(value) if "--" in value else value
    return value


def redact_event(event: dict[str, Any]) -> dict[str, Any]:
    """审计流水与审批单里的一条记录。"""
    clean = redact(dict(event))
    if isinstance(clean.get("command_line"), str):
        clean["command_line"] = redact_command_line(clean["command_line"])
    return clean


def redact_run_record(record: dict[str, Any]) -> dict[str, Any]:
    """运行记录按**路径**脱敏，而不是把整份 JSON 全走一遍。

    ``definition_snapshot`` 是被冻结的契约（字段类型、绑定结构），遮它会连记录本身都读不回来 ——
    契约里存的是"怎么调"，不是"这次传了什么值"，所以原样保留。真正会带密钥的是调用现场传进来的
    那些：``inputs``、每个节点的 ``inputs``、审批任务的 ``payload``、错误 details。
    """
    out = dict(record)
    if isinstance(out.get("inputs"), dict):
        out["inputs"] = redact(out["inputs"])
    if isinstance(out.get("outputs"), dict):
        out["outputs"] = redact(out["outputs"])
    nodes = out.get("node_runs")
    if isinstance(nodes, dict):
        cleaned_nodes = {}
        for node_id, node in nodes.items():
            if not isinstance(node, dict):
                cleaned_nodes[node_id] = node
                continue
            node_copy = dict(node)
            if isinstance(node_copy.get("inputs"), dict):
                node_copy["inputs"] = redact(node_copy["inputs"])
            result = node_copy.get("result")
            if isinstance(result, dict):
                result_copy = dict(result)
                if isinstance(result_copy.get("outputs"), dict):
                    result_copy["outputs"] = redact(result_copy["outputs"])
                if isinstance(result_copy.get("error"), dict):
                    result_copy["error"] = redact(result_copy["error"])
                node_copy["result"] = result_copy
            cleaned_nodes[node_id] = node_copy
        out["node_runs"] = cleaned_nodes
    reviews = out.get("review_tasks")
    if isinstance(reviews, list):
        out["review_tasks"] = [{**r, "payload": redact(r["payload"])} if isinstance(r, dict)
                               and isinstance(r.get("payload"), dict) else r for r in reviews]
    if isinstance(out.get("error"), dict):
        out["error"] = redact(out["error"])
    return out


def _is_meaningful(value: Any) -> bool:
    return value not in (None, "", [], {})
