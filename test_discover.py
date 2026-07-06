"""Tests for the `discover` command's Wikipedia/Wikidata parsing.

These exercise the pure parsers against recorded JSON fixtures (tests/fixtures/),
so no network is touched. The fixtures mirror the real shapes returned by the
MediaWiki action API (formatversion=2) and the Wikidata wbgetclaims API.
"""

import json
import unittest
from pathlib import Path

import historian

FIXTURES = Path(__file__).resolve().parent / "tests" / "fixtures"


def load(name):
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


class SearchTests(unittest.TestCase):
    def test_parse_search_results_strips_snippet_html(self):
        results = historian.parse_search_results(load("wiki_search.json"))
        self.assertEqual(len(results), 3)
        self.assertEqual(results[0]["title"], "Chiikawa")
        self.assertEqual(results[0]["pageid"], 66492530)
        # The <span class="searchmatch"> markup is flattened away.
        self.assertNotIn("<span", results[0]["snippet"])
        self.assertIn("Japanese manga series", results[0]["snippet"])

    def test_empty_search_is_graceful(self):
        self.assertEqual(historian.parse_search_results(load("wiki_search_empty.json")), [])
        self.assertEqual(historian.parse_search_results({}), [])


class PageInfoTests(unittest.TestCase):
    def test_parse_page_info_extracts_qid_and_url(self):
        info = historian.parse_page_info(load("wiki_page_info.json"))
        self.assertEqual(info["title"], "Chiikawa")
        self.assertEqual(info["qid"], "Q104814230")
        self.assertEqual(info["url"], "https://en.wikipedia.org/wiki/Chiikawa")
        self.assertFalse(info["is_disambiguation"])

    def test_disambiguation_flag_detected(self):
        info = historian.parse_page_info(load("wiki_page_info_disambig.json"))
        self.assertTrue(info["is_disambiguation"])

    def test_missing_page_returns_none(self):
        data = {"query": {"pages": [{"title": "Nope", "missing": True}]}}
        self.assertIsNone(historian.parse_page_info(data))
        self.assertIsNone(historian.parse_page_info({}))


class OfficialSiteTests(unittest.TestCase):
    def test_parse_official_sites_p856(self):
        sites = historian.parse_official_sites(load("wikidata_p856.json"))
        # The one 'value' claim is kept; the 'novalue' snak is skipped.
        self.assertEqual(sites, ["https://chiikawa.jp/"])

    def test_no_claims_is_empty(self):
        self.assertEqual(historian.parse_official_sites({"claims": {}}), [])
        self.assertEqual(historian.parse_official_sites({}), [])


class ExtLinkTests(unittest.TestCase):
    def test_parse_extlinks_reads_all_urls(self):
        urls = historian.parse_extlinks(load("wiki_extlinks.json"))
        self.assertIn("https://chiikawa.jp/", urls)
        # Protocol-relative link is promoted to https.
        self.assertIn("https://nagano-chiikawa.com/shop", urls)

    def test_filter_extlinks_drops_reference_junk(self):
        urls = historian.parse_extlinks(load("wiki_extlinks.json"))
        kept = historian.filter_extlinks(urls)
        self.assertIn("https://chiikawa.jp/", kept)
        self.assertIn("https://www.anime-chiikawa.jp/", kept)
        self.assertIn("https://nagano-chiikawa.com/shop", kept)
        joined = " ".join(kept)
        for junk in ("web.archive.org", "doi.org", "wikidata.org",
                     "jstor.org", "books.google"):
            self.assertNotIn(junk, joined)

    def test_filter_extlinks_dedupes(self):
        dupes = ["https://a.example/", "https://a.example/", "https://b.example/"]
        self.assertEqual(historian.filter_extlinks(dupes),
                         ["https://a.example/", "https://b.example/"])


class DisambiguationTests(unittest.TestCase):
    def test_parse_disambiguation_options(self):
        opts = historian.parse_disambiguation_options(load("wiki_disambig_links.json"))
        self.assertEqual(opts,
                         ["Mercury (planet)", "Mercury (element)", "Mercury (mythology)"])


class SelectionTests(unittest.TestCase):
    def test_all_keywords(self):
        self.assertEqual(historian.parse_selection("a", 3), [0, 1, 2])
        self.assertEqual(historian.parse_selection("all", 3), [0, 1, 2])

    def test_number_list_is_1_based_in_0_based_out(self):
        self.assertEqual(historian.parse_selection("1,3", 3), [0, 2])
        self.assertEqual(historian.parse_selection("2 1", 3), [0, 1])

    def test_out_of_range_and_garbage_ignored(self):
        self.assertEqual(historian.parse_selection("1, 9, foo, 2", 3), [0, 1])

    def test_empty_selects_nothing(self):
        self.assertEqual(historian.parse_selection("", 3), [])
        self.assertEqual(historian.parse_selection("   ", 3), [])


class CandidateAssemblyTests(unittest.TestCase):
    """_discover_candidates orders official > wikipedia > extlinks and de-dupes."""

    def test_official_site_beats_duplicate_extlink(self):
        from unittest import mock

        session = mock.Mock()

        def fake_get_json(_session, url, **kw):
            if url == historian.WIKIDATA_API:
                return load("wikidata_p856.json")
            return load("wiki_extlinks.json")  # any wikipedia api.php call

        info = {"title": "Chiikawa", "qid": "Q104814230",
                "url": "https://en.wikipedia.org/wiki/Chiikawa"}
        with mock.patch.object(historian, "_get_json", fake_get_json):
            cands = historian._discover_candidates(session, info)

        labels = [c["label"] for c in cands]
        urls = [c["url"] for c in cands]
        # Official site first, wikipedia article next.
        self.assertEqual(labels[0], "official site")
        self.assertEqual(cands[0]["url"], "https://chiikawa.jp/")
        self.assertEqual(labels[1], "wikipedia")
        self.assertIn("https://en.wikipedia.org/wiki/Chiikawa", urls)
        # chiikawa.jp appears once even though it's also an external link.
        self.assertEqual(urls.count("https://chiikawa.jp/"), 1)


if __name__ == "__main__":
    unittest.main(verbosity=2)
