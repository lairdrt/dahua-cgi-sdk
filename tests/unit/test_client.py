from datetime import datetime
from unittest import TestCase
from unittest.mock import Mock, call, patch
from zoneinfo import ZoneInfo

from dahua_rpc.client import DahuaClient
from dahua_rpc.exceptions import InvalidResponseError


class DahuaClientRpcIntegrationTests(TestCase):
    @patch("dahua_rpc.client._RpcConnection")
    def test_client_owns_lazy_rpc_connection_and_closes_it(
        self, rpc_type: Mock
    ) -> None:
        rpc = rpc_type.return_value
        rpc.call.side_effect = _identity_responses()

        client = DahuaClient(
            host="recorder.example",
            username="admin",
            password="password",
            port=8080,
            timeout=5.0,
        )

        rpc_type.assert_called_once_with(
            host="recorder.example",
            port=8080,
            username="admin",
            password="password",
            timeout=5.0,
            use_ssl=False,
        )
        self.assertEqual(
            rpc.call.call_args_list,
            [
                call("magicBox.getSystemInfo"),
                call("magicBox.getSoftwareVersion"),
                call("magicBox.getHardwareVersion"),
                call("configManager.getConfig", {"name": "NTP"}),
            ],
        )
        self.assertEqual(client.model, "DHI-NVR5216-16P-4KS2E")
        self.assertEqual(client.serial_number, "serial")
        self.assertEqual(client.hardware_revision, "hardware")
        self.assertEqual(client.firmware_version, "software")
        self.assertEqual(client.api_version, "web")
        self.assertEqual(client.processor, "processor")
        self.assertIsNone(client.manufacturer)
        self.assertEqual(client.timezone, ZoneInfo("America/Los_Angeles"))
        self.assertIs(client.media._connection, rpc)
        client.close()

        rpc_type.return_value.close.assert_called_once_with()

    @patch("dahua_rpc.client._RpcConnection")
    def test_close_stops_rpc_keepalive_before_resources_and_logout(
        self, rpc_type: Mock
    ) -> None:
        rpc = rpc_type.return_value
        rpc.call.side_effect = _identity_responses()
        events: list[str] = []
        rpc.stop_keepalive.side_effect = lambda: events.append("stop_keepalive")
        rpc.close.side_effect = lambda: events.append("rpc_close")
        client = DahuaClient(
            host="recorder.example", username="admin", password="password"
        )
        playback = Mock()
        playback.close.side_effect = lambda: events.append("playback_close")
        client._playbacks.add(playback)

        client.close()

        self.assertEqual(
            events, ["stop_keepalive", "playback_close", "rpc_close"]
        )

    @patch("dahua_rpc.client._RpcConnection")
    def test_current_time_is_recorder_timezone_aware(self, rpc_type: Mock) -> None:
        rpc = rpc_type.return_value
        rpc.call.side_effect = _identity_responses() + [
            {"params": {"time": "2026-08-16 13:04:01"}}
        ]
        client = DahuaClient(
            host="recorder.example", username="admin", password="password"
        )

        self.assertEqual(
            client.current_time,
            datetime(2026, 8, 16, 13, 4, 1, tzinfo=client.timezone),
        )
        rpc.call.assert_called_with("global.getCurrentTime")

    @patch("dahua_rpc.client._RpcConnection")
    def test_malformed_rpc_identity_is_rejected(
        self, rpc_type: Mock
    ) -> None:
        rpc_type.return_value.call.return_value = {"params": []}
        with self.assertRaisesRegex(InvalidResponseError, "valid params"):
            DahuaClient(host="recorder.example", username="admin", password="x")

    @patch("dahua_rpc.client._RpcConnection")
    def test_missing_model_is_rejected_without_cgi_fallback(
        self, rpc_type: Mock
    ) -> None:
        responses = _identity_responses()
        responses[0] = {"params": {"serialNumber": "serial"}}
        rpc_type.return_value.call.side_effect = responses
        with self.assertRaisesRegex(InvalidResponseError, "determine recorder model"):
            DahuaClient(host="recorder.example", username="admin", password="x")

    @patch("dahua_rpc.client._RtspConnection")
    @patch("dahua_rpc.client._RpcConnection")
    def test_client_owns_and_closes_created_playbacks(
        self, rpc_type: Mock, rtsp_type: Mock
    ) -> None:
        rpc_type.return_value.call.side_effect = _identity_responses()
        client = DahuaClient(
            host="recorder.example",
            username="admin",
            password="password",
            timeout=5.0,
        )
        recording = Mock(file_path="/mnt/dvr/recording.dav")

        playback = client.media.playback(recording)

        rtsp_type.assert_called_once_with(
            host="recorder.example",
            port=554,
            username="admin",
            password="password",
            timeout=5.0,
            file_path="/mnt/dvr/recording.dav",
        )
        self.assertIn(playback, client._playbacks)
        client.close()
        rtsp_type.return_value.close_socket.assert_called_once_with()
        self.assertNotIn(playback, client._playbacks)

    @patch("dahua_rpc.client._RtspConnection")
    @patch("dahua_rpc.client._RpcConnection")
    def test_recorded_audio_is_explicitly_opted_in(
        self, rpc_type: Mock, rtsp_type: Mock
    ) -> None:
        rpc_type.return_value.call.side_effect = _identity_responses()
        client = DahuaClient(
            host="recorder.example", username="admin", password="password"
        )
        recording = Mock(file_path="/mnt/dvr/recording.dav")

        client.media.playback(recording, audio=True)

        self.assertTrue(rtsp_type.call_args.kwargs["include_audio"])

    @patch("dahua_rpc.client._RtspConnection")
    @patch("dahua_rpc.client._RpcConnection")
    def test_client_owns_and_closes_created_live_streams(
        self, rpc_type: Mock, rtsp_type: Mock
    ) -> None:
        rpc_type.return_value.call.side_effect = _identity_responses()
        client = DahuaClient(
            host="recorder.example",
            username="admin",
            password="password",
            timeout=5.0,
        )
        profile = Mock()

        live_stream = client._create_live_stream(1, "Main", 0, profile)

        rtsp_type.assert_called_once_with(
            host="recorder.example",
            port=554,
            username="admin",
            password="password",
            timeout=5.0,
            target_path="/cam/realmonitor?channel=1&subtype=0",
            initial_range=None,
        )
        self.assertIn(live_stream, client._live_streams)
        client.close()
        rtsp_type.return_value.close_socket.assert_called_once_with()
        self.assertNotIn(live_stream, client._live_streams)


def _identity_responses() -> list[dict]:
    return [
        {
            "params": {
                "processor": "processor",
                "serialNumber": "serial",
                "updateSerial": "DHI-NVR5216-16P-4KS2E",
            }
        },
        {"params": {"version": {"Version": "software", "WebVersion": "web"}}},
        {"params": {"version": "hardware"}},
        {
            "params": {
                "table": {
                    "TimeZone": 28,
                    "TimeZoneDesc": "America/Los_Angeles",
                }
            }
        },
    ]
