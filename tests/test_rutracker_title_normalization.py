import unittest
import xml.etree.ElementTree as ET

import jacred_v2_adapter as adapter


TORZNAB_NS = "http://torznab.com/schemas/2015/feed"
SOURCE_TITLE = (
    "Лексс / Lexx / Сезон: 1 / Серии: 1-4 из 4 "
    "[1997, ... DVD5] MVO (ТВ6)"
)


def lexx_item(source_title=SOURCE_TITLE):
    return {
        "Title": source_title,
        "Details": "https://example.invalid/lexx",
        "PublishDate": "2026-10-23T00:00:00Z",
        "Category": [5000],
        "Size": 123456,
        "Seeders": 1,
        "Peers": 0,
        "info": {
            "name": "Lexx",
            "originalname": "Lexx",
            "relased": 1997,
            "quality": 480,
            "types": ["serial"],
            "voices": [],
        },
    }


class RuTrackerTitleNormalizationTests(unittest.TestCase):
    def test_labelled_russian_season_episode_range(self):
        cases = (
            ("Lexx / Сезон: 1 / Серии: 1-4 из 4", "S01E01-04"),
            ("Lexx / Сезон : 01 | Серии : 01–04 из 04", "S01E01-04"),
            ("Lexx; СЕЗОН=1; СЕРИИ=1—4 из 4", "S01E01-04"),
            ("Lexx Сезон: 2, Серии: 7-12 из 12", "S02E07-12"),
        )
        for title, expected in cases:
            with self.subTest(title=title):
                self.assertEqual(adapter.normalize_season_episode(title), expected)

    def test_existing_season_range_syntax_remains_supported(self):
        self.assertEqual(
            adapter.normalize_season_episode("Lexx 1 сезон (1-4 из 4)"),
            "S01E01-04",
        )
        self.assertEqual(
            adapter.normalize_season_episode("Lexx S01E01-04 WEB-DL 480p"),
            "S01E01-04",
        )

    def test_dvd_source_formats_are_distinct_and_canonical(self):
        cases = (
            ("Lexx [DVD5]", "DVD5"),
            ("Lexx [DVD9]", "DVD9"),
            ("Lexx DVDRip", "DVDRip"),
            ("Lexx DVD-Rip", "DVDRip"),
            ("Lexx DVD", "DVD"),
        )
        for title, expected in cases:
            with self.subTest(title=title):
                self.assertEqual(adapter.extract_format(title), expected)

    def test_full_lexx_release_normalizes_as_expected(self):
        item = lexx_item()
        title = adapter.make_tv_title(item)

        self.assertTrue(
            title.startswith("Lexx S01E01-04 (1997) DVD5 480p | ТВ6"),
            title,
        )
        self.assertIn("*** " + SOURCE_TITLE, title)

    def test_torznab_title_and_description_preserve_source_title(self):
        item = lexx_item()
        feed = ET.fromstring(adapter.make_feed([(item, "tv")]))
        feed_item = feed.find("./channel/item")
        self.assertIsNotNone(feed_item)

        title = feed_item.findtext("title")
        description = feed_item.findtext("description")
        self.assertTrue(
            title.startswith("Lexx S01E01-04 (1997) DVD5 480p | ТВ6"),
            title,
        )
        self.assertEqual(description, SOURCE_TITLE)
        self.assertIn("*** " + SOURCE_TITLE, title)

        attrs = {
            node.get("name"): node.get("value")
            for node in feed_item.findall(f"{{{TORZNAB_NS}}}attr")
        }
        self.assertEqual(attrs["quality"], "480p")

    def test_missing_episode_labels_does_not_invent_episode_range(self):
        self.assertEqual(adapter.normalize_season_episode("Lexx / Сезон: 1"), "S01")


if __name__ == "__main__":
    unittest.main()
