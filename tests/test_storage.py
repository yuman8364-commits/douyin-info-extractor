# -*- coding: utf-8 -*-

from pathlib import Path
import tempfile
import unittest

from storage import ArtifactTransaction, TransactionError


class StorageTransactionTests(unittest.TestCase):
    def test_workbook_failure_restores_old_video_and_caption(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            old_video = root / "爆款视频" / "1.mp4"
            old_caption = root / "文案提取" / "1.txt"
            old_video.parent.mkdir(parents=True)
            old_caption.parent.mkdir(parents=True)
            old_video.write_bytes(b"old-video")
            old_caption.write_text("用户文案", encoding="utf-8")

            tx = ArtifactTransaction(root, 1)
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
