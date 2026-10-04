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
   argv **数组**同样按这条判（旗标在一项、值在下一项，见 ``redact_argv``）。
4. ``stdin`` 不遮也不截断：它是命令的输入数据，运行记录留着原文才能"按同样输入重跑"。
   要传密钥请走 ``env``，别拼在 stdin 或 argv 里 —— 这条写在 agent 指南里。
5. **落盘与出网是两条边界，都要有**。这一版之前只有前一条：``services.py`` 把原始
   ``outputs``+``logs`` 交给调用方、``command_executor`` 把未脱敏的命令行塞进
   ``outputs["command_line"]`` 与 ``logs[0]``、``api.py`` 又把内存里的运行对象原样返回，
   于是"磁盘上遮了、响应里没遮"。出网那一道现在是 ``redact_wire()``：
   由 HTTP 中间件在**唯一的出口**上对每个 JSON 响应调用（见 ``api.py``）。

命令自己的 stdout/stderr 里如果出现密钥，本模块无从判断（同一条边界写在指南里）——
但"把命令行回显到 stdout"这种形状是认得出的，`mask_echoes()` 专管这一类。
"""

from __future__ import annotations

import re
from typing import Any

MASK = "***（已脱敏）"

#: 字段名里出现这些词就当敏感。
#: 注意 `auth` 这个词本身**不在**名单里：它在本项目的响应里是"鉴权策略说明"
#: （`/api/v1/agent/info` 的 `{"auth": {"required": …}}`），把它当密钥名会连契约文档都遮没。
#: 真正带凭据的写法要么叫 `authorization` / `auth_token`（名单里有），要么值本身长成
#: `Bearer xxx` —— 那种由下面 `_SECRET_SCHEME` 按值抓，不靠字段名。
_SECRET_NAME = re.compile(
    r"(?:^|[^a-z0-9])(?:token|secret|password|passwd|pwd|credential|credentials|cookie|cookies"
    r"|authorization|auth_token|auth-token|apikey|api_key|api-key|access_key|accesskey|access-token"
    r"|private_key|client_secret|session_key|session_id|bearer|privatekey)(?:$|[^a-z0-9])",
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

#: 这些键的内容是"命令的输入/输出数据"，不按字段名判敏感。
#: ``logs`` 以前在这份名单里 —— 那正是"磁盘遮了、线上没遮"的另一半：执行器把**完整命令行**
#: 放在 ``logs[0]``（`command_executor.py`），按名字豁免就等于把 `--password 值` 原样送给调用方。
#: 现在它不在名单里：日志按普通字符串走命令行的那条遮法（旗标留着、值遮掉）。
PASSTHROUGH_KEYS = {"stdin", "stdout", "stderr", "partial_stdout", "partial_stderr",
                    "text", "content", "body", "output", "file_path", "path"}

#: 这些键的值是 argv 形状的列表：旗标与它的值分在相邻两项里，逐条判名字判不出来
ARGV_KEYS = {"argv", "args", "command", "failed_command"}

_MAX_DEPTH = 8


def redact_argv(items: Any) -> Any:
    """argv 形状的列表：`["--password", "值"]` 里值在**下一项**，按字段名判不出来。

    命令行字符串那份有 `_SECRET_SPACED` 兜着，而运行记录与响应里还有一份原始 argv
    （`run.inputs["argv"]`、workflow 运行返回的 `definition_snapshot` 之外的现场值），
    不处理这一份就等于"遮了拼接的那条，没遮数组的那条"。
    """
    if not isinstance(items, (list, tuple)) or not all(isinstance(i, str) for i in items):
        return items
    out = list(items)
    flag = re.compile(rf"^{_SECRET_FLAG}$", re.I)
    for index, token in enumerate(out):
        if not flag.match(token.strip()):
            continue
        if index + 1 < len(out) and out[index + 1] and not out[index + 1].startswith("-"):
            out[index + 1] = MASK            # `--password 值`：值是下一项
        # `--password=值` 这种写法在单项里，交给下面那条赋值正则
        out[index] = redact_command_line(token)
    return [redact_command_line(item) if isinstance(item, str) else item for item in out]


def mask_echoes(value: Any, depth: int = 0) -> Any:
    """把"命令行回声"这一类内容就地遮掉，不看字段名。

    stdout / stderr 是命令自己的输出，按名字判敏感会遮掉有用的东西；但工具把 argv 回显到
    标准输出（`set -x`、`echo`、很多 CLI 的"我打算跑这条"）时，`--password 值` 就出现在
    输出里了。这一遍只认旗标形状，不认字段名，所以对普通输出是恒等变换。
    """
    if depth > _MAX_DEPTH + 4:
        return value
    if isinstance(value, dict):
        return {k: mask_echoes(v, depth + 1) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [mask_echoes(v, depth + 1) for v in value]
    if isinstance(value, str):
        return redact_command_line(value) if ("--" in value or "-p" in value) else value
    return value


def redact_wire(payload: Any) -> Any:
    """**出口那一道**：每一个响应体出网前都过这里。

    落盘脱敏（`persistence.py` / `catalog.audit`）与内存里那份运行对象是两件事 ——
    `runner.py` 存的 `inputs` 是原始现场，`services.invoke_tool` 直接把 `outputs` 与 `logs`
    交给调用方，`POST /api/v1/workflows/{id}/run` 更是把内存对象整个返回。逐个路由补是补不完的
    （补不完就是这次 BLOCKER 2 的成因），所以判据放在中间件那一个点上：
    ① 按名字/env 的规则走一遍；② argv 数组按旗标-值相邻关系走一遍；③ 所有命令行回声走一遍。
    """
    return mask_echoes(redact(payload))


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
        if str(key or "") in ARGV_KEYS:
            # `["--password", "值"]`：值在下一项里，按字段名判不出来，按相邻关系判
            value = redact_argv(value)
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
