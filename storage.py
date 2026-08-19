# -*- coding: utf-8 -*-
"""提取产物的暂存、提交和失败回滚。"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
import os
from pathlib import Path
import shutil
import uuid
from typing import Callable


class TransactionError(RuntimeError):
    def __init__(self, message: str, rolled_back: bool = False):
        super().__init__(message)
        self.rolled_back = rolled_back


@dataclass(slots=True)
class CommitResult:
    media_path: Path
    cover_path: Path | None
    backup_dir: Path | None


class ArtifactTransaction:
    """单条作品的文件事务。

    下载先进入输出目录同盘的 ``.staging``。提交时把旧产物移到备份，
    提升新产物，再执行工作簿回调；回调失败则恢复旧产物。
    """

    def __init__(self, output_dir: Path, seq: int):
        self.output_dir = Path(output_dir)
        self.seq = int(seq)
        self.stage_root = (
            self.output_dir / ".staging" / f"{self.seq}_{uuid.uuid4().hex}"
        )
        self.stage_media_root = self.stage_root / "media"
        self.stage_cover_root = self.stage_root / "cover"
        self.stage_media_root.mkdir(parents=True, exist_ok=True)
        self.stage_cover_root.mkdir(parents=True, exist_ok=True)

    def video_target(self) -> Path:
        return self.stage_media_root / f"{self.seq}.mp4"

    def note_target(self) -> Path:
        return self.stage_media_root / str(self.seq)

    def cover_dir(self) -> Path:
        return self.stage_cover_root

    def cleanup(self) -> None:
        shutil.rmtree(self.stage_root, ignore_errors=True)
        staging_root = self.output_dir / ".staging"
        try:
            staging_root.rmdir()
        except OSError:
            pass

    def commit(
        self,
        *,
        work_kind: str,
        staged_media: Path,
        staged_cover: Path | None,
        persist_workbook: Callable[[Path | None], None],
    ) -> CommitResult:
        videos_dir = self.output_dir / "爆款视频"
        covers_dir = self.output_dir / "封面"
        captions_dir = self.output_dir / "文案提取"
        videos_dir.mkdir(parents=True, exist_ok=True)
        covers_dir.mkdir(parents=True, exist_ok=True)
        captions_dir.mkdir(parents=True, exist_ok=True)

        final_video = videos_dir / f"{self.seq}.mp4"
        final_note = videos_dir / str(self.seq)
        final_media = final_note if work_kind == "note" else final_video
        caption = captions_dir / f"{self.seq}.txt"

        timestamp = datetime.now().strftime("%Y-%m-%d_%H%M%S_%f")
        backup_dir = self.output_dir / "替换备份" / f"{timestamp}_{self.seq}"
        moved: list[tuple[Path, Path]] = []
        promoted: list[Path] = []
        caption_created = False

        def backup(path: Path, bucket: str) -> None:
            if not path.exists():
                return
            target_dir = backup_dir / bucket
            target_dir.mkdir(parents=True, exist_ok=True)
            target = target_dir / path.name
            shutil.move(str(path), str(target))
            moved.append((target, path))

        try:
            # 两种媒体都备份，避免视频/图文互换后残留旧类型。
            backup(final_video, "媒体")
            backup(final_note, "媒体")
            for old_cover in sorted(covers_dir.glob(f"{self.seq}.*")):
                backup(old_cover, "封面")
            if work_kind == "note":
                backup(caption, "文案")

            if work_kind == "note":
                shutil.move(str(staged_media), str(final_note))
            else:
                os.replace(staged_media, final_video)
            promoted.append(final_media)

            final_cover: Path | None = None
            if staged_cover is not None:
                final_cover = covers_dir / staged_cover.name
                os.replace(staged_cover, final_cover)
                promoted.append(final_cover)

            if work_kind == "video" and not caption.exists():
                tmp_caption = captions_dir / f".{self.seq}.{uuid.uuid4().hex}.tmp"
                tmp_caption.write_text("", encoding="utf-8")
                os.replace(tmp_caption, caption)
                caption_created = True

            persist_workbook(final_cover)
        except Exception as exc:
            rollback_ok = True
            for path in reversed(promoted):
                try:
                    if path.is_dir():
                        shutil.rmtree(path)
                    else:
                        path.unlink(missing_ok=True)
                except OSError:
                    rollback_ok = False
            if caption_created:
                try:
                    caption.unlink(missing_ok=True)
                except OSError:
                    rollback_ok = False
            for backup_path, original_path in reversed(moved):
                try:
                    original_path.parent.mkdir(parents=True, exist_ok=True)
                    shutil.move(str(backup_path), str(original_path))
                except OSError:
                    rollback_ok = False
            self.cleanup()
            raise TransactionError(
                f"提交失败，{'已恢复旧文件' if rollback_ok else '自动恢复不完整，请检查替换备份'}：{exc}",
                rolled_back=rollback_ok,
            ) from exc

        self.cleanup()
        if backup_dir.exists() and not any(backup_dir.rglob("*")):
            shutil.rmtree(backup_dir, ignore_errors=True)
            kept_backup: Path | None = None
        else:
            kept_backup = backup_dir if backup_dir.exists() else None
        return CommitResult(final_media, final_cover, kept_backup)
