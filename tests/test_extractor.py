# -*- coding: utf-8 -*-

import json
from pathlib import Path
import tempfile
import threading
import unittest
from unittest import mock

import requests

import extractor
from tasking import TaskCancelled


class FakeResponse:
    def __init__(self, *, text="", url="", status=200, headers=None, chunks=None):
        self.text = text
        self.url = url
        self.status_code = status
        self.headers = headers or {}
        self._chunks = chunks or []
        self.closed = False

    def iter_content(self, chunk_size=0):
        for chunk in self._chunks:
            if isinstance(chunk, Exception):
                raise chunk
            yield chunk

    def close(self):
        self.closed = True


class FakeSession:
    def __init__(self, responses):
        self.responses = list(responses)
        self.headers = dict(extractor.BASE_HEADERS)

    def get(self, *args, **kwargs):
        return self.responses.pop(0)


def router_html(items):
    payload = {"loaderData": {"video": {"item_list": items}}}
    return f"<script>window._ROUTER_DATA = {json.dumps(payload)}</script>"


class ExtractorTests(unittest.TestCase):
    def test_find_item_requires_requested_aweme_id(self):
        data = {
            "first": {"item_list": [{"aweme_id": "wrong"}]},
            "second": {"item_list": [{"aweme_id": "target"}]},
        }
        self.assertEqual(extractor.find_item(data, "target")["aweme_id"], "target")
        self.assertIsNone(extractor.find_item(data, "missing"))

    def test_retry_returns_the_session_that_succeeded(self):
        original = FakeSession([FakeResponse(text=router_html([]))])
        recovered = FakeSession(
            [FakeResponse(text=router_html([{"aweme_id": "123", "desc": "ok"}]))]
        )
        with mock.patch.object(extractor, "_new_session", return_value=recovered), mock.patch.object(
            extractor, "interruptible_wait"
        ):
            session, item = extractor.fetch_item_with_session(original, "123", "video")
        self.assertIs(session, recovered)
        self.assertEqual(item["aweme_id"], "123")

    def test_page_without_router_data_is_classified_as_waf(self):
        sessions = [FakeSession([FakeResponse(text="blocked")]) for _ in range(4)]
        with mock.patch.object(extractor, "_new_session", side_effect=sessions[1:]), mock.patch.object(
            extractor, "interruptible_wait"
        ):
            with self.assertRaises(extractor.WafBlockedError):
                extractor.fetch_item_with_session(sessions[0], "123", "video")

    def test_failed_video_download_keeps_existing_target(self):
        item = {"video": {"play_addr": {"url_list": ["https://media/video"]}}}
        response = FakeResponse(
            headers={"content-type": "video/mp4", "content-length": "8"},
            chunks=[b"new", requests.ConnectionError("断网")],
        )
        session = FakeSession([response])
        with tempfile.TemporaryDirectory() as temp:
            target = Path(temp) / "1.mp4"
            target.write_bytes(b"old-media")
            with self.assertRaises(extractor.ExtractionError):
                extractor.download_video(session, item, target)
            self.assertEqual(target.read_bytes(), b"old-media")
            self.assertEqual(list(Path(temp).glob("*.part")), [])

    def test_html_response_is_not_saved_as_video(self):
        item = {"video": {"play_addr": {"url_list": ["https://media/video"]}}}
        response = FakeResponse(
            headers={"content-type": "text/html", "content-length": "6"},
            chunks=[b"blocked"],
        )
        with tempfile.TemporaryDirectory() as temp:
            target = Path(temp) / "1.mp4"
            with self.assertRaises(extractor.ExtractionError):
                extractor.download_video(FakeSession([response]), item, target)
            self.assertFalse(target.exists())

    def test_cancelled_download_keeps_existing_target(self):
        item = {"video": {"play_addr": {"url_list": ["https://media/video"]}}}
        response = FakeResponse(
            headers={"content-type": "video/mp4", "content-length": "3"},
            chunks=[b"new"],
        )
        event = threading.Event()
        event.set()
        with tempfile.TemporaryDirectory() as temp:
            target = Path(temp) / "1.mp4"
            target.write_bytes(b"old")
            with self.assertRaises(TaskCancelled):
                extractor.download_video(FakeSession([response]), item, target, cancel_event=event)
            self.assertEqual(target.read_bytes(), b"old")


if __name__ == "__main__":
    unittest.main()
