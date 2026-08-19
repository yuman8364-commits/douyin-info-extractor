# -*- coding: utf-8 -*-
"""抖音作品信息提取引擎（纯 HTTP，无需浏览器或插件）。

数据来源：用移动端 User-Agent 请求 iesdouyin.com 分享页，
解析页面中的 window._ROUTER_DATA，提取标题、标签、点赞数、评论数、
博主、封面，以及无水印视频地址和图集原图。
"""

from __future__ import annotations

import json
import logging
import os
import random
import re
from dataclasses import dataclass
from pathlib import Path
import threading
import uuid
from typing import Callable, Optional

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


class WafBlockedError(ExtractionError):
    pass


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


def resolve_share_url(
    session: requests.Session,
    url: str,
    cancel_event: threading.Event | None = None,
) -> str:
    """跟随短链跳转，返回真实落地地址。"""
    last_error: requests.RequestException | None = None
    for attempt in range(2):
        ensure_not_cancelled(cancel_event)
        try:
            response = session.get(url, timeout=20, allow_redirects=True)
            try:
                if response.status_code >= 400:
                    raise WafBlockedError(f"短链跳转失败：HTTP {response.status_code}")
                return response.url
            finally:
                response.close()
        except requests.RequestException as exc:
            last_error = exc
            if attempt == 0:
                interruptible_wait(1.0 + random.random(), cancel_event)
    raise WafBlockedError(
        f"网络请求失败：{last_error.__class__.__name__ if last_error else '未知错误'}，请检查网络后重试"
    ) from last_error


def parse_id_kind(final_url: str) -> tuple[str, str]:
    """从真实地址中解析 (类型, 作品ID)；slides 按图文处理。"""
    match = ID_KIND_RE.search(final_url or "")
    if not match:
        raise InvalidLinkError("无法从链接中解析出作品 ID")
    kind_raw, aweme_id = match.groups()
    kind = "video" if kind_raw == "video" else "note"
    return kind, aweme_id


def find_router_data(html: str) -> Optional[dict]:
    """解析 HTML 中的 window._ROUTER_DATA，找不到时返回 None。"""
    match = ROUTER_RE.search(html or "")
    if not match:
        return None
    try:
        return json.loads(match.group(1).strip())
    except (TypeError, json.JSONDecodeError):
        return None


def _item_id(item: dict) -> str:
    return str(item.get("aweme_id") or item.get("aweme_id_str") or "").strip()


def find_item(data, aweme_id: str | None = None) -> Optional[dict]:
    """递归查找作品；提供作品 ID 时只返回完全匹配的条目。"""
    if isinstance(data, dict):
        item_list = data.get("item_list")
        if isinstance(item_list, list) and item_list:
            for item in item_list:
                if not isinstance(item, dict):
                    continue
                if aweme_id is None or _item_id(item) == str(aweme_id):
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
    """请求分享页并提取作品数据，带 UA 轮换、新会话重试和备用地址回退。

    提示「需要登录/已删除/受限制」大多是抖音限流的误报：换全新会话并延时
    重试通常即可恢复（用户反馈重启程序能解决，本质就是新会话 + 延时）。
    """
    other_kind = "note" if kind == "video" else "video"
    douyin_path = "video" if kind == "video" else "note"
    attempts = [
        (UA_IPHONE, f"{SHARE_BASE}/{kind}/{aweme_id}/"),
        (UA_ANDROID, f"{SHARE_BASE}/{kind}/{aweme_id}/"),
        (UA_IPHONE, f"https://www.douyin.com/{douyin_path}/{aweme_id}"),
        (UA_IPHONE, f"{SHARE_BASE}/{other_kind}/{aweme_id}/"),
    ]

    saw_router_data = False
    for index, (ua, url) in enumerate(attempts):
        ensure_not_cancelled(cancel_event)
        if index > 0:
            # 换全新会话再试，避免旧会话被风控关联
            session = _new_session()
        session.headers["User-Agent"] = ua
        try:
            response = session.get(url, timeout=20)
        except requests.RequestException as exc:
            logger.info(
                "第 %d 次请求失败（%s），将重试", index + 1, exc.__class__.__name__
            )
            if index < len(attempts) - 1:
                interruptible_wait(1.5 + random.random(), cancel_event)
            continue

        try:
            if response.status_code >= 400:
                logger.info("第 %d 次尝试：HTTP %d，将重试", index + 1, response.status_code)
                data = None
            else:
                data = find_router_data(response.text)
        finally:
            response.close()
        if data is not None:
            saw_router_data = True
            item = find_item(data, aweme_id)
            if item is not None:
                if index > 0:
                    logger.info("第 %d 次尝试成功恢复数据", index + 1)
                return session, item
            logger.warning(
                "第 %d 次尝试：页面数据存在但无作品数据（多为限流误报），将重试",
                index + 1,
            )
        else:
            logger.info("第 %d 次尝试：未解析到 _ROUTER_DATA，将重试", index + 1)

        if index < len(attempts) - 1:
            interruptible_wait(2 + random.random() * 2, cancel_event)

    if saw_router_data:
        raise LoginRequiredError("页面未返回目标作品，可能需要登录、已受限或页面结构已变化")
    raise WafBlockedError("被抖音风控拦截，请稍后重试")


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
    url = extract_url(text)
    session = _new_session()

    final_url = resolve_share_url(session, url, cancel_event)
    kind, aweme_id = parse_id_kind(final_url)
    session, item = fetch_item_with_session(session, aweme_id, kind, cancel_event)
    if _item_id(item) and _item_id(item) != aweme_id:
        raise ExtractionError("页面返回了其他作品的数据，已拒绝保存")
    fields = extract_fields(item, aweme_id)
    canonical_kind = "video" if kind == "video" else "note"
    canonical_url = f"https://www.douyin.com/{canonical_kind}/{aweme_id}"
    return FetchedRecord(session, kind, aweme_id, canonical_url, item, fields)


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


def download_cover(
    session: requests.Session,
    url: str,
    out_dir: Path,
    name: str,
    cancel_event: threading.Event | None = None,
) -> Path:
    """下载封面图到 out_dir，以 name 作为文件名，扩展名跟随响应 content-type。"""
    ensure_not_cancelled(cancel_event)
    response = session.get(
        url,
        stream=True,
        headers={
            "User-Agent": session.headers.get("User-Agent", UA_IPHONE),
            "Referer": REFERER,
        },
        timeout=(15, 60),
    )
    try:
        suffix = _suffix_for(url, response.headers.get("content-type", ""))
        target = out_dir / f"{name}{suffix}"
        _atomic_response_to_file(
            response, target, expected="image", cancel_event=cancel_event
        )
        return target
    finally:
        response.close()


def download_video(
    session: requests.Session,
    item: dict,
    target: Path,
    progress_cb: Optional[ProgressCallback] = None,
    size_check: Optional[Callable[[int], Optional[str]]] = None,
    cancel_event: threading.Event | None = None,
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
        response = None
        try:
            response = session.get(
                url,
                stream=True,
                timeout=(15, 60),
                headers={
                    "User-Agent": session.headers.get("User-Agent", UA_IPHONE),
                    "Referer": REFERER,
                },
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
        except (requests.RequestException, ExtractionError) as exc:
            last_error = str(exc) or exc.__class__.__name__
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
        response = session.get(
            url,
            stream=True,
            timeout=60,
            headers={
                "User-Agent": session.headers.get("User-Agent", UA_IPHONE),
                "Referer": REFERER,
            },
        )
        try:
            suffix = _suffix_for(url, response.headers.get("content-type", ""))
            path = target_dir / f"{index}{suffix}"
            _atomic_response_to_file(
                response, path, expected="image", cancel_event=cancel_event
            )
            saved.append(path)
            if progress_cb:
                progress_cb(index, len(images))
        finally:
            response.close()

    if len(saved) != len(images):
        raise ExtractionError(f"图集下载不完整：应为 {len(images)} 张，实际 {len(saved)} 张")
    return saved
