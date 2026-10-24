import io
import tempfile
import unittest
from unittest import mock

import jacred_v2_adapter as adapter


class MockFailureTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.old_state_path = adapter.JACRED_STATE_PATH
        adapter.JACRED_STATE_PATH = self.temp.name + "/state.sqlite3"
        self.addCleanup(self._restore_state)
        adapter.reset_protection_state()
        with adapter.CACHE_LOCK:
            adapter.CACHE.clear()
            adapter.INFLIGHT_LOCKS.clear()
        with adapter.SOURCE_CACHE_LOCK:
            adapter.SOURCE_CACHE.clear()
            adapter.SOURCE_INFLIGHT_LOCKS.clear()
        adapter.LAST_UPSTREAM_COMPLETION = 0

    def _restore_state(self):
        adapter.JACRED_STATE_PATH = self.old_state_path
        self.temp.cleanup()
        with adapter.CACHE_LOCK:
            adapter.CACHE.clear()
            adapter.INFLIGHT_LOCKS.clear()
        with adapter.SOURCE_CACHE_LOCK:
            adapter.SOURCE_CACHE.clear()
            adapter.SOURCE_INFLIGHT_LOCKS.clear()

    def test_queue_lock_timeout_raises_cooldown_without_network_request(self):
        adapter.UPSTREAM_REQUEST_LOCK.acquire()
        try:
            with (
                mock.patch.object(adapter, "JACRED_MAX_QUEUE_WAIT_SECONDS", 0.05),
                mock.patch.object(adapter.urllib.request, "urlopen") as urlopen,
            ):
                with self.assertRaises(adapter.UpstreamCoolingDown) as caught:
                    adapter.query_jacred("Example Show", "", "tv")
                self.assertIn("queue", str(caught.exception).lower())
                urlopen.assert_not_called()
        finally:
            adapter.UPSTREAM_REQUEST_LOCK.release()

    def test_transport_timeout_is_registered_and_propagated_without_server(self):
        with (
            mock.patch.object(adapter, "wait_for_upstream_slot", return_value=False),
            mock.patch.object(
                adapter.urllib.request,
                "urlopen",
                side_effect=TimeoutError("simulated upstream timeout"),
            ) as urlopen,
            mock.patch.object(adapter, "register_other_failure", return_value=None) as register,
        ):
            with self.assertRaises(TimeoutError):
                adapter.query_jacred("Example Show", "", "tv")

        urlopen.assert_called_once()
        self.assertTrue(register.call_args.args[0].startswith("TimeoutError: simulated upstream timeout"))

    def test_failed_source_query_is_not_cached_for_the_next_attempt(self):
        with mock.patch.object(adapter, "query_jacred", side_effect=TimeoutError("mock timeout")) as query:
            for _ in range(2):
                with self.assertRaises(TimeoutError):
                    adapter.cached_jacred_results("Example Show", "", "tv")
        self.assertEqual(query.call_count, 2)
        self.assertEqual(adapter.SOURCE_CACHE, {})

    def test_partial_movie_branch_failure_keeps_successful_tv_results_and_does_not_cache_incomplete_feed(self):
        tv_item = {
            "Title": "Example Show S01E01 WEB-DL 1080p",
            "Details": "https://example.invalid/example-show",
            "MagnetUri": "magnet:?xt=urn:btih:" + ("a" * 40),
            "PublishDate": "2026-10-24T00:00:00Z",
            "Category": [5000],
            "Size": 123456,
            "Seeders": 1,
            "Peers": 0,
            "info": {
                "name": "Example Show",
                "originalname": "Example Show",
                "relased": 2026,
                "quality": 1080,
                "types": ["serial"],
                "voices": [],
            },
        }

        def mock_source(query, year, media_type):
            if media_type == "movie":
                raise TimeoutError("mock movie branch timeout")
            return [tv_item], False

        handler = object.__new__(adapter.Handler)
        handler.path = "/torznab/api?t=search&q=Example+Show"
        handler.wfile = io.BytesIO()
        sent_xml = []
        handler.send_xml = lambda xml, *args: sent_xml.append(xml)
        handler.send_response = mock.Mock()
        handler.send_header = mock.Mock()
        handler.end_headers = mock.Mock()

        with (
            mock.patch.object(adapter, "cached_jacred_results", side_effect=mock_source),
            mock.patch.object(adapter, "find_russian_tv_alias", return_value=(None, 0, 0)),
            mock.patch.object(adapter, "notify_upstream_problem") as notify,
        ):
            handler.do_GET()

        self.assertEqual(len(sent_xml), 1)
        xml = sent_xml[0]
        self.assertIn(b"Example Show", xml)
        handler.send_response.assert_not_called()
        notify.assert_called_once()
        self.assertEqual(notify.call_args.args[1], "movie")
        request_key = adapter.cache_key({"t": ["search"], "q": ["Example Show"]})
        self.assertNotIn(request_key, adapter.CACHE)


if __name__ == "__main__":
    unittest.main()
