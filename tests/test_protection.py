import email.utils
import json
import os
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest import mock

import jacred_v2_adapter as adapter


class MockUpstreamHandler(BaseHTTPRequestHandler):
    requests = []
    status = 200
    body = b'{"Results": []}'
    retry_after = None
    active = 0
    max_active = 0
    lock = threading.Lock()

    def log_message(self, fmt, *args):
        pass

    def do_GET(self):
        with self.lock:
            type(self).active += 1
            type(self).max_active = max(type(self).max_active, type(self).active)
            type(self).requests.append((self.path, self.headers.get("User-Agent"), time.monotonic()))
        try:
            time.sleep(0.02)
            self.send_response(type(self).status)
            if type(self).retry_after:
                self.send_header("Retry-After", type(self).retry_after)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(type(self).body)
        finally:
            with self.lock:
                type(self).active -= 1


class ProtectionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.state_path = adapter.JACRED_STATE_PATH
        self.url = adapter.JACRED_URL
        self.interval = adapter.JACRED_MIN_INTERVAL_MS
        self.ua = adapter.JACRED_USER_AGENT
        adapter.JACRED_STATE_PATH = os.path.join(self.temp.name, "state.sqlite3")
        adapter.JACRED_URL = "http://127.0.0.1:9/api/v2.0/indexers/all/results"
        adapter.JACRED_MIN_INTERVAL_MS = 5000
        adapter.JACRED_USER_AGENT = "Mock-Adapter-Test"
        adapter.LAST_UPSTREAM_COMPLETION = 0
        adapter.reset_protection_state()
        MockUpstreamHandler.requests = []
        MockUpstreamHandler.status = 200
        MockUpstreamHandler.body = b'{"Results": []}'
        MockUpstreamHandler.retry_after = None
        MockUpstreamHandler.active = 0
        MockUpstreamHandler.max_active = 0
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), MockUpstreamHandler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        adapter.JACRED_URL = f"http://127.0.0.1:{self.server.server_port}/api/v2.0/indexers/all/results"

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=1)
        adapter.JACRED_STATE_PATH = self.state_path
        adapter.JACRED_URL = self.url
        adapter.JACRED_MIN_INTERVAL_MS = self.interval
        adapter.JACRED_USER_AGENT = self.ua
        self.temp.cleanup()

    def test_local_default_and_interval_floor_are_configured(self):
        self.assertGreaterEqual(adapter.JACRED_MIN_INTERVAL_MS, 5000)
        self.assertIn("/api/v2.0/indexers/all/results", adapter.JACRED_URL)

    def test_user_agent_and_v2_query_shape(self):
        self.assertEqual(adapter.query_jacred("Test title", "2024", "movie"), [])
        path, ua, _ = MockUpstreamHandler.requests[-1]
        query = urllib.parse.parse_qs(urllib.parse.urlsplit(path).query)
        self.assertEqual(query["q"], ["Test title"])
        self.assertEqual(query["category"], ["movie_"])
        self.assertEqual(query["year"], ["2024"])
        self.assertEqual(ua, "Mock-Adapter-Test")

    def test_retry_after_seconds_and_http_date(self):
        class Error:
            def __init__(self, value):
                self.headers = {"Retry-After": value}
        self.assertEqual(adapter.parse_retry_after(Error("120")), 120.0)
        future = email.utils.formatdate(time.time() + 120, usegmt=True)
        self.assertGreater(adapter.parse_retry_after(Error(future)), 100)

    def test_429_escalation_is_persisted(self):
        first = adapter.register_http_429(1)
        state = adapter.read_protection_state()
        self.assertEqual(state["state"], "OPEN")
        self.assertEqual(state["consecutive_429"], 1)
        self.assertGreaterEqual(first.retry_after_seconds, 60)
        second = adapter.register_http_429(1)
        state = adapter.read_protection_state()
        self.assertEqual(state["consecutive_429"], 2)
        self.assertGreaterEqual(second.retry_after_seconds, 300)

    def test_first_403_opens_24h_block(self):
        error = urllib.error.HTTPError(adapter.JACRED_URL, 403, "Forbidden", {"Server": "mock"}, None)
        adapter.register_http_403(error)
        state = adapter.read_protection_state()
        self.assertEqual(state["state"], "OPEN")
        self.assertFalse(state["manual_review"])
        self.assertGreaterEqual(state["block_until"] - time.time(), 86390)

    def test_second_403_requires_manual_review_after_seven_days(self):
        error = urllib.error.HTTPError(adapter.JACRED_URL, 403, "Forbidden", {"Server": "mock"}, None)
        adapter.register_http_403(error)
        adapter.register_http_403(error)
        state = adapter.read_protection_state()
        self.assertEqual(state["consecutive_403"], 2)
        self.assertTrue(state["manual_review"])
        self.assertGreaterEqual(state["block_until"] - time.time(), 604790)

    def test_403_during_recovery_probe_sets_manual_review(self):
        adapter.register_http_403(
            urllib.error.HTTPError(adapter.JACRED_URL, 403, "Forbidden", {}, None)
        )
        adapter.update_protection_state(state="OPEN", block_until=time.time() - 1, manual_review=0)
        probe = adapter.begin_upstream_attempt()
        self.assertTrue(probe)
        adapter.register_http_403(
            urllib.error.HTTPError(adapter.JACRED_URL, 403, "Forbidden", {}, None)
        )
        state = adapter.read_protection_state()
        self.assertTrue(state["manual_review"])
        self.assertEqual(state["consecutive_403"], 2)

    def test_401_requires_manual_review(self):
        error = urllib.error.HTTPError(adapter.JACRED_URL, 401, "Unauthorized", {}, None)
        adapter.register_http_401(error)
        state = adapter.read_protection_state()
        self.assertTrue(state["manual_review"])
        self.assertEqual(state["state"], "OPEN")

    def test_persisted_state_survives_reopen(self):
        adapter.register_http_429(1)
        state_before = adapter.read_protection_state()
        state_after = adapter.read_protection_state()
        self.assertEqual(state_after["consecutive_429"], state_before["consecutive_429"])
        self.assertEqual(state_after["block_until"], state_before["block_until"])

    def test_success_does_not_clear_escalation_after_one_request(self):
        adapter.register_http_429(1)
        # Make the cooldown expired only for this isolated state-machine test.
        adapter.update_protection_state(state="CLOSED", block_until=0, manual_review=0)
        for _ in range(adapter.JACRED_STABLE_SUCCESS_COUNT - 1):
            adapter.register_upstream_success()
        state = adapter.read_protection_state()
        self.assertEqual(state["consecutive_429"], 1)
        adapter.register_upstream_success()
        state = adapter.read_protection_state()
        self.assertEqual(state["consecutive_429"], 0)

    def test_two_simultaneous_queries_are_serialized(self):
        adapter.JACRED_MIN_INTERVAL_MS = 5000
        adapter.LAST_UPSTREAM_COMPLETION = time.monotonic() - 10
        errors = []
        def call(query):
            try:
                adapter.query_jacred(query, "", "movie")
            except Exception as exc:
                errors.append(exc)
        threads = [threading.Thread(target=call, args=(f"q{i}",)) for i in range(2)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=2)
        self.assertEqual(len(MockUpstreamHandler.requests), 2)
        self.assertEqual(MockUpstreamHandler.max_active, 1)
        self.assertGreaterEqual(
            MockUpstreamHandler.requests[1][2] - MockUpstreamHandler.requests[0][2],
            0.015,
        )
        self.assertFalse(errors)


if __name__ == "__main__":
    unittest.main()
