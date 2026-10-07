# -*- coding: utf-8 -*-
"""视频版本机下载记录和旧版运行日志迁移。"""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path
import re

import extractor
import input_parser


LOG_START_RE = re.compile(r"开始处理顺序\s+(\d+)：(.+)$")
LOG_SAVED_RE = re.compile(r"顺序\s+(\d+)\s+视频已保存：(.+)$")
NUMBERED_COPY_RE = re.compile(r"_\d+$")


class VideoHistory:
    """以作品 ID/标准化链接指向本地 MP4，避免重复联网下载。"""

    def __init__(self, state_dir: Path, output_dir: Path, logger: logging.Logger):
        self.path = Path(state_dir) / "video_history.json"
        self.output_dir = Path(output_dir)
        self.logger = logger
        self._items: dict[str, dict[str, str]] = {}
        self._load()
        self._migrate_run_logs()

    def _load(self) -> None:
        try:
            data = json.loads(self.path.read_text(encoding="utf-8-sig"))
        except FileNotFoundError:
            return
        except (OSError, ValueError):
            self.logger.exception("读取视频去重记录失败；将从旧运行日志恢复可用记录")
            return
        items = data.get("items") if isinstance(data, dict) else None
        if not isinstance(items, dict):
            return
        for identity, entry in items.items():
            if isinstance(identity, str) and isinstance(entry, dict) and entry.get("path"):
                self._items[identity] = {"path": str(entry["path"])}

    @staticmethod
    def _identity_keys(urls: list[str]) -> list[str]:
        identities: list[str] = []
        for url in urls:
            identity = input_parser.link_identity(url)
            if identity and identity not in identities:
                identities.append(identity)
        return identities

    def _migrate_run_logs(self) -> None:
        """将旧版日志中已保存且仍存在的 MP4 回填到去重记录。"""
        log_files = sorted(
            self.path.parent.glob("运行日志.log*"),
            key=lambda item: item.stat().st_mtime if item.exists() else 0,
        )
        candidates: dict[str, list[tuple[Path, int]]] = {}
        event_order = 0
        for log_path in log_files:
            try:
                lines = log_path.read_text(encoding="utf-8-sig", errors="replace").splitlines()
            except OSError:
                self.logger.warning("读取旧运行日志失败：%s", log_path, exc_info=True)
                continue
            pending: dict[int, list[str]] = {}
            for line in lines:
                started = LOG_START_RE.search(line)
                if started:
                    seq = int(started.group(1))
                    pending[seq] = extractor.extract_urls(started.group(2))
                    continue
                saved = LOG_SAVED_RE.search(line)
                if not saved:
                    continue
                seq = int(saved.group(1))
                urls = pending.pop(seq, [])
                video_path = Path(saved.group(2).strip().strip('"'))
                if not urls or video_path.suffix.lower() != ".mp4" or not video_path.is_file():
                    continue
                event_order += 1
                for identity in self._identity_keys(urls):
                    candidates.setdefault(identity, []).append((video_path, event_order))

        changed = False
        for identity, paths in candidates.items():
            current = self._existing_path(self._items.get(identity, {}).get("path"))
            if current is not None:
                continue
            best_path, _order = min(paths, key=self._candidate_rank)
            self._items[identity] = {"path": str(best_path.resolve())}
            changed = True
        if changed:
            self.logger.info("已从旧运行日志恢复 %d 个可用视频链接记录", sum(1 for key in candidates if self._existing_path(self._items.get(key, {}).get("path"))))
            self._save()

    def _candidate_rank(self, item: tuple[Path, int]) -> tuple[int, int, int]:
        path, order = item
        try:
            same_output = path.resolve().parent == self.output_dir.resolve()
        except OSError:
            same_output = False
        is_numbered_copy = bool(NUMBERED_COPY_RE.search(path.stem))
        return (0 if same_output else 1, 1 if is_numbered_copy else 0, order)

    @staticmethod
    def _existing_path(value: str | None) -> Path | None:
        if not value:
            return None
        try:
            path = Path(value)
            if path.is_file() and path.suffix.lower() == ".mp4":
                return path
        except OSError:
            return None
        return None

    def find(self, identities: list[str]) -> Path | None:
        for identity in identities:
            entry = self._items.get(identity)
            path = self._existing_path(entry.get("path") if entry else None)
            if path is not None:
                return path
        return None

    def record(self, identities: list[str], path: Path) -> bool:
        if not path.is_file():
            return False
        absolute_path = str(path.resolve())
        for identity in identities:
            if identity:
                self._items[identity] = {"path": absolute_path}
        return self._save()

    def _save(self) -> bool:
        payload = {
            "version": 1,
            "items": self._items,
        }
        temp_path = self.path.with_name(self.path.name + ".tmp")
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            temp_path.write_text(
                json.dumps(payload, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            os.replace(temp_path, self.path)
            return True
        except OSError:
            self.logger.exception("写入视频去重记录失败")
            try:
                temp_path.unlink(missing_ok=True)
            except OSError:
                pass
            return False


def identities_for_input(raw_input: str) -> list[str]:
    """返回输入内容中每个作品链接的稳定标识，不保存原始分享文本。"""
    return VideoHistory._identity_keys(extractor.extract_urls(raw_input))


def aweme_identity(aweme_id: str) -> str:
    return f"aweme:{aweme_id}"
