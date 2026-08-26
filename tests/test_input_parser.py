# -*- coding: utf-8 -*-

import unittest

import input_parser


class InputParserTests(unittest.TestCase):
    def test_normalize_locks_sequence_and_keeps_share_text(self):
        source = "9. https://v.douyin.com/Abc_12/\n------------\n2. 9.25 原文 https://v.douyin.com/Xyz-9/"
        normalized = input_parser.normalize_input_text(source)
        self.assertIn("1. https://v.douyin.com/Abc_12/", normalized)
        self.assertIn("2. 9.25 原文 https://v.douyin.com/Xyz-9/", normalized)
        self.assertTrue(normalized.endswith("3."))

    def test_multiple_urls_in_one_block_become_unlocked_jobs(self):
        jobs, ignored = input_parser.build_input_jobs(
            "1. https://v.douyin.com/One/ https://v.douyin.com/Two/"
        )
        self.assertEqual(ignored, 0)
        self.assertEqual(
            jobs,
            [
                (None, "https://v.douyin.com/One/"),
                (None, "https://v.douyin.com/Two/"),
            ],
        )

    def test_single_extract_from_placeholder_falls_back(self):
        text = "1. https://v.douyin.com/One/\n------------\n2."
        self.assertEqual(
            input_parser.input_job_at_line(text, 3),
            (1, "https://v.douyin.com/One/"),
        )

    def test_records_are_imported_in_document_sequence(self):
        records = {
            9: {"raw_input": "分享文案 https://v.douyin.com/Nine/"},
            2: {
                "raw_input": (
                    "https://www.douyin.com/video/200 "
                    "https://www.douyin.com/note/201"
                )
            },
        }
        links = input_parser.links_from_records_in_sequence(records)
        self.assertEqual(
            links,
            [
                "https://www.douyin.com/video/200",
                "https://www.douyin.com/note/201",
                "https://v.douyin.com/Nine/",
            ],
        )
        formatted = input_parser.format_ordered_links(links)
        jobs, ignored = input_parser.build_input_jobs(formatted)
        self.assertEqual(ignored, 0)
        self.assertEqual(jobs, [(1, links[0]), (2, links[1]), (3, links[2])])
        self.assertTrue(formatted.endswith("4."))

    def test_delete_matching_link_removes_block_and_renumbers(self):
        source = input_parser.format_ordered_links(
            [
                "https://www.douyin.com/video/100",
                "https://www.douyin.com/video/200",
                "https://www.douyin.com/video/300",
            ]
        )

        updated, removed = input_parser.remove_matching_entry(
            source, 2, "https://www.douyin.com/video/200"
        )

        self.assertEqual(removed, 1)
        jobs, ignored = input_parser.build_input_jobs(updated)
        self.assertEqual(ignored, 0)
        self.assertEqual(
            jobs,
            [
                (1, "https://www.douyin.com/video/100"),
                (2, "https://www.douyin.com/video/300"),
            ],
        )

    def test_delete_missing_link_does_not_remove_same_position(self):
        source = input_parser.format_ordered_links(
            ["https://www.douyin.com/video/100", "https://www.douyin.com/video/200"]
        )

        updated, removed = input_parser.remove_matching_entry(
            source, 1, "https://www.douyin.com/video/999"
        )

        self.assertEqual(removed, 0)
        self.assertEqual(
            input_parser.build_input_jobs(updated)[0],
            input_parser.build_input_jobs(source)[0],
        )


if __name__ == "__main__":
    unittest.main()
