"""Link parsing tests — the part that must never regress, since a mis-parse
either silently drops a link the user pasted or sends junk to yt-dlp.

Run: python3 -m unittest discover -s tests -v   (from the project root)
"""

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from igbulk import links  # noqa: E402


class TestClassify(unittest.TestCase):
    def test_reel_variants_canonicalise(self):
        for raw in [
            "https://www.instagram.com/reel/DAbc123_xy/",
            "http://instagram.com/reel/DAbc123_xy",
            "instagram.com/reels/DAbc123_xy/",
            "https://www.instagram.com/nasa/reel/DAbc123_xy/",
            "https://www.instagram.com/reel/DAbc123_xy/?igsh=MzRlODBiNWFlZA==",
            "https://www.instagram.com/reel/DAbc123_xy/?img_index=2#foo",
        ]:
            link = links.classify(raw)
            self.assertIsNotNone(link, raw)
            self.assertEqual(link.url, "https://www.instagram.com/reel/DAbc123_xy/", raw)
            self.assertEqual(link.kind, "reel")
            self.assertEqual(link.shortcode, "DAbc123_xy")

    def test_post_and_tv(self):
        self.assertEqual(links.classify("https://instagram.com/p/XyZ-9/").url,
                         "https://www.instagram.com/p/XyZ-9/")
        self.assertEqual(links.classify("https://instagram.com/p/XyZ-9/").kind, "post")
        self.assertEqual(links.classify("https://instagram.com/tv/AbC12/").kind, "tv")

    def test_mirror_hosts(self):
        link = links.classify("https://ddinstagram.com/reel/AbC123/")
        self.assertIsNotNone(link)
        self.assertEqual(link.shortcode, "AbC123")

    def test_share_link_kept_intact(self):
        link = links.classify("https://www.instagram.com/share/reel/_kL9xQ2/")
        self.assertEqual(link.kind, "share")
        self.assertTrue(link.url.endswith("/share/reel/_kL9xQ2/"))
        self.assertTrue(link.supported)

    def test_login_only_kinds(self):
        story = links.classify("https://www.instagram.com/stories/nasa/3412345678901234567/")
        self.assertEqual(story.kind, "story")
        self.assertFalse(story.supported)
        hl = links.classify("https://www.instagram.com/stories/highlights/17901234567890123/")
        self.assertEqual(hl.kind, "highlight")
        self.assertFalse(hl.supported)
        profile = links.classify("https://www.instagram.com/nasa/")
        self.assertEqual(profile.kind, "profile")
        self.assertFalse(profile.supported)

    def test_reserved_paths_are_not_profiles(self):
        for raw in ["https://www.instagram.com/explore/",
                    "https://www.instagram.com/accounts/login/",
                    "https://www.instagram.com/direct/inbox/"]:
            link = links.classify(raw)
            self.assertTrue(link is None or link.kind != "profile", raw)

    def test_non_instagram_ignored(self):
        self.assertIsNone(links.classify("https://tiktok.com/@x/video/123"))
        self.assertIsNone(links.classify("notaurl"))


class TestParse(unittest.TestCase):
    def test_messy_paste(self):
        text = """
        hey check these out:
        https://www.instagram.com/reel/AAA111/?igsh=abc
        "https://www.instagram.com/reel/AAA111/"          <- same one again
        (https://www.instagram.com/p/BBB222/),
        [look](https://instagram.com/reel/CCC333/)
        https://youtube.com/watch?v=xyz
        """
        result = links.parse(text)
        self.assertEqual([l.shortcode for l in result.links], ["AAA111", "BBB222", "CCC333"])
        self.assertEqual(result.duplicates, 1)
        self.assertEqual(result.links[0].dupes, 1)

    def test_comma_and_space_separated(self):
        text = ("https://instagram.com/reel/AAA111/,https://instagram.com/reel/BBB222/ "
                "https://instagram.com/p/CCC333/")
        result = links.parse(text)
        self.assertEqual([l.shortcode for l in result.links], ["AAA111", "BBB222", "CCC333"])

    def test_reel_and_post_with_same_code_are_distinct_entries_by_code(self):
        # Instagram shortcodes are globally unique, so /p/X and /reel/X are the
        # same media; de-dupe must collapse them.
        result = links.parse("https://instagram.com/p/AAA111/\nhttps://instagram.com/reel/AAA111/")
        self.assertEqual(len(result.links), 1)

    def test_order_preserved_and_empty_input(self):
        self.assertEqual(links.parse("").links, [])
        self.assertEqual(links.parse(None).links, [])

    def test_rejected_collected(self):
        result = links.parse("https://www.instagram.com/explore/tags/cats/")
        self.assertEqual(result.links, [])
        self.assertEqual(len(result.rejected), 1)

    def test_parse_urls_list(self):
        result = links.parse_urls(["https://instagram.com/reel/AAA111/",
                                   "https://instagram.com/p/BBB222/"])
        self.assertEqual(len(result.links), 2)

    def test_serialisation_round_trip(self):
        d = links.parse("https://instagram.com/reel/AAA111/").as_dict()
        self.assertEqual(d["count"], 1)
        self.assertEqual(d["links"][0]["kind_label"], "Reel")
        self.assertTrue(d["links"][0]["supported"])


if __name__ == "__main__":
    unittest.main()
