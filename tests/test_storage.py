# -*- coding: utf-8 -*-

from pathlib import Path
import tempfile
import unittest

from storage import ArtifactTransaction, RecordDeletionTransaction, TransactionError


class StorageTransactionTests(unittest.TestCase):
    def _deletion_files(self, root: Path) -> None:
        for folder in (root / "爆款视频", root / "文案提取", root / "封面"):
            folder.mkdir(parents=True, exist_ok=True)
        (root / "爆款视频" / "2.mp4").write_bytes(b"delete-video")
        (root / "文案提取" / "2.txt").write_text("delete-caption", encoding="utf-8")
        (root / "封面" / "2.jpg").write_bytes(b"delete-cover")
        (root / "爆款视频" / "3.mp4").write_bytes(b"shift-video")
        (root / "文案提取" / "3.txt").write_text("shift-caption", encoding="utf-8")
        (root / "封面" / "3.jpg").write_bytes(b"shift-cover")

    def test_record_deletion_removes_target_and_shifts_later_files(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            self._deletion_files(root)

            result = RecordDeletionTransaction(root, 2).commit(lambda: True)

            self.assertEqual(result.deleted_artifacts, 3)
            self.assertEqual(result.shifted_artifacts, 3)
            self.assertIsNone(result.backup_dir)
            self.assertEqual((root / "爆款视频" / "2.mp4").read_bytes(), b"shift-video")
            self.assertEqual(
                (root / "文案提取" / "2.txt").read_text(encoding="utf-8"),
                "shift-caption",
            )
            self.assertFalse((root / "爆款视频" / "3.mp4").exists())
            self.assertFalse((root / ".staging").exists())

    def test_record_deletion_failure_restores_all_files(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            self._deletion_files(root)

            with self.assertRaises(TransactionError) as caught:
                RecordDeletionTransaction(root, 2).commit(
                    lambda: (_ for _ in ()).throw(OSError("写表失败"))
                )

            self.assertTrue(caught.exception.rolled_back)
            self.assertEqual((root / "爆款视频" / "2.mp4").read_bytes(), b"delete-video")
            self.assertEqual((root / "爆款视频" / "3.mp4").read_bytes(), b"shift-video")
            self.assertFalse((root / ".staging").exists())

    def test_record_deletion_can_keep_deleted_files_in_backup(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            self._deletion_files(root)

            result = RecordDeletionTransaction(root, 2, keep_backup=True).commit(lambda: True)

            self.assertIsNotNone(result.backup_dir)
            self.assertEqual(
                (result.backup_dir / "爆款视频" / "2.mp4").read_bytes(),
                b"delete-video",
            )
            self.assertEqual((root / "爆款视频" / "2.mp4").read_bytes(), b"shift-video")

    def test_workbook_failure_restores_old_video_and_caption(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            old_video = root / "爆款视频" / "1.mp4"
            old_caption = root / "文案提取" / "1.txt"
            old_video.parent.mkdir(parents=True)
            old_caption.parent.mkdir(parents=True)
            old_video.write_bytes(b"old-video")
            old_caption.write_text("用户文案", encoding="utf-8")

            tx = ArtifactTransaction(root, 1, keep_backup=False)
            staged = tx.video_target()
            staged.write_bytes(b"new-video")
            with self.assertRaises(TransactionError) as caught:
                tx.commit(
                    work_kind="video",
                    staged_media=staged,
                    staged_cover=None,
                    persist_workbook=lambda _cover: (_ for _ in ()).throw(OSError("写表失败")),
                )
            self.assertTrue(caught.exception.rolled_back)
            self.assertEqual(old_video.read_bytes(), b"old-video")
            self.assertEqual(old_caption.read_text(encoding="utf-8"), "用户文案")
            self.assertFalse((root / "替换备份").exists())

    def test_successful_replace_can_discard_long_term_backup(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            old_video = root / "爆款视频" / "4.mp4"
            old_video.parent.mkdir(parents=True)
            old_video.write_bytes(b"old")

            tx = ArtifactTransaction(root, 4, keep_backup=False)
            staged = tx.video_target()
            staged.write_bytes(b"new")
            result = tx.commit(
                work_kind="video",
                staged_media=staged,
                staged_cover=None,
                persist_workbook=lambda _cover: None,
            )

            self.assertEqual(old_video.read_bytes(), b"new")
            self.assertIsNone(result.backup_dir)
            self.assertFalse((root / "替换备份").exists())

    def test_video_to_note_removes_conflict_and_backs_up_caption(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            old_video = root / "爆款视频" / "2.mp4"
            caption = root / "文案提取" / "2.txt"
            old_video.parent.mkdir(parents=True)
            caption.parent.mkdir(parents=True)
            old_video.write_bytes(b"old")
            caption.write_text("保留我", encoding="utf-8")

            tx = ArtifactTransaction(root, 2)
            note = tx.note_target()
            note.mkdir(parents=True)
            (note / "1.jpg").write_bytes(b"image")
            result = tx.commit(
                work_kind="note",
                staged_media=note,
                staged_cover=None,
                persist_workbook=lambda _cover: None,
            )
            self.assertFalse(old_video.exists())
            self.assertTrue((root / "爆款视频" / "2" / "1.jpg").exists())
            self.assertFalse(caption.exists())
            self.assertIsNotNone(result.backup_dir)
            self.assertEqual(
                (result.backup_dir / "文案" / "2.txt").read_text(encoding="utf-8"),
                "保留我",
            )

    def test_note_to_video_removes_old_directory_and_creates_caption(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            old_note = root / "爆款视频" / "3"
            old_note.mkdir(parents=True)
            (old_note / "1.jpg").write_bytes(b"old-image")

            tx = ArtifactTransaction(root, 3)
            video = tx.video_target()
            video.write_bytes(b"video")
            tx.commit(
                work_kind="video",
                staged_media=video,
                staged_cover=None,
                persist_workbook=lambda _cover: None,
            )
            self.assertFalse(old_note.exists())
            self.assertEqual((root / "爆款视频" / "3.mp4").read_bytes(), b"video")
            self.assertTrue((root / "文案提取" / "3.txt").exists())


if __name__ == "__main__":
    unittest.main()
