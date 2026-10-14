import unittest

import jacred_v2_adapter as adapter


class HDRTitleNormalizationTests(unittest.TestCase):
    def test_hdr10_plus_and_dolby_vision_are_both_preserved(self):
        title = "Lanterns 4K WEB-DL HDR10+, DV 8.1, 10-bit"
        self.assertEqual(adapter.hdr_markers(title), ["HDR10+", "Dolby Vision"])

    def test_explicit_dolby_vision_and_dovi_aliases(self):
        self.assertEqual(adapter.hdr_markers("Movie 2160p Dolby Vision"), ["Dolby Vision"])
        self.assertEqual(adapter.hdr_markers("Movie UHD DoVi"), ["Dolby Vision"])

    def test_hdr10_plus_does_not_collapse_to_hdr10_or_generic_hdr(self):
        self.assertEqual(adapter.hdr_markers("Movie HDR10+"), ["HDR10+"])

    def test_hdr10_without_plus_is_specific(self):
        self.assertEqual(adapter.hdr_markers("Movie 2160p HDR10"), ["HDR10"])

    def test_hdr10_and_hdr10_plus_can_both_be_retained_if_explicit(self):
        self.assertEqual(
            adapter.hdr_markers("Movie HDR10 and HDR10+"),
            ["HDR10", "HDR10+"],
        )

    def test_dv_without_profile_or_context_is_ignored(self):
        self.assertEqual(adapter.hdr_markers("The initials DV appear here"), [])

    def test_dv_with_uhd_context_is_treated_as_dolby_vision(self):
        self.assertEqual(adapter.hdr_markers("Movie 4K DV"), ["Dolby Vision"])

    def test_dv_with_explicit_hdr10_context_is_treated_as_dolby_vision(self):
        self.assertEqual(adapter.hdr_markers("Movie HDR10 DV"), ["HDR10", "Dolby Vision"])

    def test_generic_hdr_is_used_when_no_specific_format_is_named(self):
        self.assertEqual(adapter.hdr_markers("Movie WEB-DL HDR"), ["HDR"])
        self.assertEqual(adapter.hdr_markers("Movie 4K HDR+"), ["HDR"])

    def test_explicit_sdr_is_emitted_only_without_hdr_markers(self):
        self.assertEqual(adapter.hdr_markers("Movie WEB-DL 1080p SDR"), ["SDR"])
        self.assertEqual(adapter.hdr_markers("Movie HDR10 SDR"), ["HDR10"])

    def test_hdr_is_taken_from_title_not_jacred_videotype(self):
        item = {"info": {"quality": 1080, "videotype": "hdr"}}
        self.assertEqual(adapter.extract_actual_quality(item, "Lanterns WEB-DL 1080p"), "1080p")

    def test_title_markers_are_appended_at_1080p(self):
        item = {"info": {"quality": 1080, "videotype": "sdr"}}
        self.assertEqual(
            adapter.extract_actual_quality(item, "Lanterns WEB-DL 1080p HDR10+ Dolby Vision"),
            "1080p HDR10+ Dolby Vision",
        )

    def test_title_markers_work_without_structured_resolution(self):
        self.assertEqual(
            adapter.extract_actual_quality({}, "Lanterns HDR10+ DV 8.1"),
            "HDR10+ Dolby Vision",
        )

    def test_normalized_tv_title_preserves_original_source_title(self):
        source = "Lanterns S01E01 WEB-DL 1080p HDR10+ DV 8.1"
        item = {
            "Title": source,
            "info": {
                "name": "Lanterns",
                "originalname": "Lanterns",
                "relased": 2026,
                "quality": 1080,
                "types": ["serial"],
            },
        }
        title = adapter.make_tv_title(item)
        self.assertIn("1080p HDR10+ Dolby Vision", title)
        self.assertIn("*** " + source, title)

    def test_normalized_movie_title_preserves_original_source_title(self):
        source = "Example Movie (2024) WEB-DL 2160p HDR10"
        item = {
            "Title": source,
            "info": {
                "name": "Example Movie",
                "originalname": "Example Movie",
                "relased": 2024,
                "quality": 2160,
                "types": ["movie"],
            },
        }
        title = adapter.make_movie_title(item)
        self.assertIn("2160p HDR10", title)
        self.assertIn("*** " + source, title)


if __name__ == "__main__":
    unittest.main()
