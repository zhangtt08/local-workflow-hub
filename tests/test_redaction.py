"""脱敏只许发生在"落盘与返回"这一侧，执行用的那一份一个字节都不许动。

这个模块是另外一轮改动留下的，写完后没有任何测试覆盖 —— 一个没人测的安全件比没有更安全：
遮少了会漏密，遮多了会把要跑的命令遮成跑不了的东西。下面每一段都是能红的。
"""
from __future__ import annotations

import copy
import json
import tempfile
import unittest
from pathlib import Path

from workflowhub.catalog import ToolCatalog
from workflowhub.redaction import MASK, is_secret_name, redact, redact_command_line

SECRET = "sk-REALLIVEVALUE42"


class RedactionRules(unittest.TestCase):
    def test_env_values_are_masked_but_names_survive(self):
        out = redact({"env": {"API_TOKEN": SECRET, "PATH": "C:/x"}})
        self.assertEqual(out["env"]["API_TOKEN"], MASK, "env 的值没遮")
        # 规则就是"env 的值一律遮，变量名留着"：排查时要知道传了哪些键，而不是它们等于什么
        self.assertEqual(out["env"]["PATH"], MASK)
        self.assertEqual(set(out["env"]), {"API_TOKEN", "PATH"}, "变量名被一起抹掉了")
        self.assertNotIn(SECRET, json.dumps(out, ensure_ascii=False))

    def test_secret_shaped_field_names_are_masked_and_structure_is_kept(self):
        out = redact({"api_key": SECRET, "endpoint": "https://hub.local", "argv": ["--dry-run"]})
        self.assertEqual(out["api_key"], MASK)
        self.assertEqual(out["endpoint"], "https://hub.local", "非敏感兄弟字段被一起遮了")
        self.assertEqual(out["argv"], ["--dry-run"])
        nested = redact({"credentials": {"user": "ztt", "password": SECRET}})
        self.assertIsInstance(nested["credentials"], dict, "结构被整块换成字符串，运行记录就读不回来了")
        self.assertEqual(nested["credentials"]["password"], MASK)
        self.assertTrue(is_secret_name("access_token"))
        self.assertFalse(is_secret_name("argv"))

    def test_data_bearing_keys_are_not_masked(self):
        """stdin/stdout/logs 这些是"命令的输入输出"，按字段名遮会把有用的东西遮没。"""
        out = redact({"stdout": f"token field printed {SECRET}", "logs": [SECRET]})
        self.assertIn(SECRET, out["stdout"])
        self.assertIn(SECRET, out["logs"][0])

    def test_command_line_keeps_the_flag_and_hides_the_value(self):
        line = redact_command_line('mysql --password=ROOTpw -h 127.0.0.1')
        self.assertIn("--password", line)
        self.assertNotIn("ROOTpw", line)
        spaced = redact_command_line('curl -H "Authorization: Bearer tk-real"')
        self.assertNotIn("tk-real", spaced)
        self.assertIn("Authorization", spaced)

    def test_deep_nesting_stops_instead_of_recurring_forever(self):
        node: dict = {"leaf": SECRET}
        for _ in range(12):
            node = {"next": node}
        out = json.dumps(redact(node), ensure_ascii=False)
        self.assertNotIn(SECRET, out)
        self.assertIn("未展开", out)


class RedactionDoesNotTouchWhatGetsExecuted(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp()) / "catalog"
        self.dir.mkdir(parents=True)
        self.catalog = ToolCatalog(self.dir)

    def test_audit_masks_on_disk_without_mutating_the_callers_payload(self):
        event = {"tool": "cli-x", "at": "2026-10-04T05:00:00",
                 "inputs": {"env": {"API_KEY": SECRET}, "argv": [f"--token={SECRET}"]}}
        snapshot = copy.deepcopy(event)
        self.catalog.audit(event)
        # 原地改就是事故：审计写的是副本，执行用的是同一份 dict，遮过之后命令就跑不通了
        self.assertEqual(event, snapshot, "audit() 把调用方的入参改掉了")
        written = (self.dir / "audit.jsonl").read_text(encoding="utf-8")
        self.assertNotIn(SECRET, written, "审计流水里留了明文密钥")
        self.assertIn(MASK, written)
        row = json.loads(written.strip().splitlines()[-1])
        self.assertEqual(row["tool"], "cli-x", "脱敏把记录本身读坏了")


if __name__ == "__main__":
    unittest.main()
