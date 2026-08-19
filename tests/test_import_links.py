# -*- coding: utf-8 -*-

from pathlib import Path
import tempfile
import unittest
from unittest import mock

from openpyxl import Workbook

import app
import exporter


class _FakeText:
    def __init__(self, value="1."):
        self.value = value

    def get(self, _start, _end):
        return self.value

    def delete(self, _start, _end):
        self.value = ""

    def insert(self, _index, value):
        self.value = value

    def mark_set(self, _mark, _index):
        pass

    def see(self, _index):
        pass


class _FakeVar:
    def __init__(self, value=""):
        self.value = value

    def get(self):
        return self.value

    def set(self, value):
        self.value = value


class ImportLinksTests(unittest.TestCase):
    def _old_workbook(self, path: Path) -> None:
        workbook = Workbook()
        sheet = workbook.active
        sheet.title = "提取记录"
        sheet.append(exporter.OLD_HEADERS)
        sheet.append(
            [
                "分享在前 https://v.douyin.com/Third/",
                "第三条",
                "",
                0,
                0,
                "基本盘",
                "",
                None,
                3,
            ]
        )
        sheet.append(
            [
                "https://www.douyin.com/video/100",
                "第一条",
                "",
                0,
                0,
                "基本盘",
                "",
                None,
                1,
            ]
        )
        workbook.save(path)
        workbook.close()

    def test_import_old_workbook_populates_input_without_modifying_file(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "以前的提取记录.xlsx"
            self._old_workbook(path)
            original = path.read_bytes()

            instance = app.DouyinExtractorApp.__new__(app.DouyinExtractorApp)
            instance.running = False
            instance.refreshing = False
            instance.output_var = _FakeVar(temp)
            instance.input_text = _FakeText()
            instance.status_var = _FakeVar()
            instance.root = object()
            instance._style_input_sequences = mock.Mock()
            instance._save_input_cache = mock.Mock()

            with mock.patch.object(app.filedialog, "askopenfilename", return_value=str(path)):
                instance.import_links_from_workbook()

            self.assertIn("1. https://www.douyin.com/video/100", instance.input_text.value)
            self.assertIn("2. https://v.douyin.com/Third/", instance.input_text.value)
            self.assertTrue(instance.input_text.value.endswith("3."))
            self.assertIn("按文档顺序导入 2 条链接", instance.status_var.value)
            self.assertEqual(path.read_bytes(), original)
            instance._save_input_cache.assert_called_once_with()


if __name__ == "__main__":
    unittest.main()
