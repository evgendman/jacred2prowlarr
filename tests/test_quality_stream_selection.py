import unittest
import xml.etree.ElementTree as ET

import jacred_v2_adapter as adapter


TORZNAB_NS = "http://torznab.com/schemas/2015/feed"
SOURCE_TITLE = "Бременские музыканты / 2023 / РУ / WEB-DL (1080p)"


def movie_item(ffprobe, quality=1080, title=SOURCE_TITLE):
    return {
        "Title": title,
        "Details": "https://example.invalid/release",
        "PublishDate": "2023-12-01T00:00:00Z",
        "Category": [2000],
        "ffprobe": ffprobe,
        "info": {
            "name": "Бременские музыканты",
            "originalname": "Бременские музыканты",
            "relased": 2023,
            "quality": quality,
            "types": ["movie"],
        },
    }


class QualityStreamSelectionTests(unittest.TestCase):
    def test_png_cover_does_not_override_actual_h264_video(self):
        video = {
            "codec_type": "video",
            "codec_name": "h264",
            "width": 1920,
            "height": 804,
        }
        cover = {
            "codec_type": "video",
            "codec_name": "png",
            "width": 5184,
            "height": 7776,
        }
        item = movie_item([video, cover])

        self.assertIs(adapter.first_video_stream(item["ffprobe"]), video)
        self.assertEqual(adapter.extract_actual_quality(item, SOURCE_TITLE), "1080p")
        self.assertEqual(adapter.actual_resolution(item), ("1920x804", "h264"))

        normalized_title = adapter.make_movie_title(item)
        self.assertIn("1080p", normalized_title)
        self.assertNotIn("2160p", normalized_title)
        self.assertIn("*** " + SOURCE_TITLE, normalized_title)

        feed = ET.fromstring(adapter.make_feed([(item, "movie")]))
        feed_item = feed.find("./channel/item")
        self.assertIsNotNone(feed_item)
        torznab_attrs = {
            node.get("name"): node.get("value")
            for node in feed_item.findall(f"{{{TORZNAB_NS}}}attr")
        }
        self.assertEqual(torznab_attrs["quality"], "1080p")
        self.assertEqual(torznab_attrs["resolution"], "1920x804")
        self.assertEqual(torznab_attrs["video"], "h264")

    def test_attached_pic_flag_excludes_high_resolution_non_png_stream(self):
        video = {
            "codec_type": "video",
            "codec_name": "h264",
            "width": 1920,
            "height": 804,
        }
        attached_image = {
            "codec_type": "video",
            "codec_name": "hevc",
            "width": 6000,
            "height": 4000,
            "disposition": {"attached_pic": 1},
        }
        item = movie_item([attached_image, video])

        self.assertIs(adapter.first_video_stream(item["ffprobe"]), video)
        self.assertEqual(adapter.actual_resolution(item), ("1920x804", "h264"))
        self.assertEqual(adapter.extract_actual_quality(item, SOURCE_TITLE), "1080p")

    def test_png_only_ffprobe_data_falls_back_to_jacred_quality(self):
        item = movie_item(
            [{"codec_type": "video", "codec_name": "png", "width": 5184, "height": 7776}],
            quality=1080,
            title="Example Movie 2160p WEB-DL",
        )

        self.assertIsNone(adapter.first_video_stream(item["ffprobe"]))
        self.assertEqual(adapter.actual_resolution(item), ("", ""))
        self.assertEqual(adapter.extract_actual_quality(item, item["Title"]), "1080p")

    def test_title_quality_is_used_when_no_valid_video_or_jacred_quality_exists(self):
        title = "Example Movie WEB-DL 720p"
        item = movie_item(
            [{"codec_type": "video", "codec_name": "png", "width": 5184, "height": 7776}],
            quality="unknown",
            title=title,
        )

        self.assertIsNone(adapter.first_video_stream(item["ffprobe"]))
        self.assertEqual(adapter.extract_actual_quality(item, title), "720p")

    def test_normal_video_codecs_remain_eligible(self):
        for codec in ("h264", "hevc", "av1"):
            with self.subTest(codec=codec):
                stream = {
                    "codec_type": "video",
                    "codec_name": codec,
                    "width": 1920,
                    "height": 1080,
                }
                self.assertIs(adapter.first_video_stream([stream]), stream)


if __name__ == "__main__":
    unittest.main()
