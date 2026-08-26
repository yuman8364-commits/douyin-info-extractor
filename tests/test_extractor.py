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
        self.content = text.encode("utf-8")
        self.apparent_encoding = "utf-8"
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
            "first": {"item_list": [{"aweme_id": "wrong", "statistics": {}}]},
            "second": {"item_list": [{"aweme_id": "target", "statistics": {}}]},
        }
        self.assertEqual(extractor.find_item(data, "target")["aweme_id"], "target")
        self.assertIsNone(extractor.find_item(data, "missing"))

    def test_find_item_accepts_direct_aweme_detail_response(self):
        data = {
            "aweme_detail": {
                "aweme_id": "123",
                "desc": "详情接口",
                "statistics": {},
            }
        }
        self.assertEqual(extractor.find_item(data, "123")["desc"], "详情接口")

    def test_find_item_rejects_id_only_placeholder(self):
        self.assertIsNone(extractor.find_item({"aweme_id": "123"}, "123"))

    def test_retry_reuses_batch_session_and_returns_success(self):
        original = FakeSession(
            [
                FakeResponse(text=router_html([])),
                FakeResponse(
                    text=router_html(
                        [{"aweme_id": "123", "desc": "ok", "statistics": {}}]
                    )
                ),
            ]
        )
        with mock.patch.object(extractor, "interruptible_wait"):
            session, item = extractor.fetch_item_with_session(original, "123", "video")
        self.assertIs(session, original)
        self.assertEqual(item["aweme_id"], "123")

    def test_page_without_router_data_is_classified_as_structure_change(self):
        session = FakeSession([FakeResponse(text="page"), FakeResponse(text="page")])
        with mock.patch.object(extractor, "interruptible_wait"):
            with self.assertRaises(extractor.PageStructureError):
                extractor.fetch_item_with_session(session, "123", "video")

    def test_captcha_page_stops_http_attempts_immediately(self):
        html = '<title>验证码中间页</title><script>TTGCaptcha.init({verify_data:{}})</script>'
        session = FakeSession(
            [
                FakeResponse(text=html),
                FakeResponse(text=router_html([{"aweme_id": "123", "statistics": {}}])),
            ]
        )
        with mock.patch.object(extractor, "interruptible_wait") as wait:
            with self.assertRaises(extractor.CaptchaChallengeError):
                extractor.fetch_item_with_session(session, "123", "video")
        self.assertEqual(len(session.responses), 1)
        wait.assert_not_called()

    def test_access_context_uses_browser_fallback_and_exact_id(self):
        context = extractor.AccessContext(Path("profile"))
        context.session = FakeSession(
            [
                FakeResponse(url="https://www.douyin.com/video/123"),
                FakeResponse(text='<title>验证码中间页</title>'),
            ]
        )
        item = {"aweme_id": "123", "desc": "浏览器恢复", "statistics": {}}
        with mock.patch.object(context, "_fetch_with_browser", return_value=item) as browser:
            record = context.fetch_record("https://www.douyin.com/video/123")
        browser.assert_called_once()
        self.assertEqual(record.aweme_id, "123")
        context.close()

    def test_browser_redirect_confirmation_wait_defaults_to_ten_seconds(self):
        context = extractor.AccessContext(Path("profile"))
        self.assertEqual(context.redirect_confirmation_delay, 10.0)
        context.close()

    def test_access_context_rejects_wrong_browser_item(self):
        context = extractor.AccessContext(Path("profile"))
        context.session = FakeSession(
            [
                FakeResponse(url="https://www.douyin.com/video/123"),
                FakeResponse(text='<title>验证码中间页</title>'),
            ]
        )
        with mock.patch.object(
            context, "_fetch_with_browser", return_value={"aweme_id": "999"}
        ):
            with self.assertRaises(extractor.PageStructureError):
                context.fetch_record("https://www.douyin.com/video/123")
        context.close()

    def test_browser_cookies_and_user_agent_are_synced_to_http_session(self):
        context = extractor.AccessContext(Path("profile"))
        context._browser_context = mock.Mock()
        context._browser_context.cookies.return_value = [
            {"name": "verify", "value": "ok", "domain": ".douyin.com", "path": "/"}
        ]
        page = mock.Mock()
        page.evaluate.return_value = "Browser-UA"
        context._sync_browser_session(page)
        self.assertEqual(context.session.headers["User-Agent"], "Browser-UA")
        self.assertEqual(context.session.cookies.get("verify", domain=".douyin.com"), "ok")
        context._browser_context = None
        context.close()

    def test_batch_reuses_browser_after_first_http_block(self):
        context = extractor.AccessContext(Path("profile"))
        context._http_blocked = True
        context._browser_context = mock.Mock()
        context.session = FakeSession(
            [FakeResponse(url="https://www.douyin.com/video/456")]
        )
        item = {"aweme_id": "456", "statistics": {}, "desc": "第二条"}
        with mock.patch.object(
            context, "_fetch_with_browser", return_value=item
        ) as browser, mock.patch.object(extractor, "fetch_item_with_session") as http:
            record = context.fetch_record("https://www.douyin.com/video/456")
        self.assertEqual(record.aweme_id, "456")
        browser.assert_called_once()
        http.assert_not_called()
        context._browser_context = None
        context.close()

    def test_browser_redirect_to_other_item_marks_target_unavailable(self):
        context = extractor.AccessContext(
            Path("profile"), redirect_confirmation_delay=0
        )
        page = mock.Mock()
        page.url = "https://www.douyin.com/video/999"
        page.is_closed.return_value = False
        page.evaluate.return_value = None
        browser_context = mock.Mock()
        context._browser_context = browser_context
        context._browser_page = page
        with self.assertRaises(extractor.TargetUnavailableError):
            context._fetch_with_browser("https://www.douyin.com/video/123", "123")
        page.wait_for_timeout.assert_not_called()
        context._browser_context = None
        context.close()

    def test_non_item_redirect_is_not_treated_as_unavailable(self):
        self.assertIsNone(
            extractor.redirected_item_id(
                "https://www.douyin.com/passport/login", "123"
            )
        )
        self.assertIsNone(
            extractor.redirected_item_id(
                "https://www.douyin.com/video/123", "123"
            )
        )
        self.assertEqual(
            extractor.redirected_item_id(
                "https://www.douyin.com/note/999", "123"
            ),
            "999",
        )
        real_unavailable_redirect = (
            "https://www.douyin.com/jingxuan?"
            "previous_page=web_video_404_link&modal_id=999"
        )
        self.assertEqual(
            extractor.redirected_item_id(real_unavailable_redirect, "123"), "999"
        )
        self.assertTrue(
            extractor.is_explicit_unavailable_redirect(real_unavailable_redirect)
        )
        self.assertFalse(
            extractor.is_explicit_unavailable_redirect(
                "https://www.douyin.com/jingxuan?modal_id=999"
            )
        )

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
