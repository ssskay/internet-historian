"""Tests for Netscape bookmarks parsing (add --bookmarks)."""

import unittest
from pathlib import Path

import historian

FIXTURE = Path(__file__).resolve().parent / "tests" / "fixtures" / "bookmarks.html"


class BookmarksParseTests(unittest.TestCase):
    def setUp(self):
        self.html = FIXTURE.read_text(encoding="utf-8")

    def test_all_http_links_collected(self):
        urls = historian.parse_bookmarks(self.html)
        self.assertIn("https://example.com/top-level", urls)
        self.assertIn("https://chiikawa.jp/", urls)
        self.assertIn("https://www.anime-chiikawa.jp/?utm_source=bookmarks", urls)
        self.assertIn("https://nagano-market.com/", urls)   # nested subfolder
        self.assertIn("https://example-comic.com/", urls)

    def test_non_http_links_dropped(self):
        urls = historian.parse_bookmarks(self.html)
        self.assertFalse(any(u.startswith("javascript:") for u in urls))

    def test_folder_scopes_to_subtree(self):
        # Chiikawa's subtree includes its own links AND the nested Shops folder,
        # but not the sibling Webcomics folder or the top-level bookmark.
        urls = historian.parse_bookmarks(self.html, folder="Chiikawa")
        self.assertIn("https://chiikawa.jp/", urls)
        self.assertIn("https://nagano-market.com/", urls)   # nested under Chiikawa
        self.assertNotIn("https://example-comic.com/", urls)  # Webcomics sibling
        self.assertNotIn("https://example.com/top-level", urls)

    def test_folder_matches_a_leaf_subfolder(self):
        urls = historian.parse_bookmarks(self.html, folder="Shops")
        self.assertEqual(urls, ["https://nagano-market.com/"])

    def test_folder_is_case_insensitive(self):
        urls = historian.parse_bookmarks(self.html, folder="webcomics")
        self.assertEqual(urls, ["https://example-comic.com/"])

    def test_unknown_folder_yields_nothing(self):
        self.assertEqual(historian.parse_bookmarks(self.html, folder="Nope"), [])


if __name__ == "__main__":
    unittest.main(verbosity=2)
