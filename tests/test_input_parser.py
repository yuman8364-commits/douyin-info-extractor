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

    def test_delete_later_duplicate_keeps_earlier_duplicate_untouched(self):
        repeated = "https://www.douyin.com/video/100"
        source = input_parser.format_ordered_links(
            [repeated, "https://www.douyin.com/video/200", repeated]
        )

        updated, removed = input_parser.remove_matching_entry(source, 3, repeated)

        self.assertEqual(removed, 1)
        self.assertEqual(
            input_parser.build_input_jobs(updated)[0],
            [
                (1, repeated),
                (2, "https://www.douyin.com/video/200"),
            ],
        )

    def test_ambiguous_duplicate_is_not_removed_when_sequence_does_not_match(self):
        repeated = "https://www.douyin.com/video/100"
        source = input_parser.format_ordered_links([repeated, repeated])

        updated, removed = input_parser.remove_matching_entry(source, 9, repeated)

        self.assertEqual(removed, 0)
        self.assertEqual(updated, source)

    def test_duplicate_paste_reports_existing_sequence(self):
        source = input_parser.format_ordered_links(
            ["https://www.douyin.com/video/100", "https://v.douyin.com/Abc/"]
        )
        duplicates = input_parser.existing_duplicate_urls(
            source, "再次分享 https://v.douyin.com/Abc/"
        )
        self.assertEqual(duplicates, [("https://v.douyin.com/Abc/", 2)])

    def test_duplicate_paste_matches_same_work_across_long_link_variants(self):
        source = input_parser.format_ordered_links(
            ["https://www.douyin.com/video/123?previous_page=one"]
        )
        duplicates = input_parser.existing_duplicate_urls(
            source, "https://www.douyin.com/note/123?previous_page=two"
        )
        self.assertEqual(
            duplicates,
            [("https://www.douyin.com/note/123", 1)],
        )

    def test_duplicate_paste_normalizes_short_link_query_and_slash(self):
        source = input_parser.format_ordered_links(
            ["https://v.douyin.com/AbC123/?share_token=old"]
        )
        duplicates = input_parser.existing_duplicate_urls(
            source, "http://v.douyin.com/AbC123?share_token=new"
        )
        self.assertEqual(
            duplicates,
            [("http://v.douyin.com/AbC123", 1)],
        )

    def test_delete_input_entry_only_shifts_following_sequences(self):
        source = input_parser.format_ordered_links(
            [
                "https://www.douyin.com/video/100",
                "https://www.douyin.com/video/200",
                "https://www.douyin.com/video/300",
                "https://www.douyin.com/video/400",
            ]
        )
        updated, removed_seq, removed_raw = input_parser.remove_entry_at_line(source, 5)
        self.assertEqual(removed_seq, 3)
        self.assertEqual(removed_raw, "https://www.douyin.com/video/300")
        self.assertEqual(
            input_parser.build_input_jobs(updated)[0],
            [
                (1, "https://www.douyin.com/video/100"),
                (2, "https://www.douyin.com/video/200"),
                (3, "https://www.douyin.com/video/400"),
            ],
        )

    def test_delete_input_entry_does_nothing_on_divider_or_placeholder(self):
        source = input_parser.format_ordered_links(
            ["https://www.douyin.com/video/100", "https://www.douyin.com/video/200"]
        )
        for line_number in (2, 5):
            updated, removed_seq, removed_raw = input_parser.remove_entry_at_line(
                source, line_number
            )
            self.assertIsNone(removed_seq)
            self.assertEqual(removed_raw, "")
            self.assertEqual(updated, source)


if __name__ == "__main__":
    unittest.main()
