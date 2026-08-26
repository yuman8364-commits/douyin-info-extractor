# -*- coding: utf-8 -*-

from pathlib import Path
import queue
import tempfile
import threading
import unittest
from unittest import mock

import requests

import app
import exporter
import extractor


def fetched(aweme_id: str, title: str, kind: str = "video") -> extractor.FetchedRecord:
    fields = {
        "aweme_id": aweme_id,
        "title": title,
        "tags": "#测试",
        "likes": 10,
        "comments": 2,
        "author": "作者",
        "cover_url": None,
    }
    item = {"aweme_id": aweme_id, "video": {"play_addr": {"url_list": ["media"]}}}
    return extractor.FetchedRecord(
        requests.Session(),
        kind,
        aweme_id,
        f"https://www.douyin.com/{kind}/{aweme_id}",
        item,
        fields,
    )


class AppWorkflowTests(unittest.TestCase):
    def _app(self):
        instance = object.__new__(app.DouyinExtractorApp)
        instance.message_queue = queue.Queue()
        instance.cancel_event = threading.Event()
        instance.silent_thread = None
        instance.silent_refreshing = False
        return instance

    def test_user_write_force_closes_excel_wps_and_retries(self):
        locked = exporter.WorkbookInUseError("locked")
        with mock.patch.object(
            app.exporter, "update_records", side_effect=[locked, None]
        ) as update, mock.patch.object(
            app, "force_close_spreadsheet_apps", return_value=["EXCEL.EXE", "et.exe"]
        ) as close, mock.patch.object(app.time, "sleep"):
            app.update_records_force_close("提取记录.xlsx", {1: {"title": "新数据"}}, [1])

        self.assertEqual(update.call_count, 2)
        self.assertTrue(all(call.kwargs["keep_backup"] is False for call in update.call_args_list))
        close.assert_called_once()

    def test_browser_confirmed_unavailable_is_not_retried(self):
        context = mock.Mock()
        context.fetch_record.side_effect = extractor.TargetUnavailableError(
            "浏览器已跳转到其他作品"
        )
        logger = mock.Mock()

        record, status = app.fetch_with_retry(
            logger,
            1,
            "https://www.douyin.com/video/123",
            access_context=context,
        )

        self.assertIsNone(record)
        self.assertEqual(status, "目标作品已失效（浏览器自动跳转到其他作品）")
        context.fetch_record.assert_called_once()

    def test_checked_backup_option_is_forwarded_to_workbook_writer(self):
        with mock.patch.object(app.exporter, "update_records") as update:
            app.update_records_force_close(
                "提取记录.xlsx",
                {1: {"title": "新数据"}},
                [1],
                keep_backup=True,
            )

        self.assertTrue(update.call_args.kwargs["keep_backup"])

    def test_delete_write_force_closes_excel_wps_and_retries(self):
        locked = exporter.WorkbookInUseError("locked")
        with mock.patch.object(
            app.exporter, "delete_record", side_effect=[locked, True]
        ) as delete, mock.patch.object(
            app, "force_close_spreadsheet_apps", return_value=["EXCEL.EXE", "et.exe"]
        ) as close, mock.patch.object(app.time, "sleep"):
            deleted = app.delete_record_force_close("提取记录.xlsx", 2)

        self.assertTrue(deleted)
        self.assertEqual(delete.call_count, 2)
        close.assert_called_once()

    def test_automatic_startup_refresh_never_force_closes_office(self):
        locked = exporter.WorkbookInUseError("locked")
        with mock.patch.object(
            app.exporter, "update_records", side_effect=locked
        ) as update, mock.patch.object(app, "force_close_spreadsheet_apps") as close:
            with self.assertRaises(exporter.WorkbookInUseError):
                app.update_records_force_close(
                    "提取记录.xlsx",
                    {1: {"title": "新数据"}},
                    [1],
                    allow_force_close=False,
                )

        update.assert_called_once()
        close.assert_not_called()

    def test_force_close_targets_microsoft_excel_and_wps_spreadsheets(self):
        completed = mock.Mock(returncode=0)
        with mock.patch.object(app.subprocess, "run", return_value=completed) as run:
            closed = app.force_close_spreadsheet_apps()

        self.assertEqual(closed, ["EXCEL.EXE", "et.exe"])
        self.assertEqual(
            [call.args[0] for call in run.call_args_list],
            [
                ["taskkill", "/F", "/IM", "EXCEL.EXE"],
                ["taskkill", "/F", "/IM", "et.exe"],
            ],
        )

    def test_transactional_extract_preserves_caption_and_id_deduplicates(self):
        instance = self._app()
        first_url = "https://v.douyin.com/First/"
        second_url = "https://v.douyin.com/Second/"

        def fake_download(_session, _item, target, *_args, **_kwargs):
            Path(target).parent.mkdir(parents=True, exist_ok=True)
            Path(target).write_bytes(b"video")
            return Path(target), None

        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            with mock.patch.object(app, "fetch_with_retry", return_value=(fetched("123", "第一次"), None)), mock.patch.object(
                app.extractor, "download_video", side_effect=fake_download
            ):
                instance._work_safe([(1, first_url)], root)

            caption = root / "文案提取" / "1.txt"
            caption.write_text("用户手填文案", encoding="utf-8")
            rows = exporter.read_records(root / "提取记录.xlsx")
            self.assertEqual(rows[1]["aweme_id"], "123")
            self.assertEqual(rows[1]["work_kind"], "视频")
            self.assertEqual((root / "爆款视频" / "1.mp4").read_bytes(), b"video")
            while not instance.message_queue.empty():
                instance.message_queue.get_nowait()

            with mock.patch.object(app, "fetch_with_retry", return_value=(fetched("123", "第二次"), None)), mock.patch.object(
                app.extractor, "download_video"
            ) as download:
                instance._work_safe([(9, second_url)], root)
                download.assert_not_called()

            messages = []
            while not instance.message_queue.empty():
                messages.append(instance.message_queue.get_nowait())
            self.assertEqual([message.kind for message in messages], ["refreshed", "done"])
            self.assertIn("强制刷新 Excel", messages[0].payload["reason"])

            rows = exporter.read_records(root / "提取记录.xlsx")
            self.assertEqual(rows[1]["title"], "第二次")
            self.assertTrue(rows[1]["updated_at"])
            self.assertNotIn(9, rows)
            self.assertEqual(caption.read_text(encoding="utf-8"), "用户手填文案")

    def test_existing_workbook_link_refreshes_without_any_media_download(self):
        instance = self._app()
        existing_url = "https://v.douyin.com/Existing/"

        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            exporter.update_records(
                root / "提取记录.xlsx",
                {
                    1: {
                        "raw_input": existing_url,
                        "title": "旧标题",
                        "type": "人工分类",
                    }
                },
            )

            with mock.patch.object(
                app, "fetch_with_retry", return_value=(fetched("456", "强刷后的标题"), None)
            ), mock.patch.object(app.extractor, "download_video") as download:
                instance._work_safe([(1, existing_url)], root)
                download.assert_not_called()

            messages = []
            while not instance.message_queue.empty():
                messages.append(instance.message_queue.get_nowait())
            self.assertEqual([message.kind for message in messages], ["refreshed", "done"])

            rows = exporter.read_records(root / "提取记录.xlsx")
            self.assertEqual(rows[1]["title"], "强刷后的标题")
            self.assertEqual(rows[1]["aweme_id"], "456")
            self.assertEqual(rows[1]["type"], "人工分类")
            self.assertFalse((root / "爆款视频" / "1.mp4").exists())

    def test_existing_link_failure_updates_status_but_preserves_old_data(self):
        instance = self._app()
        existing_url = "https://v.douyin.com/Unavailable/"
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            exporter.update_records(
                root / "提取记录.xlsx",
                {
                    3: {
                        "raw_input": existing_url,
                        "title": "旧标题",
                        "likes": 88,
                        "type": "人工分类",
                        "status": "正常",
                    }
                },
            )

            failure = "目标作品暂不可用（页面未返回目标作品）"
            with mock.patch.object(app, "fetch_with_retry", return_value=(None, failure)):
                instance._work_safe([(3, existing_url)], root)

            rows = exporter.read_records(root / "提取记录.xlsx")
            self.assertEqual(rows[3]["status"], failure)
            self.assertEqual(rows[3]["title"], "旧标题")
            self.assertEqual(rows[3]["likes"], 88)
            self.assertEqual(rows[3]["type"], "人工分类")
            self.assertTrue(rows[3]["updated_at"])

    def test_refresh_failure_replaces_normal_status_but_keeps_metadata(self):
        instance = self._app()
        instance.auto_refresh = False
        instance.backup_enabled = False
        existing_url = "https://v.douyin.com/UnavailableRefresh/"
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            original = {
                "raw_input": existing_url,
                "title": "仍需保留的标题",
                "likes": 66,
                "status": "正常",
            }
            exporter.update_records(root / "提取记录.xlsx", {3: original})

            failure = "风控或网络异常（被抖音风控拦截）"
            with mock.patch.object(app, "fetch_with_retry", return_value=(None, failure)):
                instance._refresh_work_safe(root, [(3, original)])

            rows = exporter.read_records(root / "提取记录.xlsx")
            self.assertEqual(rows[3]["status"], failure)
            self.assertEqual(rows[3]["title"], "仍需保留的标题")
            self.assertEqual(rows[3]["likes"], 66)
            self.assertTrue(rows[3]["updated_at"])

    def test_pre_cancelled_job_finishes_without_writing(self):
        instance = self._app()
        instance.cancel_event.set()
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            instance._work_safe([(1, "https://v.douyin.com/Cancel/")], root)
            kinds = [instance.message_queue.get_nowait().kind for _ in range(2)]
            self.assertEqual(kinds, ["cancelled", "done"])
            self.assertFalse((root / "提取记录.xlsx").exists())

    def test_startup_auto_refresh_entrypoint_is_removed(self):
        self.assertFalse(hasattr(app.DouyinExtractorApp, "_auto_refresh_existing_records"))

    def test_refresh_browser_failure_updates_current_only_and_pauses_batch(self):
        instance = self._app()
        instance.auto_refresh = False
        instance.backup_enabled = False
        first_url = "https://v.douyin.com/Challenge/"
        second_url = "https://v.douyin.com/Unchecked/"
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            records = {
                1: {"raw_input": first_url, "title": "第一条", "status": "正常"},
                2: {"raw_input": second_url, "title": "第二条", "status": "正常"},
            }
            exporter.update_records(root / "提取记录.xlsx", records)
            blocked = extractor.BrowserVerificationError(
                "未完成浏览器验证",
                status="风控或验证异常（未完成浏览器验证）",
            )
            with mock.patch.object(app, "fetch_with_retry", side_effect=blocked) as fetch:
                instance._refresh_work_safe(root, list(records.items()))

            self.assertEqual(fetch.call_count, 1)
            rows = exporter.read_records(root / "提取记录.xlsx")
            self.assertEqual(rows[1]["status"], "风控或验证异常（未完成浏览器验证）")
            self.assertEqual(rows[2]["status"], "正常")
            self.assertFalse(rows[2].get("updated_at"))
            messages = []
            while not instance.message_queue.empty():
                messages.append(instance.message_queue.get_nowait())
            done = next(message for message in messages if message.kind == "rdone")
            self.assertEqual(done.payload["unchecked"], 1)
            self.assertIn("批次已暂停", done.payload["message"])

    def test_refresh_verification_notice_does_not_break_ui_poller(self):
        instance = self._app()
        instance.refreshing = True
        instance.status_var = mock.Mock()
        instance.progress_label = mock.Mock()
        instance.root = mock.Mock()
        instance._post("rverification", {"event": "verification_required"}, "链接已失效")

        instance._poll_refresh()

        instance.status_var.set.assert_called_once_with("链接已失效")
        instance.progress_label.config.assert_called_once_with(text="等待浏览器验证…")
        instance.root.after.assert_called_once_with(100, instance._poll_refresh)

    def test_refresh_row_highlight_tracks_sequence_and_scrolls_into_view(self):
        instance = self._app()
        instance.tree = mock.Mock()
        instance.records = {
            "row-a": {"seq": 1},
            "row-b": {"seq": 2},
        }

        instance._highlight_record(2, "refresh_failure")

        instance.tree.item.assert_called_once_with(
            "row-b", tags=("refresh_failure",)
        )
        instance.tree.see.assert_called_once_with("row-b")

    def test_refresh_terminal_message_is_posted_after_access_context_closes(self):
        instance = self._app()
        instance.auto_refresh = False
        instance.backup_enabled = False
        events = []

        class FakeAccessContext:
            def __init__(self, *_args, **_kwargs):
                pass

            def close(self):
                events.append("closed")

        original_post = instance._post

        def record_post(kind, payload=None, extra=None):
            if kind in {"rdone", "rcancelled", "rerror"}:
                events.append(kind)
            original_post(kind, payload, extra)

        instance._post = record_post
        instance.cancel_event.set()
        with tempfile.TemporaryDirectory() as temp, mock.patch.object(
            app.extractor, "AccessContext", FakeAccessContext
        ):
            instance._refresh_work_safe(Path(temp), [])

        self.assertEqual(events, ["closed", "rdone"])

    def test_open_records_opens_existing_workbook(self):
        instance = self._app()
        instance.output_var = mock.Mock()
        instance.status_var = mock.Mock()
        instance.root = mock.Mock()
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            workbook = root / "提取记录.xlsx"
            workbook.write_bytes(b"existing workbook")
            instance.output_var.get.return_value = str(root)

            with mock.patch.object(app.os, "startfile", create=True) as startfile:
                instance.open_records()

            startfile.assert_called_once_with(workbook)
            instance.status_var.set.assert_called_once_with("已打开「提取记录.xlsx」")

    def test_open_records_missing_workbook_only_shows_message(self):
        instance = self._app()
        instance.output_var = mock.Mock()
        instance.status_var = mock.Mock()
        instance.root = mock.Mock()
        with tempfile.TemporaryDirectory() as temp:
            instance.output_var.get.return_value = temp

            with mock.patch.object(
                app.os, "startfile", create=True
            ) as startfile, mock.patch.object(app.messagebox, "showinfo") as showinfo:
                instance.open_records()

            startfile.assert_not_called()
            showinfo.assert_called_once()
            self.assertFalse((Path(temp) / "提取记录.xlsx").exists())
            instance.status_var.set.assert_called_once_with(
                "输出目录里没有「提取记录.xlsx」"
            )


if __name__ == "__main__":
    unittest.main()
