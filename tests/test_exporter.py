# -*- coding: utf-8 -*-

from pathlib import Path
import tempfile
import unittest
from unittest import mock

from openpyxl import Workbook, load_workbook

import exporter


class ExporterTests(unittest.TestCase):
    def _legacy_workbook(self, path: Path) -> None:
        workbook = Workbook()
        sheet = workbook.active
        sheet.title = "提取记录"
        sheet.append(exporter.BASE_HEADERS + ["人工备注"])
        sheet.append(
            ["https://v.douyin.com/Old/", "旧标题", "#旧", 1, 2, "人工分类", "作者", "正常", None, 1, "别覆盖"]
        )
        other = workbook.create_sheet("我的分析")
        other["A1"] = "保留这个工作表"
        workbook.save(path)
        workbook.close()

    def test_migration_preserves_extra_sheet_column_and_manual_type(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "提取记录.xlsx"
            self._legacy_workbook(path)
            records = exporter.read_records(path)
            records[1].update(
                {
                    "title": "新标题",
                    "likes": 99,
                    "aweme_id": "123",
                    "work_kind": "视频",
                    "updated_at": "2026-08-19 15:00:00",
                }
            )
            exporter.update_records(path, records, [1])

            workbook = load_workbook(path)
            self.assertIn("我的分析", workbook.sheetnames)
            self.assertEqual(workbook["我的分析"]["A1"].value, "保留这个工作表")
            sheet = workbook["提取记录"]
            headers = [cell.value for cell in sheet[1]]
            self.assertIn("作品ID", headers)
            self.assertIn("作品形式", headers)
            self.assertIn("最后更新", headers)
            self.assertEqual(sheet.cell(2, headers.index("类型") + 1).value, "人工分类")
            self.assertEqual(sheet.cell(2, headers.index("人工备注") + 1).value, "别覆盖")
            workbook.close()
            self.assertTrue(any((path.parent / "表格备份").glob("*.xlsx")))

    def test_replace_failure_keeps_original_workbook(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "提取记录.xlsx"
            self._legacy_workbook(path)
            original = path.read_bytes()
            records = exporter.read_records(path)
            records[1]["title"] = "不应落盘"
            with mock.patch.object(exporter.os, "replace", side_effect=PermissionError("locked")):
                with self.assertRaises(exporter.WorkbookInUseError):
                    exporter.update_records(path, records, [1])
            self.assertEqual(path.read_bytes(), original)
            self.assertEqual(list(path.parent.glob(".*.tmp.xlsx")), [])


if __name__ == "__main__":
    unittest.main()
