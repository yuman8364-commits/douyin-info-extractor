# -*- coding: utf-8 -*-

import json
from urllib.parse import quote
import unittest
import tempfile
from pathlib import Path

from openpyxl import Workbook

import account_monitor


def page(user, items=()):
    data = {"loaderData": {"user": user, "post": {"item_list": list(items)}}}
    return f'<script id="RENDER_DATA" type="application/json">{quote(json.dumps(data))}</script>'


class AccountMonitorTests(unittest.TestCase):
    def test_import_multiple_accounts_from_common_excel_columns(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "accounts.xlsx"
            workbook = Workbook()
            sheet = workbook.active
            sheet.title = "监控名单"
            sheet.append(["姓名", "抖音号", "备注", "主页链接"])
            sheet.append(["甲", "account-a", "", ""])
            sheet.append(["乙", "account-b", "", "https://www.douyin.com/user/sec-c"])
            sheet.append(["重复", "account-a", "", ""])
            workbook.save(path)
            workbook.close()

            result = account_monitor.import_accounts_from_table(path)

        self.assertEqual(result.accounts, ("account-a", "account-b", "sec-c"))
        self.assertEqual(len(result.matched_columns), 2)
        self.assertEqual(result.invalid_cells, ())

    def test_table_without_supported_header_does_not_guess(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "other.xlsx"
            workbook = Workbook()
            workbook.active.append(["姓名", "电话号码"])
            workbook.active.append(["甲", "123"])
            workbook.save(path)
            workbook.close()

            result = account_monitor.import_accounts_from_table(path)

        self.assertEqual(result.accounts, ())
        self.assertEqual(result.matched_columns, ())
    def test_normalize_profile_link_and_plain_id(self):
        self.assertEqual(account_monitor.normalize_account_input(" @my-id "), "my-id")
        self.assertEqual(
            account_monitor.normalize_account_input("https://www.douyin.com/user/MS4wLjAB?x=1"),
            "MS4wLjAB",
        )

    def test_parse_latest_public_work(self):
        user = {"unique_id": "abc", "sec_uid": "sec", "nickname": "作者"}
        snapshot = account_monitor.parse_account_snapshot(
            page(user, [
                {"aweme_id": "10", "create_time": 100, "desc": "旧", "author": {"sec_uid": "sec"}},
                {"aweme_id": "20", "create_time": 200, "desc": "新 视频", "author": {"sec_uid": "sec"}},
            ]),
            "abc",
        )
        self.assertEqual(snapshot.nickname, "作者")
        self.assertEqual(snapshot.latest_aweme_id, "20")
        self.assertEqual(snapshot.latest_create_time, 200)

    def test_explicit_cancelled_account_is_abnormal(self):
        with self.assertRaises(account_monitor.AccountAbnormalError):
            account_monitor.parse_account_snapshot("该帐号已注销", "abc")

    def test_first_check_is_baseline_then_first_new_work_alerts(self):
        empty = account_monitor.AccountSnapshot("abc", "作者", "", 0, "")
        first = account_monitor.AccountSnapshot("abc", "作者", "20", 200, "新视频")
        self.assertFalse(account_monitor.is_new_work({}, first))
        self.assertTrue(
            account_monitor.is_new_work(
                {
                    "baseline_initialized": True,
                    "latest_aweme_id": empty.latest_aweme_id,
                    "latest_create_time": empty.latest_create_time,
                },
                first,
            )
        )

    def test_captcha_is_not_misreported_as_account_abnormal(self):
        with self.assertRaises(account_monitor.MonitorError) as raised:
            account_monitor.parse_account_snapshot("ttgcaptcha verify_data", "abc")
        self.assertNotIsInstance(raised.exception, account_monitor.AccountAbnormalError)


if __name__ == "__main__":
    unittest.main()
