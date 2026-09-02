# -*- coding: utf-8 -*-
"""抖音作品信息提取引擎（HTTP 优先，验证码时使用独立浏览器兜底）。

数据来源：用移动端 User-Agent 请求 iesdouyin.com 分享页，
解析页面中的 window._ROUTER_DATA，提取标题、标签、点赞数、评论数、
博主、封面，以及无水印视频地址和图集原图。
"""

from __future__ import annotations

import base64
import json
import logging
import os
import random
import re
import time
from dataclasses import dataclass
from pathlib import Path
import threading
import uuid
from typing import Callable, Optional
from urllib.parse import parse_qs, urlparse

import requests

from tasking import TaskCancelled, ensure_not_cancelled, interruptible_wait

logger = logging.getLogger(__name__)

UA_IPHONE = (
    "Mozilla/5.0 (iPhone; CPU iPhone OS 16_6 like Mac OS X) "
    "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/16.6 Mobile/15E148 Safari/604.1"
)
UA_ANDROID = (
    "Mozilla/5.0 (Linux; Android 8.0.0; SM-G955U Build/R16NW) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/116.0.0.0 Mobile Safari/537.36"
)

BASE_HEADERS = {
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "zh-CN,zh;q=0.9",
    "User-Agent": UA_IPHONE,
}

SHARE_BASE = "https://www.iesdouyin.com/share"
REFERER = "https://www.iesdouyin.com/"
PAGE_TIMEOUT = (10, 20)
MEDIA_TIMEOUT = (15, 60)
NETWORK_RETRY_ATTEMPTS = 2
MEDIA_RETRY_ATTEMPTS = 2
TRANSIENT_STATUS_CODES = frozenset({408, *range(500, 600)})

DOUYIN_URL_RE = re.compile(
    r"https?://"
    r"(?:"
    r"v\.douyin\.com/[A-Za-z0-9_-]+/?|"
    r"(?:www\.)?douyin\.com/(?:video|note|slides)/\d+|"
    r"www\.iesdouyin\.com/share/(?:video|note)/\d+"
    r")",
    re.IGNORECASE,
)
ID_KIND_RE = re.compile(r"/(video|note|slides)/(\d+)")
ROUTER_RE = re.compile(r"window\._ROUTER_DATA\s*=\s*(.*?)</script>", re.S)

ProgressCallback = Callable[[int, int], None]


class ExtractionError(Exception):
    """提取失败基类。"""


class InvalidLinkError(ExtractionError):
    pass


class LoginRequiredError(ExtractionError):
    pass


class TargetUnavailableError(LoginRequiredError):
    """浏览器确认目标作品已失效或已被平台跳转替代。"""


class WafBlockedError(ExtractionError):
    pass


class NetworkRequestError(WafBlockedError):
    """连接、TLS、DNS、超时或上游临时错误。"""


class CaptchaChallengeError(WafBlockedError):
    """HTTP 200 实际返回抖音验证码中间页。"""


class PageStructureError(ExtractionError):
    """页面可访问，但没有找到与目标 ID 匹配的结构化作品数据。"""


class BrowserVerificationError(ExtractionError):
    """浏览器验证无法完成；调用方应暂停整批，避免继续触发风控。"""

    def __init__(self, message: str, *, status: str | None = None):
        super().__init__(message)
        self.status = status or f"风控或验证异常（{message}）"


class ResponseValidationError(ExtractionError):
    """响应虽然成功返回，但内容不是预期的媒体。"""


@dataclass(slots=True)
class FetchedRecord:
    """一次成功抓取的完整上下文。"""

    session: requests.Session
    kind: str
    aweme_id: str
    canonical_url: str
    item: dict
    fields: dict

    def __iter__(self):
        """兼容旧的五元组解包。"""
        yield self.session
        yield self.kind
        yield self.aweme_id
        yield self.item
        yield self.fields


BrowserNotice = Callable[[str, str], None]


class AccessContext:
    """单个提取批次共享的 HTTP 会话与可选 Playwright 浏览器上下文。"""

    def __init__(
        self,
        browser_profile_root: Path | None,
        cancel_event: threading.Event | None = None,
        notice: BrowserNotice | None = None,
        verification_timeout: float = 300.0,
        redirect_confirmation_delay: float = 10.0,
    ):
        self.session = _new_session()
        self.browser_profile_root = (
            Path(browser_profile_root) if browser_profile_root is not None else None
        )
        self.cancel_event = cancel_event
        self.notice = notice
        self.verification_timeout = verification_timeout
        self.redirect_confirmation_delay = redirect_confirmation_delay
        self._playwright = None
        self._browser_context = None
        self._browser_page = None
        self._browser_channel: str | None = None
        self._http_blocked = False

    def __enter__(self):
        return self

    def __exit__(self, _exc_type, _exc_value, _traceback):
        self.close()

    def _notify(self, event: str, message: str) -> None:
        logger.info("浏览器兜底：%s", message)
        if self.notice is not None:
            self.notice(event, message)

    @property
    def browser_context(self):
        """返回当前批次的浏览器上下文，供媒体下载复用其网络链路。"""
        return self._browser_context

    def ensure_browser_context(self):
        """媒体网络失败时惰性启动专用浏览器，不为正常 HTTP 任务提前弹窗。"""
        if self._browser_context is None:
            self._notify("network_fallback", "媒体网络请求失败，正在切换应用专用浏览器网络")
        if self.browser_profile_root is None:
            return None
        return self._start_browser()

    def close(self) -> None:
        context, playwright = self._browser_context, self._playwright
        self._browser_context = None
        self._browser_page = None
        self._playwright = None
        if context is not None:
            try:
                context.close()
            except Exception:
                logger.debug("关闭浏览器上下文失败", exc_info=True)
        if playwright is not None:
            try:
                playwright.stop()
            except Exception:
                logger.debug("停止 Playwright 失败", exc_info=True)
        try:
            self.session.close()
        except Exception:
            logger.debug("关闭 HTTP 会话失败", exc_info=True)

    def fetch_record(self, text: str) -> FetchedRecord:
        """优先用共享 HTTP 会话抓取；结构/验证码异常时转入浏览器。"""
        url = extract_url(text)
        final_url = url
        try:
            # 长链接已经带有作品 ID，不必先经过一次短链解析请求；这也让
            # Requests 在本机 TUN/Fake-IP 不可用时可以直接切换浏览器兜底。
            kind, aweme_id = parse_id_kind(url)
        except InvalidLinkError:
            try:
                final_url = resolve_share_url(self.session, url, self.cancel_event)
                kind, aweme_id = parse_id_kind(final_url)
            except NetworkRequestError:
                if self.browser_profile_root is None:
                    raise
                self._http_blocked = True
                self._notify(
                    "network_fallback",
                    "短链接网络解析失败，改用应用专用浏览器网络继续处理",
                )
                final_url = self._resolve_short_url_with_browser(url)
                target = _target_from_url(final_url)
                if target is None:
                    raise BrowserVerificationError("浏览器打开短链接后没有找到作品地址")
                kind, aweme_id = target
        browser_kind = "video" if kind == "video" else "note"
        browser_url = f"https://www.douyin.com/{browser_kind}/{aweme_id}"
        if self._http_blocked:
            item = self._fetch_with_browser(browser_url, aweme_id)
        else:
            try:
                self.session, item = fetch_item_with_session(
                    self.session, aweme_id, kind, self.cancel_event
                )
            except (CaptchaChallengeError, PageStructureError, NetworkRequestError) as exc:
                if self.browser_profile_root is None:
                    raise
                self._http_blocked = True
                if isinstance(exc, NetworkRequestError):
                    self._notify(
                        "network_fallback",
                        "HTTP 网络请求失败，改用应用专用浏览器网络继续处理",
                    )
                else:
                    reason = (
                        "检测到验证码中间页"
                        if isinstance(exc, CaptchaChallengeError)
                        else "作品页结构无法解析"
                    )
                    self._notify(
                        "verification_required",
                        f"{reason}，请在弹出的浏览器中完成验证",
                    )
                item = self._fetch_with_browser(browser_url, aweme_id)

        if _item_id(item) != aweme_id:
            raise PageStructureError("页面返回了其他作品的数据，已拒绝保存")
        fields = extract_fields(item, aweme_id)
        canonical_kind = "video" if kind == "video" else "note"
        canonical_url = f"https://www.douyin.com/{canonical_kind}/{aweme_id}"
        return FetchedRecord(self.session, kind, aweme_id, canonical_url, item, fields)

    def _resolve_short_url_with_browser(self, url: str) -> str:
        """用浏览器解析 Requests 无法访问的抖音短链接。"""
        context = self._start_browser()
        page = self._browser_page
        if page is None or page.is_closed():
            page = context.new_page()
            self._browser_page = page
        try:
            self._goto_browser(page, url)
            resolved = page.url
        except TaskCancelled:
            raise
        except Exception as exc:
            raise BrowserVerificationError(f"浏览器解析短链接失败：{exc}") from exc
        if _target_from_url(resolved) is None:
            raise BrowserVerificationError("浏览器打开短链接后没有找到作品地址")
        return resolved

    def _goto_browser(self, page, url: str) -> None:
        """浏览器导航遇到可恢复的网络错误时只重试一次。"""
        for attempt in range(2):
            ensure_not_cancelled(self.cancel_event)
            try:
                page.goto(url, wait_until="domcontentloaded", timeout=30_000)
                return
            except TaskCancelled:
                raise
            except Exception as exc:
                if attempt == 0 and _is_browser_transport_error(exc):
                    self._notify("network_retry", "浏览器网络导航超时或连接中断，正在重试")
                    try:
                        page.wait_for_timeout(1000)
                    except Exception:
                        pass
                    continue
                raise

    def _start_browser(self):
        if self._browser_context is not None:
            return self._browser_context
        ensure_not_cancelled(self.cancel_event)
        try:
            from playwright.sync_api import sync_playwright
        except Exception as exc:
            raise BrowserVerificationError(
                "浏览器组件不可用，请重新安装完整发布目录"
            ) from exc

        self.browser_profile_root.mkdir(parents=True, exist_ok=True)
        playwright = sync_playwright().start()
        errors: list[str] = []
        for channel in ("msedge", "chrome"):
            ensure_not_cancelled(self.cancel_event)
            profile = self.browser_profile_root / channel
            profile.mkdir(parents=True, exist_ok=True)
            try:
                context = playwright.chromium.launch_persistent_context(
                    user_data_dir=str(profile),
                    channel=channel,
                    headless=False,
                    viewport=None,
                    locale="zh-CN",
                    accept_downloads=False,
                )
                self._playwright = playwright
                self._browser_context = context
                self._browser_channel = channel
                self._browser_page = context.pages[0] if context.pages else context.new_page()
                return context
            except Exception as exc:
                errors.append(f"{channel}: {exc}")
        playwright.stop()
        detail = "；".join(errors)
        logger.warning("无法启动系统浏览器：%s", detail)
        raise BrowserVerificationError(
            "无法启动系统 Edge/Chrome，请关闭残留的工具专用浏览器后重试"
        )

    def _sync_browser_session(self, page) -> None:
        try:
            user_agent = page.evaluate("navigator.userAgent")
            if user_agent:
                self.session.headers["User-Agent"] = str(user_agent)
            for cookie in self._browser_context.cookies():
                kwargs = {"path": cookie.get("path") or "/"}
                domain = cookie.get("domain")
                if domain:
                    kwargs["domain"] = domain
                self.session.cookies.set(cookie["name"], cookie["value"], **kwargs)
        except Exception as exc:
            logger.warning("同步浏览器验证状态失败：%s", exc)

    def _fetch_with_browser(self, final_url: str, aweme_id: str) -> dict:
        """等待用户完成验证，并从浏览器网络响应中取得精确作品数据。"""
        context = self._start_browser()
        page = self._browser_page
        if page is None or page.is_closed():
            page = context.new_page()
            self._browser_page = page

        matched: dict[str, dict] = {}

        def capture(response) -> None:
            if matched:
                return
            content_type = (response.headers.get("content-type") or "").lower()
            if "json" not in content_type and not any(
                marker in response.url for marker in ("aweme/detail", "iteminfo", "post")
            ):
                return
            try:
                data = response.json()
            except Exception:
                return
            item = find_item(data, aweme_id)
            if item is not None:
                matched["item"] = item

        page.on("response", capture)
        try:
            self._goto_browser(page, final_url)
            navigation_started = time.monotonic()
            deadline = navigation_started + self.verification_timeout
            while time.monotonic() < deadline:
                ensure_not_cancelled(self.cancel_event)
                if page.is_closed():
                    raise BrowserVerificationError("未完成浏览器验证，窗口已关闭")
                redirected_id = redirected_item_id(page.url, aweme_id)
                redirect_observation_complete = (
                    time.monotonic() - navigation_started
                    >= self.redirect_confirmation_delay
                )
                explicit_unavailable = is_explicit_unavailable_redirect(page.url)
                if redirect_observation_complete and (
                    redirected_id is not None or explicit_unavailable
                ):
                    self._notify(
                        "target_unavailable",
                        "目标作品已失效，浏览器已自动跳转到其他作品",
                    )
                    detail = (
                        f"并推荐作品 {redirected_id}"
                        if redirected_id is not None
                        else ""
                    )
                    raise TargetUnavailableError(
                        f"浏览器确认目标作品 {aweme_id} 已返回 404{detail}"
                    )
                if matched:
                    self._sync_browser_session(page)
                    self._notify("verification_succeeded", "浏览器验证成功，继续处理当前批次")
                    return matched["item"]
                try:
                    router_data = page.evaluate("() => window._ROUTER_DATA || null")
                    item = find_item(router_data, aweme_id)
                    if item is not None:
                        self._sync_browser_session(page)
                        self._notify("verification_succeeded", "浏览器验证成功，继续处理当前批次")
                        return item
                except Exception:
                    if page.is_closed():
                        raise BrowserVerificationError("未完成浏览器验证，窗口已关闭")
                page.wait_for_timeout(500)
        except TaskCancelled:
            raise
        except TargetUnavailableError:
            raise
        except BrowserVerificationError:
            raise
        except Exception as exc:
            raise BrowserVerificationError(f"浏览器验证失败：{exc}") from exc
        finally:
            try:
                page.remove_listener("response", capture)
            except Exception:
                pass
        try:
            page_text = f"{page.title()}\n{page.content()}".lower()
        except Exception:
            page_text = ""
        if any(marker in page_text for marker in ("验证码中间页", "ttgcaptcha", "verify_data")):
            raise BrowserVerificationError(
                "未在 5 分钟内完成浏览器验证",
                status="风控或验证异常（未完成浏览器验证）",
            )
        raise BrowserVerificationError(
            "浏览器已打开作品页，但未取得匹配的作品数据",
            status="获取失败（页面结构可能已变化）",
        )


def extract_url(text: str) -> str:
    """从整段分享文案中提取第一条抖音链接，找不到时抛出 InvalidLinkError。"""
    match = DOUYIN_URL_RE.search(text or "")
    if not match:
        raise InvalidLinkError("粘贴内容中没有找到有效的抖音链接")
    return match.group(0)


def extract_urls(text: str) -> list[str]:
    """从整段文字中提取全部抖音链接（去重、保持出现顺序）；没有则返回空列表。"""
    found = DOUYIN_URL_RE.findall(text or "")
    return list(dict.fromkeys(found))


def _target_from_url(current_url: str) -> tuple[str, str] | None:
    """从浏览器最终地址提取作品类型和 ID，也兼容失效跳转的查询参数。"""
    parsed = urlparse(current_url or "")
    match = ID_KIND_RE.search(parsed.path)
    if match:
        kind_raw, aweme_id = match.groups()
        return ("video" if kind_raw == "video" else "note"), aweme_id

    query = parse_qs(parsed.query)
    for key in ("aweme_id", "item_id", "modal_id"):
        for value in query.get(key) or []:
            if str(value).isdigit():
                kind = "note" if "/note/" in parsed.path or "/slides/" in parsed.path else "video"
                return kind, str(value)
    return None


def _is_browser_transport_error(exc: Exception) -> bool:
    """识别 Playwright 导航层面的临时网络错误，不把业务页错误重复放大。"""
    lowered = str(exc).lower()
    return any(
        marker in lowered
        for marker in (
            "timeout",
            "err_connection",
            "err_internet",
            "err_name_not_resolved",
            "err_proxy",
            "err_timed_out",
            "network",
            "dns",
        )
    )


def _retry_delay(attempt: int, retry_after: str | None = None) -> float:
    """生成有上限的退避时间，避免服务端 Retry-After 阻塞任务过久。"""
    if retry_after:
        try:
            return min(8.0, max(0.0, float(retry_after)))
        except (TypeError, ValueError):
            pass
    return min(8.0, float(2 ** max(0, attempt - 1)) + random.random())


def _get_with_retry(
    session: requests.Session,
    url: str,
    *,
    timeout,
    cancel_event: threading.Event | None = None,
    phase: str = "网络请求",
    attempts: int = NETWORK_RETRY_ATTEMPTS,
    **kwargs,
):
    """执行只读 GET；仅重试连接异常和明确的上游临时状态。"""
    attempts = max(1, int(attempts))
    last_error: Exception | None = None
    for attempt in range(1, attempts + 1):
        ensure_not_cancelled(cancel_event)
        try:
            response = session.get(url, timeout=timeout, **kwargs)
        except requests.RequestException as exc:
            last_error = exc
            retry_after = None
            status_detail = exc.__class__.__name__
        else:
            if response.status_code not in TRANSIENT_STATUS_CODES:
                return response
            retry_after = response.headers.get("retry-after")
            status_detail = f"HTTP {response.status_code}"
            response.close()
            last_error = NetworkRequestError(f"{phase}暂时不可用（{status_detail}）")

        if attempt < attempts:
            logger.info(
                "%s第 %d/%d 次失败（%s），等待后重试",
                phase,
                attempt,
                attempts,
                status_detail,
            )
            interruptible_wait(_retry_delay(attempt, retry_after), cancel_event)

    if isinstance(last_error, NetworkRequestError):
        raise last_error
    detail = last_error.__class__.__name__ if last_error else "未知错误"
    error = NetworkRequestError(f"{phase}网络请求失败（{detail}）")
    raise error from last_error


def resolve_share_url(
    session: requests.Session,
    url: str,
    cancel_event: threading.Event | None = None,
) -> str:
    """跟随短链跳转，返回真实落地地址。"""
    response = _get_with_retry(
        session,
        url,
        timeout=PAGE_TIMEOUT,
        cancel_event=cancel_event,
        phase="短链接",
        allow_redirects=True,
    )
    try:
        if response.status_code >= 400:
            raise WafBlockedError(f"短链跳转失败：HTTP {response.status_code}")
        return response.url
    finally:
        response.close()


def parse_id_kind(final_url: str) -> tuple[str, str]:
    """从真实地址中解析 (类型, 作品ID)；slides 按图文处理。"""
    match = ID_KIND_RE.search(final_url or "")
    if not match:
        raise InvalidLinkError("无法从链接中解析出作品 ID")
    kind_raw, aweme_id = match.groups()
    kind = "video" if kind_raw == "video" else "note"
    return kind, aweme_id


def redirected_item_id(current_url: str, target_aweme_id: str) -> str | None:
    """返回浏览器跳转后的其他作品 ID；非作品页或仍是目标作品时返回空。"""
    parsed = urlparse(current_url or "")
    candidates: list[str] = []
    match = ID_KIND_RE.search(parsed.path)
    if match:
        candidates.append(match.group(2))
    query = parse_qs(parsed.query)
    for key in ("modal_id", "aweme_id", "item_id"):
        candidates.extend(query.get(key) or [])
    target = str(target_aweme_id)
    return next(
        (candidate for candidate in candidates if candidate.isdigit() and candidate != target),
        None,
    )


def is_explicit_unavailable_redirect(current_url: str) -> bool:
    """识别抖音失效作品跳往精选页时携带的明确 404 来源标记。"""
    query = parse_qs(urlparse(current_url or "").query)
    return "web_video_404_link" in (query.get("previous_page") or [])


def find_router_data(html: str) -> Optional[dict]:
    """解析 HTML 中的 window._ROUTER_DATA，找不到时返回 None。"""
    match = ROUTER_RE.search(html or "")
    if not match:
        return None
    try:
        return json.loads(match.group(1).strip())
    except (TypeError, json.JSONDecodeError):
        return None


def is_captcha_page(response) -> bool:
    """识别抖音 HTTP 200 验证码中间页，避免将其误判为作品异常。"""
    try:
        body = response.content.decode(
            response.apparent_encoding or "utf-8", errors="replace"
        )
    except Exception:
        body = response.text or ""
    lowered = body.lower()
    return (
        "验证码中间页" in body
        or ("ttgcaptcha" in lowered and "verify_data" in lowered)
        or ("captchaoptions" in lowered and 'type":"verify"' in lowered)
    )


def _item_id(item: dict) -> str:
    return str(item.get("aweme_id") or item.get("aweme_id_str") or "").strip()


def _looks_like_full_item(item: dict, aweme_id: str) -> bool:
    """拒绝只含 aweme_id 的路由/埋点占位对象。"""
    if _item_id(item) != str(aweme_id):
        return False
    return any(key in item for key in ("video", "images", "statistics", "author"))


def find_item(data, aweme_id: str | None = None) -> Optional[dict]:
    """递归查找作品；提供作品 ID 时只返回完全匹配的条目。"""
    if isinstance(data, dict):
        if aweme_id is not None and _looks_like_full_item(data, aweme_id):
            return data
        item_list = data.get("item_list")
        if isinstance(item_list, list) and item_list:
            for item in item_list:
                if not isinstance(item, dict):
                    continue
                if aweme_id is None or _looks_like_full_item(item, aweme_id):
                    return item
        for value in data.values():
            found = find_item(value, aweme_id)
            if found is not None:
                return found
    elif isinstance(data, list):
        for value in data:
            found = find_item(value, aweme_id)
            if found is not None:
                return found
    return None


def _new_session() -> requests.Session:
    """创建带默认请求头的全新会话（模拟“重启程序”）。"""
    session = requests.Session()
    session.headers.update(BASE_HEADERS)
    return session


def fetch_item_with_session(
    session: requests.Session,
    aweme_id: str,
    kind: str,
    cancel_event: threading.Event | None = None,
) -> tuple[requests.Session, dict]:
    """最多请求两个正确作品页；验证码立即退出，禁止重试放大。"""
    douyin_path = "video" if kind == "video" else "note"
    attempts = [
        (UA_IPHONE, f"{SHARE_BASE}/{kind}/{aweme_id}/"),
        (UA_ANDROID, f"https://www.douyin.com/{douyin_path}/{aweme_id}"),
    ]

    last_network_error: Exception | None = None
    for index, (ua, url) in enumerate(attempts):
        ensure_not_cancelled(cancel_event)
        session.headers["User-Agent"] = ua
        try:
            response = _get_with_retry(
                session,
                url,
                timeout=PAGE_TIMEOUT,
                cancel_event=cancel_event,
                phase=f"作品页第 {index + 1} 次尝试",
            )
        except NetworkRequestError as exc:
            last_network_error = exc
            # _get_with_retry 已经完成同一地址的有限退避；这里直接切换
            # 第二个作品页地址，避免对同一个失败链路额外放大请求。
            continue

        last_network_error = None
        try:
            if is_captcha_page(response):
                logger.warning("第 %d 次尝试命中验证码中间页，立即停止 HTTP 重试", index + 1)
                raise CaptchaChallengeError("检测到抖音验证码中间页")
            if response.status_code in {403, 429}:
                raise CaptchaChallengeError(
                    f"作品页触发访问限制：HTTP {response.status_code}"
                )
            if response.status_code in {404, 410}:
                raise LoginRequiredError(
                    f"作品页暂不可用：HTTP {response.status_code}"
                )
            if response.status_code >= 400:
                raise WafBlockedError(f"作品页请求失败：HTTP {response.status_code}")
            data = find_router_data(response.text)
        finally:
            response.close()
        if data is not None:
            item = find_item(data, aweme_id)
            if item is not None:
                if index > 0:
                    logger.info("第 %d 次尝试成功恢复数据", index + 1)
                return session, item
            logger.warning(
                "第 %d 次尝试：页面数据存在但无目标作品，将尝试浏览器兜底",
                index + 1,
            )
        else:
            logger.info("第 %d 次尝试：未解析到作品数据", index + 1)

        if index < len(attempts) - 1:
            interruptible_wait(1.5 + random.random(), cancel_event)

    if last_network_error is not None:
        raise last_network_error
    raise PageStructureError("作品页可访问，但未返回匹配的结构化作品数据")


def fetch_item(session: requests.Session, aweme_id: str, kind: str) -> dict:
    """兼容旧调用；新流程使用 :func:`fetch_item_with_session`。"""
    _session, item = fetch_item_with_session(session, aweme_id, kind)
    return item


def extract_tags(item: dict, desc: str) -> str:
    """提取话题标签：优先 text_extra.hashtag_name，缺失时从标题正则提取。"""
    names: list[str] = []
    for entry in item.get("text_extra") or []:
        if isinstance(entry, dict):
            name = str(entry.get("hashtag_name") or "").strip()
            if name and name not in names:
                names.append(name)
    if names:
        return " ".join(f"#{name}" if not name.startswith("#") else name for name in names)

    found = re.findall(r"#\S+", desc or "")
    return " ".join(dict.fromkeys(found)) or "无"


def extract_author(item: dict) -> str:
    """提取博主昵称。"""
    author = item.get("author") or {}
    return str(author.get("nickname") or "").strip()


def extract_fields(item: dict, aweme_id: str) -> dict:
    """从作品数据中提取标题、标签、点赞、评论、博主和封面地址。"""
    desc = item.get("desc") or ""
    title = re.sub(r"\s+", " ", desc).strip() or "无"

    stats = item.get("statistics") or {}
    likes = int(stats.get("digg_count") or 0)
    comments = int(stats.get("comment_count") or 0)

    cover_url: Optional[str] = None
    video = item.get("video") or {}
    images = item.get("images") or []
    if images and isinstance(images[0], dict) and images[0].get("url_list"):
        cover_url = images[0]["url_list"][0]
    elif isinstance(video, dict):
        cover = video.get("cover") or {}
        if isinstance(cover, dict) and cover.get("url_list"):
            cover_url = cover["url_list"][0]

    return {
        "aweme_id": aweme_id,
        "title": title,
        "tags": extract_tags(item, desc),
        "likes": likes,
        "comments": comments,
        "author": extract_author(item),
        "cover_url": cover_url,
    }


def fetch_record(
    text: str,
    cancel_event: threading.Event | None = None,
) -> FetchedRecord:
    """解析一条输入并返回成功会话、稳定 ID、规范链接和作品字段。"""
    context = AccessContext(None, cancel_event)
    try:
        record = context.fetch_record(text)
        # 兼容旧调用：成功会话的所有权交给返回值，不能随上下文关闭。
        context.session = _new_session()
        return record
    finally:
        context.close()


def iter_play_urls(item: dict) -> list[str]:
    """生成无水印优先的视频地址候选：playwm→play 替换后的地址在前。"""
    video = item.get("video") or {}
    play_addr = video.get("play_addr") or {}
    candidates: list[str] = []
    for url in play_addr.get("url_list") or []:
        swapped = url.replace("/playwm/", "/play/")
        if swapped != url:
            candidates.append(swapped)
        if url not in candidates:
            candidates.append(url)
    return candidates


def _suffix_for(url: str, content_type: str) -> str:
    content_type = (content_type or "").lower()
    if "webp" in content_type:
        return ".webp"
    if "png" in content_type:
        return ".png"
    if "jpeg" in content_type or "jpg" in content_type:
        return ".jpg"
    url_suffix = Path(url.split("?", 1)[0]).suffix.lower()
    return url_suffix if url_suffix in {".webp", ".jpg", ".jpeg", ".png"} else ".jpg"


def _validate_media_response(response, expected: str) -> None:
    if response.status_code != 200:
        raise ResponseValidationError(f"HTTP {response.status_code}")
    content_type = (response.headers.get("content-type") or "").lower()
    if "text/html" in content_type or "application/json" in content_type:
        raise ResponseValidationError(f"服务器返回了 {content_type or '非媒体内容'}")
    if expected == "image" and content_type and not content_type.startswith("image/"):
        raise ResponseValidationError(f"图片响应类型异常：{content_type}")
    if expected == "video" and content_type and not (
        content_type.startswith("video/") or "octet-stream" in content_type
    ):
        raise ResponseValidationError(f"视频响应类型异常：{content_type}")


def _atomic_response_to_file(
    response,
    target: Path,
    *,
    expected: str,
    cancel_event: threading.Event | None = None,
    progress_cb: Optional[ProgressCallback] = None,
) -> int:
    _validate_media_response(response, expected)
    target.parent.mkdir(parents=True, exist_ok=True)
    part = target.with_name(f".{target.name}.{uuid.uuid4().hex}.part")
    total = int(response.headers.get("content-length") or 0)
    done = 0
    try:
        with part.open("wb") as file_obj:
            for chunk in response.iter_content(chunk_size=64 * 1024):
                ensure_not_cancelled(cancel_event)
                if not chunk:
                    continue
                file_obj.write(chunk)
                done += len(chunk)
                if progress_cb:
                    progress_cb(done, total)
            file_obj.flush()
            os.fsync(file_obj.fileno())
        if done <= 0:
            raise ResponseValidationError("下载内容为空")
        if total > 0 and done != total:
            raise ResponseValidationError(f"下载不完整：应为 {total} 字节，实际 {done} 字节")
        os.replace(part, target)
        return done
    finally:
        part.unlink(missing_ok=True)


class _BrowserResponseAdapter:
    """把浏览器页面内的流式 fetch 适配成下载器使用的最小 Response 接口。"""

    def __init__(
        self,
        page,
        stream_key: str,
        status_code: int,
        headers: dict,
        cancel_event: threading.Event | None,
        idle_timeout: float,
    ):
        self._page = page
        self._stream_key = stream_key
        self.status_code = int(status_code)
        self.headers = {
            str(key).lower(): str(value) for key, value in (headers or {}).items()
        }
        self._cancel_event = cancel_event
        self._idle_timeout = max(1.0, float(idle_timeout))
        self._closed = False

    def iter_content(self, chunk_size: int = 64 * 1024):
        del chunk_size  # 浏览器 ReadableStream 自行决定网络块大小。
        last_activity = time.monotonic()
        last_loaded = 0
        while True:
            try:
                ensure_not_cancelled(self._cancel_event)
                state = self._page.evaluate(
                    """(key) => {
                        const streams = window.__douyinMediaStreams || {};
                        const state = streams[key];
                        if (!state) {
                            return {missing: true, chunks: [], done: true, error: ""};
                        }
                        return {
                            missing: false,
                            chunks: state.queue.splice(0, state.queue.length),
                            done: Boolean(state.done),
                            error: state.error || "",
                            loaded: Number(state.loaded || 0),
                        };
                    }""",
                    self._stream_key,
                )
            except TaskCancelled:
                self.close()
                raise
            except Exception as exc:
                raise NetworkRequestError(f"浏览器媒体响应读取失败（{exc}）") from exc

            if state.get("missing"):
                raise NetworkRequestError("浏览器媒体读取状态已丢失")
            error = str(state.get("error") or "").strip()
            if error:
                raise NetworkRequestError(f"浏览器媒体响应读取失败（{error}）")

            chunks = state.get("chunks") or []
            loaded = int(state.get("loaded") or 0)
            if chunks or loaded > last_loaded:
                last_activity = time.monotonic()
                last_loaded = loaded
            for encoded in chunks:
                try:
                    chunk = base64.b64decode(str(encoded), validate=True)
                except Exception as exc:
                    raise ResponseValidationError("浏览器媒体数据块损坏") from exc
                if chunk:
                    yield chunk

            if state.get("done"):
                return
            if time.monotonic() - last_activity >= self._idle_timeout:
                self.close()
                raise NetworkRequestError(
                    f"浏览器媒体读取超过 {int(self._idle_timeout)} 秒无进度"
                )
            interruptible_wait(0.1, self._cancel_event)

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        try:
            self._page.evaluate(
                """(key) => {
                    const streams = window.__douyinMediaStreams || {};
                    const state = streams[key];
                    if (state && state.controller && !state.done) {
                        state.cancelled = true;
                        state.controller.abort();
                    }
                    delete streams[key];
                }""",
                self._stream_key,
            )
        except Exception:
            logger.debug("释放浏览器媒体流失败", exc_info=True)


class _BrowserBufferedResponseAdapter:
    """兼容页面 fetch 不可用时的 Playwright APIResponse。"""

    def __init__(self, response):
        self._response = response
        self.status_code = int(response.status)
        self.headers = {
            str(key).lower(): str(value) for key, value in response.headers.items()
        }
        self._body: bytes | None = None

    def iter_content(self, chunk_size: int = 64 * 1024):
        if self._body is None:
            try:
                self._body = bytes(self._response.body())
            except Exception as exc:
                raise NetworkRequestError(f"浏览器媒体响应读取失败（{exc}）") from exc
        size = max(1, int(chunk_size))
        for offset in range(0, len(self._body), size):
            yield self._body[offset : offset + size]

    def close(self) -> None:
        dispose = getattr(self._response, "dispose", None)
        if callable(dispose):
            try:
                dispose()
            except Exception:
                logger.debug("释放浏览器媒体响应失败", exc_info=True)


def _playwright_timeout_ms(timeout) -> int:
    if isinstance(timeout, tuple):
        return int(sum(float(value) for value in timeout) * 1000)
    return int(float(timeout) * 1000)


def _browser_timeout_parts(timeout) -> tuple[float, float]:
    if isinstance(timeout, tuple):
        connect_timeout = float(timeout[0])
        idle_timeout = float(timeout[-1])
    else:
        connect_timeout = min(30.0, float(timeout))
        idle_timeout = float(timeout)
    return max(1.0, connect_timeout), max(1.0, idle_timeout)


def _browser_page(browser_context):
    pages = getattr(browser_context, "pages", None)
    if not isinstance(pages, (list, tuple)):
        return None
    for page in pages:
        try:
            if not page.is_closed():
                return page
        except Exception:
            continue
    return None


def _start_browser_media_stream(
    browser_context,
    url: str,
    timeout,
    cancel_event: threading.Event | None,
):
    """在真实浏览器页面中流式 fetch，避免 APIRequest 一次性缓冲整段视频。"""
    page = _browser_page(browser_context)
    if page is None:
        raise NetworkRequestError("浏览器页面不可用")

    stream_key = uuid.uuid4().hex
    try:
        page.evaluate(
            """({url, key}) => {
                const streams = window.__douyinMediaStreams =
                    window.__douyinMediaStreams || {};
                const controller = new AbortController();
                const state = streams[key] = {
                    controller,
                    cancelled: false,
                    ready: false,
                    done: false,
                    error: "",
                    status: 0,
                    headers: {},
                    queue: [],
                    loaded: 0,
                };
                const encode = (bytes) => {
                    let binary = "";
                    const step = 0x8000;
                    for (let offset = 0; offset < bytes.length; offset += step) {
                        binary += String.fromCharCode.apply(
                            null,
                            bytes.subarray(offset, offset + step)
                        );
                    }
                    return btoa(binary);
                };
                (async () => {
                    try {
                        const response = await fetch(url, {
                            credentials: "include",
                            cache: "no-store",
                            signal: controller.signal,
                        });
                        state.status = response.status;
                        response.headers.forEach((value, name) => {
                            state.headers[String(name).toLowerCase()] = String(value);
                        });
                        state.ready = true;
                        if (!response.body) {
                            throw new Error("响应没有可读取的媒体数据流");
                        }
                        const reader = response.body.getReader();
                        while (true) {
                            while (state.queue.length >= 4 && !state.cancelled) {
                                await new Promise((resolve) => setTimeout(resolve, 25));
                            }
                            if (state.cancelled) {
                                await reader.cancel();
                                break;
                            }
                            const result = await reader.read();
                            if (result.done) break;
                            if (result.value && result.value.byteLength) {
                                state.queue.push(encode(result.value));
                                state.loaded += result.value.byteLength;
                            }
                        }
                    } catch (error) {
                        if (!state.cancelled) {
                            state.error = String(
                                error && (error.stack || error.message) || error
                            );
                        }
                    } finally {
                        state.done = true;
                    }
                })();
                return true;
            }""",
            {"url": url, "key": stream_key},
        )
    except Exception as exc:
        raise NetworkRequestError(f"浏览器媒体流启动失败（{exc}）") from exc

    connect_timeout, idle_timeout = _browser_timeout_parts(timeout)
    deadline = time.monotonic() + connect_timeout
    try:
        while time.monotonic() < deadline:
            ensure_not_cancelled(cancel_event)
            state = page.evaluate(
                """(key) => {
                    const state = (window.__douyinMediaStreams || {})[key];
                    if (!state) return {missing: true};
                    return {
                        missing: false,
                        ready: Boolean(state.ready),
                        done: Boolean(state.done),
                        error: state.error || "",
                        status: Number(state.status || 0),
                        headers: state.headers || {},
                    };
                }""",
                stream_key,
            )
            if state.get("missing"):
                raise NetworkRequestError("浏览器媒体连接状态已丢失")
            error = str(state.get("error") or "").strip()
            if error:
                raise NetworkRequestError(f"浏览器媒体连接失败（{error}）")
            if state.get("ready"):
                return _BrowserResponseAdapter(
                    page,
                    stream_key,
                    int(state.get("status") or 0),
                    state.get("headers") or {},
                    cancel_event,
                    idle_timeout,
                )
            if state.get("done"):
                raise NetworkRequestError("浏览器媒体连接未返回响应头")
            interruptible_wait(0.1, cancel_event)
        raise NetworkRequestError(
            f"浏览器媒体连接超过 {int(connect_timeout)} 秒未响应"
        )
    except Exception:
        try:
            page.evaluate(
                """(key) => {
                    const streams = window.__douyinMediaStreams || {};
                    const state = streams[key];
                    if (state && state.controller) state.controller.abort();
                    delete streams[key];
                }""",
                stream_key,
            )
        except Exception:
            pass
        raise


def _browser_get_response(
    browser_context,
    url: str,
    headers: dict,
    timeout,
    cancel_event: threading.Event | None = None,
):
    """通过专用浏览器流式下载媒体；页面不支持时使用限时兼容请求。"""
    try:
        return _start_browser_media_stream(
            browser_context, url, timeout, cancel_event
        )
    except TaskCancelled:
        raise
    except NetworkRequestError as stream_exc:
        logger.warning("浏览器页面流式下载不可用，改用限时兼容请求：%s", stream_exc)

    try:
        request_context = browser_context.request
        response = request_context.get(
            url,
            headers=headers,
            timeout=min(30_000, _playwright_timeout_ms(timeout)),
            fail_on_status_code=False,
        )
        ensure_not_cancelled(cancel_event)
        return _BrowserBufferedResponseAdapter(response)
    except NetworkRequestError:
        raise
    except TaskCancelled:
        raise
    except Exception as exc:
        raise NetworkRequestError(f"浏览器媒体网络请求失败（{exc}）") from exc


def _open_media_response(
    session: requests.Session,
    url: str,
    *,
    headers: dict,
    timeout,
    cancel_event: threading.Event | None,
    phase: str,
    browser_context=None,
    browser_context_provider: Callable[[], object | None] | None = None,
    prefer_browser: bool = False,
):
    """优先走 Requests；其网络链路失败时切换到已打开的浏览器网络。"""
    if prefer_browser and browser_context is None and browser_context_provider is not None:
        browser_context = browser_context_provider()
    if prefer_browser and browser_context is not None:
        return _browser_get_response(
            browser_context, url, headers, timeout, cancel_event
        )
    try:
        return _get_with_retry(
            session,
            url,
            timeout=timeout,
            cancel_event=cancel_event,
            phase=phase,
            attempts=1,
            stream=True,
            headers=headers,
        )
    except NetworkRequestError as exc:
        if browser_context is None and browser_context_provider is not None:
            browser_context = browser_context_provider()
        if browser_context is None:
            raise
        logger.warning("%s Requests 链路失败，切换浏览器网络：%s", phase, exc)
        return _browser_get_response(
            browser_context, url, headers, timeout, cancel_event
        )


def _is_retryable_media_error(exc: Exception) -> bool:
    """只重试连接/读取中断、临时 HTTP 或不完整响应。"""
    if isinstance(exc, (NetworkRequestError, requests.RequestException)):
        return True
    if not isinstance(exc, ResponseValidationError):
        return False
    message = str(exc)
    status_match = re.match(r"HTTP\s+(\d+)$", message)
    if status_match:
        return int(status_match.group(1)) in TRANSIENT_STATUS_CODES
    return message.startswith(("下载内容为空", "下载不完整"))


def download_cover(
    session: requests.Session,
    url: str,
    out_dir: Path,
    name: str,
    cancel_event: threading.Event | None = None,
    *,
    browser_context=None,
    browser_context_provider: Callable[[], object | None] | None = None,
) -> Path:
    """下载封面图到 out_dir，以 name 作为文件名，扩展名跟随响应 content-type。"""
    ensure_not_cancelled(cancel_event)
    headers = {
        "User-Agent": session.headers.get("User-Agent", UA_IPHONE),
        "Referer": REFERER,
    }
    last_error: Exception | None = None
    prefer_browser = False
    for attempt in range(1, MEDIA_RETRY_ATTEMPTS + 1):
        response = None
        try:
            response = _open_media_response(
                session,
                url,
                headers=headers,
                timeout=MEDIA_TIMEOUT,
                cancel_event=cancel_event,
                phase="封面",
                browser_context=browser_context,
                browser_context_provider=browser_context_provider,
                prefer_browser=prefer_browser,
            )
            suffix = _suffix_for(url, response.headers.get("content-type", ""))
            target = out_dir / f"{name}{suffix}"
            _atomic_response_to_file(
                response, target, expected="image", cancel_event=cancel_event
            )
            return target
        except TaskCancelled:
            raise
        except (NetworkRequestError, requests.RequestException, ResponseValidationError) as exc:
            last_error = exc
            if not _is_retryable_media_error(exc):
                raise
            if browser_context is not None:
                prefer_browser = True
            if attempt < MEDIA_RETRY_ATTEMPTS:
                logger.info("封面第 %d/%d 次失败（%s），等待后重试", attempt, MEDIA_RETRY_ATTEMPTS, exc)
                interruptible_wait(_retry_delay(attempt), cancel_event)
        finally:
            if response is not None:
                response.close()
    raise ExtractionError(f"封面下载失败（{last_error or '未知错误'}）") from last_error


def download_video(
    session: requests.Session,
    item: dict,
    target: Path,
    progress_cb: Optional[ProgressCallback] = None,
    size_check: Optional[Callable[[int], Optional[str]]] = None,
    cancel_event: threading.Event | None = None,
    *,
    browser_context=None,
    browser_context_provider: Callable[[], object | None] | None = None,
) -> tuple[Path, Optional[str]]:
    """流式下载无水印视频到 target，逐块回调 (已下载字节, 总字节)。

    size_check 在拿到 content-length 后调用；若返回非空字符串（表示文件夹里
    已有字节数完全相同的文件），则跳过下载并返回 (target, 命中提示)。
    正常下载成功返回 (target, None)。
    """
    urls = iter_play_urls(item)
    if not urls:
        raise ExtractionError("该作品没有可用的视频下载地址")

    target.parent.mkdir(parents=True, exist_ok=True)
    last_error = "未知错误"
    for url in urls:
        ensure_not_cancelled(cancel_event)
        headers = {
            "User-Agent": session.headers.get("User-Agent", UA_IPHONE),
            "Referer": REFERER,
        }
        prefer_browser = False
        for attempt in range(1, MEDIA_RETRY_ATTEMPTS + 1):
            response = None
            try:
                response = _open_media_response(
                    session,
                    url,
                    headers=headers,
                    timeout=MEDIA_TIMEOUT,
                    cancel_event=cancel_event,
                    phase="视频",
                    browser_context=browser_context,
                    browser_context_provider=browser_context_provider,
                    prefer_browser=prefer_browser,
                )
                _validate_media_response(response, "video")

                total = int(response.headers.get("content-length") or 0)
                if size_check is not None:
                    hit = size_check(total)
                    if hit:
                        logger.warning("视频大小与已有文件 %s 相同，跳过下载", hit)
                        return target, hit

                _atomic_response_to_file(
                    response,
                    target,
                    expected="video",
                    cancel_event=cancel_event,
                    progress_cb=progress_cb,
                )
                return target, None
            except TaskCancelled:
                raise
            except (NetworkRequestError, requests.RequestException, ResponseValidationError) as exc:
                last_error = str(exc) or exc.__class__.__name__
                if not _is_retryable_media_error(exc):
                    break
                if browser_context is not None:
                    prefer_browser = True
                if attempt < MEDIA_RETRY_ATTEMPTS:
                    logger.info(
                        "视频地址第 %d/%d 次失败（%s），等待后重试",
                        attempt,
                        MEDIA_RETRY_ATTEMPTS,
                        exc,
                    )
                    interruptible_wait(_retry_delay(attempt), cancel_event)
            finally:
                if response is not None:
                    response.close()

    raise ExtractionError(f"视频下载失败（{last_error}）")


def download_images(
    session: requests.Session,
    item: dict,
    target_dir: Path,
    progress_cb: Optional[ProgressCallback] = None,
    cancel_event: threading.Event | None = None,
    *,
    browser_context=None,
    browser_context_provider: Callable[[], object | None] | None = None,
) -> list[Path]:
    """下载图文作品的原图（无水印）到 target_dir，返回保存路径列表。"""
    images = item.get("images") or []
    if not images:
        raise ExtractionError("该图文作品没有可下载的图片")

    target_dir.mkdir(parents=True, exist_ok=True)
    saved: list[Path] = []
    for index, image in enumerate(images, 1):
        ensure_not_cancelled(cancel_event)
        if not isinstance(image, dict) or not image.get("url_list"):
            raise ExtractionError(f"图集第 {index} 张没有可用下载地址")
        url = image["url_list"][0]
        headers = {
            "User-Agent": session.headers.get("User-Agent", UA_IPHONE),
            "Referer": REFERER,
        }
        last_error: Exception | None = None
        prefer_browser = False
        for attempt in range(1, MEDIA_RETRY_ATTEMPTS + 1):
            response = None
            try:
                response = _open_media_response(
                    session,
                    url,
                    headers=headers,
                    timeout=MEDIA_TIMEOUT,
                    cancel_event=cancel_event,
                    phase=f"图集第 {index} 张",
                    browser_context=browser_context,
                    browser_context_provider=browser_context_provider,
                    prefer_browser=prefer_browser,
                )
                suffix = _suffix_for(url, response.headers.get("content-type", ""))
                path = target_dir / f"{index}{suffix}"
                _atomic_response_to_file(
                    response, path, expected="image", cancel_event=cancel_event
                )
                saved.append(path)
                if progress_cb:
                    progress_cb(index, len(images))
                break
            except TaskCancelled:
                raise
            except (NetworkRequestError, requests.RequestException, ResponseValidationError) as exc:
                last_error = exc
                if not _is_retryable_media_error(exc):
                    raise
                if browser_context is not None:
                    prefer_browser = True
                if attempt < MEDIA_RETRY_ATTEMPTS:
                    logger.info(
                        "图集第 %d 张第 %d/%d 次失败（%s），等待后重试",
                        index,
                        attempt,
                        MEDIA_RETRY_ATTEMPTS,
                        exc,
                    )
                    interruptible_wait(_retry_delay(attempt), cancel_event)
            finally:
                if response is not None:
                    response.close()
        else:
            raise ExtractionError(
                f"图集第 {index} 张下载失败（{last_error or '未知错误'}）"
            ) from last_error

    if len(saved) != len(images):
        raise ExtractionError(f"图集下载不完整：应为 {len(images)} 张，实际 {len(saved)} 张")
    return saved
