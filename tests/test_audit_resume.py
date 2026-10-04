"""审计账本在"文件已经存在"这条现网路径上必须算得对。

以前的测试都用空的临时目录起步：audit.jsonl 不存在 → _audit_tail() 走 OSError 分支 →
_audit_reset() 顺手把 _audit_resync 建了出来，于是属性缺失从来暴露不了。
真机上 data/audit.jsonl 是一直存在的，重启后第一次读计数就 AttributeError，
凡是要按工具统计调用次数的端点（含统一契约的 /api/agent/tools）整体 500。
"""
from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from workflowhub.catalog import ToolCatalog


class AuditResumeOnExistingFile(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.dir = self.tmp / "catalog"
        self.dir.mkdir(parents=True)

    def _seed(self, rows: list[dict]) -> None:
        """先把账本写成"上一轮进程留下的样子"，再构造目录（等价于服务重启）。"""
        with (self.dir / "audit.jsonl").open("w", encoding="utf-8") as stream:
            for row in rows:
                stream.write(json.dumps(row, ensure_ascii=False) + "\n")

    def test_existing_ledger_is_counted_instead_of_crashing(self):
        self._seed([
            {"tool": "cli-a", "at": "2026-10-04T01:00:00", "ok": True},
            {"tool": "cli-a", "at": "2026-10-04T02:00:00", "ok": False},
            {"tool": "cli-b", "at": "2026-10-04T03:00:00", "ok": True},
        ])
        catalog = ToolCatalog(self.dir)
        self.assertEqual(catalog.invocation_count("cli-a"), 2)
        self.assertEqual(catalog.invocation_count("cli-b"), 1)
        self.assertEqual(catalog.last_invocation("cli-a"), "2026-10-04T02:00:00")
        stats = catalog.audit_stats()
        self.assertEqual(stats["tools_counted"], 2)
        self.assertFalse(stats["windowed"])

    def test_restart_then_append_keeps_counting_from_where_it_stopped(self):
        self._seed([{"tool": "cli-a", "at": "2026-10-04T01:00:00", "ok": True}])
        fresh = ToolCatalog(self.dir)
        self.assertEqual(fresh.invocation_count("cli-a"), 1)

        fresh.audit({"tool": "cli-a", "at": "2026-10-04T04:00:00", "ok": True})
        self.assertEqual(fresh.invocation_count("cli-a"), 2)

        again = ToolCatalog(self.dir)
        self.assertEqual(again.invocation_count("cli-a"), 2)
        again.audit({"tool": "cli-a", "at": "2026-10-04T05:00:00", "ok": True})
        self.assertEqual(again.invocation_count("cli-a"), 3)

    def test_truncated_ledger_recounts_from_zero_without_double_counting(self):
        self._seed([{"tool": "cli-a", "at": f"2026-10-04T0{i}:00:00", "ok": True} for i in range(1, 4)])
        catalog = ToolCatalog(self.dir)
        self.assertEqual(catalog.invocation_count("cli-a"), 3)

        # 文件被换小（轮转/手工截断）：续读位置已经比文件长度大，必须整本重数，
        # 不能把"位置钳一下继续"当成没发生 —— 那样计数会永远差一截。
        self._seed([{"tool": "cli-a", "at": "2026-10-04T09:00:00", "ok": True}])
        self.assertEqual(catalog.invocation_count("cli-a"), 1)


if __name__ == "__main__":
    unittest.main()
