# -*- coding: utf-8 -*-

from pathlib import Path
import unittest


class ReleasePrivacyTests(unittest.TestCase):
    def test_build_script_never_copies_personal_state(self):
        script = (Path(__file__).parents[1] / "构建发布版.ps1").read_text(encoding="utf-8")
        self.assertNotIn('Copy-Item -Force $source', script)
        self.assertIn('[System.Text.UTF8Encoding]::new($false)', script)
        self.assertIn('Release privacy check failed', script)
        self.assertIn('$browserProfilePath', script)
        self.assertIn('Remove-Item -LiteralPath $browserProfilePath', script)

    def test_existing_release_state_is_blank_when_present(self):
        data = Path(__file__).parents[1] / "dist" / "抖音信息提取工具" / "data"
        if not data.exists():
            self.skipTest("发布目录尚未生成")
        config = (data / "config.json").read_text(encoding="utf-8-sig").strip()
        cache = (data / "input_cache.txt").read_text(encoding="utf-8-sig").strip()
        self.assertEqual(config, "{}")
        self.assertEqual(cache, "1.")
        self.assertNotIn("http", config.lower() + cache.lower())
        profile = data / "browser_profile"
        self.assertFalse(profile.exists(), "发布包不得携带浏览器 Cookie 或缓存")


if __name__ == "__main__":
    unittest.main()
