import importlib.util
import io
import json
from pathlib import Path
import unittest
from unittest.mock import patch
from urllib.error import HTTPError, URLError

spec = importlib.util.spec_from_file_location("bridge", Path(__file__).parents[1] / "src" / "bridge.py")
bridge = importlib.util.module_from_spec(spec)
spec.loader.exec_module(bridge)


class BridgeTests(unittest.TestCase):
    def test_button_wrapper_and_mapping(self):
        event = bridge.normalize_message({"type":"event", "payload":{"event_type":"button", "key":"button_2", "value":True}}, {"button_2":"bathroom"})
        self.assertEqual(event["station_id"], "bathroom")
        self.assertEqual(event["type"], "button_pressed")
        self.assertTrue(event["timestamp"].endswith("Z"))

    def test_releases_and_telemetry_are_ignored(self):
        for value in (False, 0, 1, "pressed", None):
            self.assertIsNone(bridge.normalize_message({"event_type":"button", "key":"kitchen", "value":value}))
        self.assertIsNone(bridge.normalize_message({"event_type":"sensor", "key":"distance", "value":100}))

    def test_unknown_station_rejected(self):
        with self.assertRaisesRegex(ValueError, "Unknown firmware key"):
            bridge.normalize_message({"event_type":"button", "key":"mystery", "value":True})

    def test_stock_event(self):
        event = bridge.normalize_message({"event_type":"stock_low", "key":"bathroom", "value":{"item_id":"toilet-paper", "level":15}})
        self.assertEqual(event["payload"]["level"], 15)
        with self.assertRaises(ValueError):
            bridge.make_event("bathroom", "stock_low", {"level":-1})

    def test_canonical_identity_preserved(self):
        original = bridge.make_event("kitchen", "speech_transcript", {"text":"We need coffee"})
        self.assertEqual(bridge.normalize_message(original), original)

    def test_invalid_speech_and_levels(self):
        with self.assertRaises(ValueError):
            bridge.make_event("kitchen", "speech_transcript", {})
        for level in (float('nan'), float('inf'), True, -1, 101):
            with self.assertRaises(ValueError):
                bridge.make_event("bathroom", "stock_low", {"item_id":"soap", "level":level})

    def test_network_retry_keeps_exact_event_body(self):
        event = bridge.make_event("kitchen", "button_pressed")
        response = io.BytesIO(json.dumps({"request":{"id":"request-1"}}).encode())
        with patch.object(bridge, "urlopen", side_effect=[URLError("temporary"), response]) as send, patch.object(bridge.time, "sleep"):
            bridge.post_event("http://localhost:3001", event)
        self.assertEqual(send.call_count, 2)
        self.assertEqual(send.call_args_list[0].args[0].data, send.call_args_list[1].args[0].data)

    def test_validation_http_error_is_not_retried(self):
        error = HTTPError("http://localhost", 400, "Bad request", {}, io.BytesIO(b'{"error":"invalid"}'))
        with patch.object(bridge, "urlopen", side_effect=error) as send:
            with self.assertRaisesRegex(RuntimeError, "HTTP 400"):
                bridge.post_event("http://localhost:3001", bridge.make_event("kitchen", "button_pressed"))
        self.assertEqual(send.call_count, 1)

    def test_poll_sends_changed_state_and_suppresses_duplicates(self):
        from unittest.mock import Mock
        port = Mock()
        cache = {}
        command = {"station_id":"kitchen", "text":"Awaiting approval", "color":"amber"}
        updated = {"station_id":"kitchen", "text":"Simulated order confirmed", "color":"green"}
        responses = [io.BytesIO(json.dumps({"command": c}).encode()) for c in (command, command, updated, None)]
        with patch.object(bridge, "urlopen", side_effect=responses) as get:
            for _ in range(4):
                bridge.poll_feedback("http://localhost:3001", ["kitchen", "kitchen"], port, cache)
        self.assertEqual(get.call_count, 4)
        self.assertEqual(port.write.call_count, 2)
        self.assertEqual(cache["kitchen"], updated)
        self.assertEqual(json.loads(port.write.call_args.args[0])["value"], updated)

    def test_poll_rejects_cross_station_command(self):
        from unittest.mock import Mock
        port = Mock()
        response = io.BytesIO(json.dumps({"command":{"station_id":"printer", "text":"Wrong", "color":"green"}}).encode())
        with patch.object(bridge, "urlopen", return_value=response), patch('sys.stderr', io.StringIO()):
            bridge.poll_feedback("http://localhost:3001", ["kitchen"], port, {})
        port.write.assert_not_called()

    def test_feedback_is_custom_action(self):
        command = {"station_id":"kitchen", "text":"Awaiting input", "color":"blue"}
        self.assertEqual(bridge.feedback_action({"command":command}), {"action":"office_feedback", "value":command})


if __name__ == '__main__':
    unittest.main()
