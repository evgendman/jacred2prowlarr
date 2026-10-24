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

    def test_bracketed_season_markers_and_contiguous_lists(self):
        cases = (
            ("Лексс / Lexx [S01] (1997) DVD5", "S01"),
            ("Лексс / Lexx [s2] (1998)", "S02"),
            ("Лексс (Сезон 1) / Lexx (Season 1)", "S01"),
            ("Lexx [s3,s4] (1999-2002)", "S03-S04"),
            ("Lexx [S01-S04]", "S01-S04"),
        )
        for title, expected in cases:
            with self.subTest(title=title):
                self.assertEqual(adapter.normalize_season_episode(title), expected)

        # Non-contiguous season lists must not be misrepresented as a range.
        self.assertEqual(adapter.normalize_season_episode("Lexx [s1,s3]"), "")

    def test_season_before_episode_notation(self):
        cases = (
            ("Debris / 1 сезон 12 серия [2021, 1080p]", "S01E12"),
            ("Debris / 1 сезон 1 серия", "S01E01"),
            ("Debris / 1-й сезон, 12-я серия", "S01E12"),
            ("Debris / Обломки (1 сезон: 1-13 серии из 13)", "S01E01-13"),
            ("Debris / Обломки (1 сезон: 1-2 серии из 8)", "S01E01-02"),
            ("Debris / 1 сезон (1-13 из 13)", "S01E01-13"),
        )
        for title, expected in cases:
            with self.subTest(title=title):
                self.assertEqual(adapter.normalize_season_episode(title), expected)

    def test_debris_episode_title_and_feed_keep_source_title(self):
        source_title = "Обломки / Debris / 1 сезон 12 серия [2021, 1080p]"
        item = lexx_item(source_title)
        item["info"].update({
            "name": "Debris",
            "originalname": "Debris",
            "relased": 2021,
            "quality": 1080,
        })

        normalized_title = adapter.make_tv_title(item)
        self.assertTrue(
            normalized_title.startswith("Debris S01E12 (2021) 1080p"),
            normalized_title,
        )
        self.assertIn("*** " + source_title, normalized_title)

        feed = ET.fromstring(adapter.make_feed([(item, "tv")]))
        feed_item = feed.find("./channel/item")
        self.assertIsNotNone(feed_item)
        self.assertTrue(
            feed_item.findtext("title").startswith("Debris S01E12 (2021) 1080p"),
            feed_item.findtext("title"),
        )
        self.assertEqual(feed_item.findtext("description"), source_title)

    def test_multi_season_labels_are_ranges_not_episode_ranges(self):
        cases = (
            ("Lexx / Сезон: 1-4 из 4 / Серии: 1-61 из 61", "S01-S04"),
            ("Lexx / Сезоны: 1-2 / Серии: 1-13 из 24", "S01-S02"),
            ("Lexx / Сезоны: 1–4", "S01-S04"),
            ("Lexx / 1-4 сезоны: 1-61 серии из 61", "S01-S04"),
        )
        for title, expected in cases:
            with self.subTest(title=title):
                normalized = adapter.normalize_season_episode(title)
                self.assertEqual(normalized, expected)
                self.assertNotIn("E", normalized)

    def test_labelled_episode_ranges_allow_missing_colons(self):
        cases = (
            ("Lexx / Сезон 1 / Серии 1-4 из 4", "S01E01-04"),
            ("Lexx / Сезон: 1 / Серии 1-4", "S01E01-04"),
            ("Lexx / Сезон 1 Серии: 1–4 из 4", "S01E01-04"),
        )
        for title, expected in cases:
            with self.subTest(title=title):
                self.assertEqual(adapter.normalize_season_episode(title), expected)

    def test_single_season_with_non_range_episode_list_does_not_invent_range(self):
        cases = (
            ("Lexx / Сезон: 1 / Серии: 1,2 (4)", "S01"),
            ("Lexx / Сезон: 2(4) / Серии: 1,2,9,12", "S02"),
            ("Lexx / Лексс (Сезон 1) / Lexx (Season 1)", "S01"),
        )
        for title, expected in cases:
            with self.subTest(title=title):
                normalized = adapter.normalize_season_episode(title)
                self.assertEqual(normalized, expected)
                self.assertNotIn("E", normalized)

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
        normalized = adapter.normalize_season_episode("Lexx / Сезон: 1")
        self.assertNotIn("E", normalized)

    def test_orphaned_parenthesis_is_removed_from_normalized_series_name(self):
        source_title = "Уламки (Сезон 1) / Debris (Season 1) (2021) WEBRip 1080p"
        item = {
            "Title": source_title,
            "Details": "https://example.invalid/debris",
            "PublishDate": "2026-10-24T00:00:00Z",
            "Category": [5000],
            "Size": 123456,
            "Seeders": 1,
            "Peers": 0,
            "info": {
                "name": "Debris (1 сезон",
                "originalname": "Debris (1 сезон",
                "relased": 2021,
                "quality": 1080,
                "types": ["serial"],
                "voices": [],
            },
        }
        title = adapter.make_tv_title(item)
        self.assertTrue(
            title.startswith("Debris S01 (2021) WEBRip 1080p"),
            title,
        )
        self.assertNotIn("Debris ( S01", title)
        self.assertIn("*** " + source_title, title)


if __name__ == "__main__":
    unittest.main()
