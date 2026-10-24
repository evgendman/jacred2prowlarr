import json
import tempfile
import unittest
from unittest import mock

import jacred_v2_adapter as adapter


class NotificationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.old_state_path = adapter.JACRED_STATE_PATH
        adapter.JACRED_STATE_PATH = self.temp.name + "/state.sqlite3"
        self.addCleanup(self._restore_state)
        adapter.reset_protection_state()
        with adapter.NOTIFICATION_STATE_LOCK:
            adapter.NOTIFICATION_STATE_MEMORY.clear()

    def _restore_state(self):
        adapter.JACRED_STATE_PATH = self.old_state_path
        self.temp.cleanup()
        with adapter.NOTIFICATION_STATE_LOCK:
            adapter.NOTIFICATION_STATE_MEMORY.clear()

    @staticmethod
    def successful_response():
        response = mock.MagicMock()
        response.__enter__.return_value = response
        response.getcode.return_value = 200
        response.read.return_value = b"{}"
        return response

    def test_apprise_payload_uses_shared_gateway_key_and_warning_tag(self):
        with (
            mock.patch.object(adapter, "JACRED_NOTIFICATIONS_ENABLED", True),
            mock.patch.object(adapter, "JACRED_APPRISE_URL", "http://192.168.0.14:8000"),
            mock.patch.object(adapter, "JACRED_APPRISE_CONFIG_KEY", "arr-stack"),
            mock.patch.object(adapter.urllib.request, "urlopen", return_value=self.successful_response()) as urlopen,
        ):
            sent = adapter.send_notification_once(
                "test:warning",
                "warning-state-1",
                "Test warning",
                "Test body",
                "warnings",
                "warning",
            )

        self.assertTrue(sent)
        request = urlopen.call_args.args[0]
        self.assertEqual(request.full_url, "http://192.168.0.14:8000/notify/arr-stack")
        self.assertEqual(request.get_method(), "POST")
        payload = json.loads(request.data.decode("utf-8"))
        self.assertEqual(
            payload,
            {
                "title": "JacRed TV + Movies adapter — Test warning",
                "body": "Test body",
                "type": "warning",
                "tag": "warnings",
            },
        )

    def test_unchanged_event_is_deduplicated_even_after_process_memory_is_cleared(self):
        with (
            mock.patch.object(adapter, "JACRED_NOTIFICATIONS_ENABLED", True),
            mock.patch.object(adapter.urllib.request, "urlopen", return_value=self.successful_response()) as urlopen,
        ):
            self.assertTrue(
                adapter.send_notification_once(
                    "upstream:queue", "queue-wait-exceeded", "Queue busy", "Body", "warnings", "warning"
                )
            )
            with adapter.NOTIFICATION_STATE_LOCK:
                adapter.NOTIFICATION_STATE_MEMORY.clear()
            self.assertFalse(
                adapter.send_notification_once(
                    "upstream:queue", "queue-wait-exceeded", "Queue busy", "Body", "warnings", "warning"
                )
            )
        urlopen.assert_called_once()

    def test_apprise_failure_is_best_effort_and_does_not_repeat_unchanged_alert(self):
        with (
            mock.patch.object(adapter, "JACRED_NOTIFICATIONS_ENABLED", True),
            mock.patch.object(
                adapter.urllib.request, "urlopen", side_effect=TimeoutError("mock gateway timeout")
            ) as urlopen,
        ):
            self.assertFalse(
                adapter.send_notification_once(
                    "upstream:transport", "transport-timeout", "Timeout", "Body", "warnings", "warning"
                )
            )
            self.assertFalse(
                adapter.send_notification_once(
                    "upstream:transport", "transport-timeout", "Timeout", "Body", "warnings", "warning"
                )
            )
        urlopen.assert_called_once()

    def test_disabled_notifications_do_not_contact_apprise(self):
        with (
            mock.patch.object(adapter, "JACRED_NOTIFICATIONS_ENABLED", False),
            mock.patch.object(adapter.urllib.request, "urlopen") as urlopen,
        ):
            self.assertFalse(
                adapter.send_notification_once(
                    "test:disabled", "x", "Disabled", "Body", "warnings", "warning"
                )
            )
        urlopen.assert_not_called()

    def test_persisted_manual_review_after_http_403_keeps_critical_403_classification(self):
        state = {
            "manual_review": 1,
            "state": "OPEN",
            "failure_reason": "HTTP 403; manual review required",
            "block_until": 0,
        }
        response = self.successful_response()
        with (
            mock.patch.object(adapter, "JACRED_NOTIFICATIONS_ENABLED", True),
            mock.patch.object(adapter, "read_protection_state", return_value=state),
            mock.patch.object(adapter.urllib.request, "urlopen", return_value=response) as urlopen,
        ):
            adapter.notify_persisted_protection_state()

        self.assertEqual(urlopen.call_count, 1)
        payload = json.loads(urlopen.call_args.args[0].data.decode("utf-8"))
        self.assertEqual(payload["tag"], "critical")
        self.assertEqual(payload["type"], "failure")
        self.assertIn("HTTP 403", payload["body"])

    def test_recoverable_alerts_are_scoped_to_the_affected_media_branch(self):
        with mock.patch.object(adapter, "send_notification_once") as send:
            adapter.notify_upstream_problem(TimeoutError("simulated timeout"), "movie")
            self.assertEqual(
                send.call_args.args[0],
                "upstream:branch:movie:transport",
            )
            send.reset_mock()
            adapter.notify_upstream_problem(TimeoutError("simulated timeout"), "tv")
            self.assertEqual(
                send.call_args.args[0],
                "upstream:branch:tv:transport",
            )

    def test_upstream_403_is_critical_and_429_is_a_warning(self):
        with mock.patch.object(adapter, "send_notification_once") as send:
            adapter.notify_upstream_problem(
                adapter.UpstreamCoolingDown(86400, "JacRed HTTP 403; upstream circuit OPEN for at least 24 hours"),
                "tv",
            )
            self.assertEqual(send.call_args.args[0], "upstream:global:access-block")
            self.assertEqual(send.call_args.args[4], "critical")
            self.assertEqual(send.call_args.args[5], "failure")

            send.reset_mock()
            adapter.notify_upstream_problem(
                adapter.UpstreamCoolingDown(300, "JacRed HTTP 429 (stage 2); all upstream requests paused"),
                "movie",
            )
            self.assertEqual(send.call_args.args[0], "upstream:global:rate-limit")
            self.assertEqual(send.call_args.args[4], "warnings")
            self.assertEqual(send.call_args.args[5], "warning")


if __name__ == "__main__":
    unittest.main()
