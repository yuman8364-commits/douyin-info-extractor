# -*- coding: utf-8 -*-

from pathlib import Path
import tempfile
import unittest

from PIL import Image

import app


class PreviewImageTests(unittest.TestCase):
    def test_portrait_cover_is_letterboxed_without_stretching(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "cover.png"
            Image.new("RGB", (900, 1600), (220, 20, 20)).save(path)

            preview = app.prepare_preview_image(path)
            self.assertEqual(preview.size, app.PREVIEW_IMAGE_SIZE)

            pixels = preview.load()
            colored = [
                (x, y)
                for y in range(preview.height)
                for x in range(preview.width)
                if pixels[x, y] != app.PREVIEW_BACKGROUND[:3]
            ]
            xs = [point[0] for point in colored]
            ys = [point[1] for point in colored]
            width = max(xs) - min(xs) + 1
            height = max(ys) - min(ys) + 1
            self.assertLessEqual(width, app.PREVIEW_IMAGE_SIZE[0])
            self.assertLessEqual(height, app.PREVIEW_IMAGE_SIZE[1])
            self.assertAlmostEqual(width / height, 900 / 1600, delta=0.01)
            self.assertLessEqual(abs((min(xs) + max(xs)) - (preview.width - 1)), 1)


if __name__ == "__main__":
    unittest.main()
