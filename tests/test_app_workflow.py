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

            with mock.patch.object(app, "fetch_with_retry", return_value=(fetched("123", "第二次"), None)), mock.patch.object(
                app.extractor, "download_video"
            ) as download:
                instance._work_safe([(9, second_url)], root)
                download.assert_not_called()

            rows = exporter.read_records(root / "提取记录.xlsx")
            self.assertEqual(rows[1]["title"], "第二次")
            self.assertNotIn(9, rows)
            self.assertEqual(caption.read_text(encoding="utf-8"), "用户手填文案")

    def test_pre_cancelled_job_finishes_without_writing(self):
        instance = self._app()
        instance.cancel_event.set()
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            instance._work_safe([(1, "https://v.douyin.com/Cancel/")], root)
            kinds = [instance.message_queue.get_nowait().kind for _ in range(2)]
            self.assertEqual(kinds, ["cancelled", "done"])
            self.assertFalse((root / "提取记录.xlsx").exists())

    def test_startup_directly_refreshes_only_cached_existing_records(self):
        first_url = "https://v.douyin.com/First/"
        second_url = "https://v.douyin.com/Second/"
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            videos = root / "爆款视频"
            videos.mkdir()
            (videos / "1.mp4").write_bytes(b"existing")
            records = {
                1: {"raw_input": first_url, "title": "第一条"},
                2: {"raw_input": second_url, "title": "第二条"},
            }
            exporter.update_records(root / "提取记录.xlsx", records)

            instance = self._app()
            instance.running = False
            instance.refreshing = False
            instance.output_var = mock.Mock()
            instance.output_var.get.return_value = str(root)
            instance.input_text = mock.Mock()
            instance.input_text.get.return_value = f"1. {first_url}\n------------\n2."
            instance._enforce_input_sequences = mock.Mock()
            instance._start_table_refresh = mock.Mock()

            instance._auto_refresh_existing_records()

            instance._start_table_refresh.assert_called_once()
            args, kwargs = instance._start_table_refresh.call_args
            self.assertEqual(args[0], root)
            self.assertEqual([seq for seq, _record in args[1]], [1])
            self.assertTrue(kwargs["automatic"])


if __name__ == "__main__":
    unittest.main()
