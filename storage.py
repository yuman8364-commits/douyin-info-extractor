# -*- coding: utf-8 -*-
"""提取产物的暂存、提交和失败回滚。"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
import os
from pathlib import Path
import re
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


@dataclass(slots=True)
class DeletionResult:
    deleted_artifacts: int
    shifted_artifacts: int
    backup_dir: Path | None


class ArtifactTransaction:
    """单条作品的文件事务。

    下载先进入输出目录同盘的 ``.staging``。提交时把旧产物移到备份，
    提升新产物，再执行工作簿回调；回调失败则恢复旧产物。
    """

    def __init__(self, output_dir: Path, seq: int, *, keep_backup: bool = True):
        self.output_dir = Path(output_dir)
        self.seq = int(seq)
        self.keep_backup = bool(keep_backup)
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

        def discard_backup_dir() -> None:
            shutil.rmtree(backup_dir, ignore_errors=True)
            try:
                backup_dir.parent.rmdir()
            except OSError:
                pass

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
            if rollback_ok:
                discard_backup_dir()
            self.cleanup()
            raise TransactionError(
                f"提交失败，{'已恢复旧文件' if rollback_ok else '自动恢复不完整，请检查替换备份'}：{exc}",
                rolled_back=rollback_ok,
            ) from exc

        self.cleanup()
        if backup_dir.exists() and not self.keep_backup:
            discard_backup_dir()
            kept_backup: Path | None = None
        elif backup_dir.exists() and not any(backup_dir.rglob("*")):
            shutil.rmtree(backup_dir, ignore_errors=True)
            kept_backup: Path | None = None
        else:
            kept_backup = backup_dir if backup_dir.exists() else None
        return CommitResult(final_media, final_cover, kept_backup)


class RecordDeletionTransaction:
    """删除单条记录的关联文件，并把后续文件编号前移一位。"""

    def __init__(self, output_dir: Path, seq: int, *, keep_backup: bool = False):
        self.output_dir = Path(output_dir)
        self.seq = int(seq)
        self.keep_backup = bool(keep_backup)
        self.stage_root = self.output_dir / ".staging" / f"delete_{self.seq}_{uuid.uuid4().hex}"

    def _entries(self) -> list[dict]:
        entries: list[dict] = []
        specs = (
            (self.output_dir / "爆款视频", re.compile(r"^(\d+)\.mp4$")),
            (self.output_dir / "爆款视频", re.compile(r"^(\d+)$")),
            (self.output_dir / "文案提取", re.compile(r"^(\d+)\.txt$")),
            (self.output_dir / "封面", re.compile(r"^(\d+)(\..+)$")),
        )
        seen: set[Path] = set()
        for folder, pattern in specs:
            if not folder.exists():
                continue
            for path in folder.iterdir():
                match = pattern.fullmatch(path.name)
                if not match or path in seen:
                    continue
                number = int(match.group(1))
                if number < self.seq:
                    continue
                seen.add(path)
                suffix = path.name[len(str(number)) :]
                renamed = path.with_name(f"{number - 1}{suffix}") if number > self.seq else None
                entries.append(
                    {
                        "seq": number,
                        "original": path,
                        "staged": self.stage_root / path.relative_to(self.output_dir),
                        "renamed": renamed,
                        "archive": None,
                    }
                )
        return sorted(entries, key=lambda item: (item["seq"], str(item["original"])))

    @staticmethod
    def _move(source: Path, destination: Path) -> None:
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(source), str(destination))

    def _cleanup(self) -> None:
        shutil.rmtree(self.stage_root, ignore_errors=True)
        try:
            self.stage_root.parent.rmdir()
        except OSError:
            pass

    def commit(self, persist_workbook: Callable[[], bool]) -> DeletionResult:
        entries = self._entries()
        timestamp = datetime.now().strftime("%Y-%m-%d_%H%M%S_%f")
        backup_dir = self.output_dir / "删除备份" / f"{timestamp}_{self.seq}"
        promoted: list[dict] = []
        archived: list[dict] = []
        try:
            for entry in entries:
                self._move(entry["original"], entry["staged"])
            for entry in entries:
                if entry["renamed"] is not None:
                    self._move(entry["staged"], entry["renamed"])
                    promoted.append(entry)
                elif self.keep_backup:
                    archive = backup_dir / entry["original"].relative_to(self.output_dir)
                    self._move(entry["staged"], archive)
                    entry["archive"] = archive
                    archived.append(entry)
            if not persist_workbook():
                raise RuntimeError(f"提取记录.xlsx 中没有顺序 {self.seq}")
        except Exception as exc:
            rollback_ok = True
            for entry in reversed(promoted):
                try:
                    self._move(entry["renamed"], entry["original"])
                except OSError:
                    rollback_ok = False
            for entry in reversed(archived):
                try:
                    self._move(entry["archive"], entry["original"])
                except OSError:
                    rollback_ok = False
            for entry in reversed(entries):
                if entry["staged"].exists():
                    try:
                        self._move(entry["staged"], entry["original"])
                    except OSError:
                        rollback_ok = False
            self._cleanup()
            if rollback_ok:
                shutil.rmtree(backup_dir, ignore_errors=True)
                try:
                    backup_dir.parent.rmdir()
                except OSError:
                    pass
            raise TransactionError(
                f"删除失败，{'已恢复原记录文件' if rollback_ok else '自动恢复不完整，请检查删除备份'}：{exc}",
                rolled_back=rollback_ok,
            ) from exc

        deleted = sum(1 for entry in entries if entry["seq"] == self.seq)
        shifted = sum(1 for entry in entries if entry["seq"] > self.seq)
        self._cleanup()
        kept_backup = backup_dir if backup_dir.exists() else None
        return DeletionResult(deleted, shifted, kept_backup)
