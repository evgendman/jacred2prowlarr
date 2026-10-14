import json
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest import mock

from tools import collect_hdr_samples as collector


class HdrCollectorTests(unittest.TestCase):
    def test_default_source_is_loopback(self):
        self.assertTrue(collector.is_loopback_url(collector.DEFAULT_SOURCE_URL))
        self.assertFalse(collector.is_loopback_url("https://jac.red/api/v2.0/indexers/all/results"))
        self.assertFalse(collector.is_loopback_url("http://192.168.0.14:9117/api"))
        self.assertFalse(collector.is_loopback_url("http://user:pass@127.0.0.1:9117/api"))

    def test_extract_infohash_from_magnet_without_retaining_link(self):
        infohash = "0123456789abcdef0123456789abcdef01234567"
        release = {
            "Title": "Lanterns S01E01 2160p HDR10+",
            "MagnetUri": f"magnet:?xt=urn:btih:{infohash}&dn=sample",
            "Details": "https://tracker.example/details/123",
            "info": {"videotype": "HDR10+", "page_url": "https://example.invalid/release"},
        }
        redactions = {}
        cleaned = collector.make_sanitized_release(release, 1, redactions)
        serialized = json.dumps(cleaned, ensure_ascii=False)

        self.assertEqual(cleaned["infohash"], infohash)
        self.assertEqual(cleaned["sample_id"], infohash)
        self.assertNotIn("MagnetUri", cleaned)
        self.assertNotIn("Details", cleaned)
        self.assertNotIn("page_url", cleaned["info"])
        self.assertNotIn("magnet:", serialized)
        self.assertNotIn("tracker.example", serialized)
        self.assertIn("info.page_url", redactions)

    def test_field_schema_profiles_nested_ffprobe_fields(self):
        records = [
            {
                "Title": "Lanterns S01E01 HDR10+",
                "info": {"videotype": "HDR10+", "quality": "2160p"},
                "ffprobe": [
                    {
                        "codec_type": "video",
                        "codec_name": "hevc",
                        "color_transfer": "smpte2084",
                        "side_data_list": [
                            {"side_data_type": "Mastering display metadata"}
                        ],
                        "stream_url": "https://example.invalid/stream",
                    },
                    {"codec_type": "audio", "codec_name": "eac3"},
                ],
                "MagnetUri": "magnet:?xt=urn:btih:0123456789abcdef0123456789abcdef01234567",
            },
            {
                "Title": "Lanterns S01E02 SDR",
                "info": {"videotype": "SDR"},
                "ffprobe": [
                    {"codec_type": "video", "codec_name": "h264", "color_transfer": "bt709"}
                ],
            },
        ]
        schema = collector.build_field_schema(records)
        fields = schema["fields"]
        self.assertEqual(fields["info.videotype"]["release_count"], 2)
        self.assertEqual(fields["ffprobe[].color_transfer"]["release_count"], 2)
        self.assertIn("ffprobe[].side_data_list[].side_data_type", fields)
        self.assertIn("ffprobe[].stream_url", schema["redacted_fields"])
        self.assertEqual(schema["redacted_fields"]["ffprobe[].stream_url"]["occurrences"], 1)

    def test_hdr_markers_do_not_conflate_hdr10_plus_and_generic_hdr(self):
        self.assertEqual(
            collector.detect_hdr_markers("Lanterns S01E01 2160p HDR10+"),
            ["HDR10+ text"],
        )
        self.assertEqual(
            collector.detect_hdr_markers("Lanterns Dolby Vision HDR"),
            ["Dolby Vision / DoVi text", "generic HDR text"],
        )
        self.assertEqual(
            collector.detect_hdr_markers("Lanterns SDR"),
            ["SDR text"],
        )
        self.assertIn("ambiguous DV token", collector.detect_hdr_markers("release DV"))
        self.assertEqual(collector.detect_hdr_markers("ordinary 1080p release"), [])

    def test_video_stream_selection_ignores_audio(self):
        streams = collector.find_video_streams([
            {"codec_type": "video", "codec_name": "hevc"},
            {"codec_type": "audio", "codec_name": "eac3"},
            {"codec_type": "video", "codec_name": "mjpeg"},
        ])
        self.assertEqual([s["codec_name"] for s in streams], ["hevc", "mjpeg"])

    def test_collector_writes_archive_without_urls_or_magnets(self):
        infohash = "0123456789abcdef0123456789abcdef01234567"
        payload = {
            "Results": [{
                "Title": "Lanterns S01E01 2160p HDR10+",
                "MagnetUri": f"magnet:?xt=urn:btih:{infohash}&dn=sample",
                "Details": "https://tracker.example/details/123",
                "PublishDate": "2026-10-01T00:00:00Z",
                "Category": [5000],
                "info": {"videotype": "HDR10+", "quality": "2160p"},
                "ffprobe": [{
                    "codec_type": "video",
                    "codec_name": "hevc",
                    "color_transfer": "smpte2084",
                    "side_data_list": [{"side_data_type": "Mastering display metadata"}],
                }],
            }],
            "jacred": {"version": "3.18.0-oss"},
        }

        with tempfile.TemporaryDirectory() as tmp:
            output = Path(tmp) / "hdr-samples"
            args = collector.parse_args(["--output-dir", str(output)])
            with mock.patch.object(collector, "fetch_once", return_value=(payload, 4321)):
                output_dir, archive_path, summary = collector.collect(args)

            self.assertEqual(summary["results_count"], 1)
            self.assertEqual(summary["unique_infohash_count"], 1)
            self.assertTrue((output_dir / "field_schema.json").exists())
            self.assertTrue((output_dir / "hdr_observations.json").exists())
            self.assertTrue(archive_path.exists())
            with zipfile.ZipFile(archive_path) as archive:
                combined = "\n".join(
                    archive.read(name).decode("utf-8")
                    for name in archive.namelist()
                    if not name.endswith("/")
                )
            self.assertNotIn("magnet:", combined)
            self.assertNotIn("tracker.example", combined)
            self.assertIn("color_transfer", combined)
            self.assertIn("HDR10+ text", combined)


if __name__ == "__main__":
    unittest.main()
