from dataclasses import FrozenInstanceError
from unittest import TestCase
from unittest.mock import Mock, call, patch

from dahua_rpc.cameras import CameraService
from dahua_rpc.client import DahuaClient
from dahua_rpc.exceptions import InvalidResponseError
from dahua_rpc.models import Camera, StreamProfile


class CameraServiceTests(TestCase):
    def setUp(self) -> None:
        self.rpc = Mock()
        self.service = CameraService(self.rpc)

    def test_list_combines_rpc_inventory_titles_and_states(self) -> None:
        self.rpc.call.side_effect = _list_responses()
        self.assertEqual(
            self.service.list(),
            (
                Camera(
                    channel=1,
                    name="Front Door",
                    configured=True,
                    connected=True,
                    address="10.0.0.21",
                    device_type="IPC-HDW3849H-AS-PV",
                    serial_number="8J0123456789",
                    mac_address="aa:bb:cc:dd:ee:01",
                    protocol="Private",
                ),
                Camera(
                    channel=11,
                    name="Camera 11",
                    configured=False,
                    connected=False,
                    address=None,
                    device_type=None,
                    serial_number=None,
                    mac_address=None,
                    protocol=None,
                ),
            ),
        )
        self.assertEqual(
            self.rpc.call.call_args_list,
            [
                call("LogicDeviceManager.getCameraAll"),
                call("configManager.getConfig", {"name": "ChannelTitle"}),
                call(
                    "LogicDeviceManager.getCameraState",
                    {"uniqueChannels": [-1]},
                ),
            ],
        )

    def test_state_connected_non_connected_missing_and_absent(self) -> None:
        inventory = [_camera(0), _camera(1), _camera(2), _camera(3)]
        states = [
            {"channel": 0, "connectionState": "Connected"},
            {"channel": 1, "connectionState": "Disconnected"},
            {"channel": 2},
        ]
        self.rpc.call.side_effect = _list_responses(inventory=inventory, states=states)
        self.assertEqual(
            [camera.connected for camera in self.service.list()],
            [True, False, False, False],
        )

    def test_get_matches_list_and_preserves_validation(self) -> None:
        self.rpc.call.side_effect = _list_responses(second_channel=1)
        self.assertEqual(self.service.get(2).channel, 2)
        self.rpc.reset_mock()
        with self.assertRaisesRegex(ValueError, "at least 1"):
            self.service.get(0)
        self.rpc.call.assert_not_called()

    def test_get_rejects_missing_channel(self) -> None:
        self.rpc.call.side_effect = _list_responses(second_channel=1)
        with self.assertRaisesRegex(InvalidResponseError, "channel 3"):
            self.service.get(3)

    def test_streams_uses_rpc_encode_and_channel_minus_one(self) -> None:
        self.rpc.call.return_value = {"params": {"table": [{}, {}, _encode()]}}
        self.assertEqual(
            self.service.streams(3),
            (
                StreamProfile(
                    kind="main", codec="H.265", width=3840, height=2160,
                    fps=15.0, bitrate=8192, bitrate_control="VBR",
                    audio_enabled=True, audio_codec="G.711A",
                ),
                StreamProfile(
                    kind="sub", codec="H.264", width=704, height=480,
                    fps=29.97, bitrate=512, bitrate_control="CBR",
                    audio_enabled=False, audio_codec="G.711A",
                ),
            ),
        )
        self.rpc.call.assert_called_once_with(
            "configManager.getConfig", {"name": "Encode"}
        )

    def test_snapshot_operation_is_not_exposed(self) -> None:
        self.assertFalse(hasattr(self.service, "snapshot"))

    def test_malformed_rpc_responses_raise_sdk_errors(self) -> None:
        malformed = (
            {},
            {"params": {"camera": "bad"}},
            {"params": {"camera": [{"Type": "Remote"}]}},
        )
        for response in malformed:
            with self.subTest(response=response):
                self.rpc.call.side_effect = [response, *_list_responses()[1:]]
                with self.assertRaises(InvalidResponseError):
                    self.service.list()

    def test_rpc_failures_propagate(self) -> None:
        self.rpc.call.side_effect = InvalidResponseError("RPC rejected")
        with self.assertRaisesRegex(InvalidResponseError, "RPC rejected"):
            self.service.list()

    def test_inventory_does_not_expose_credentials(self) -> None:
        inventory = [_camera(0) | {"UserName": "admin", "Password": "secret"}]
        self.rpc.call.side_effect = _list_responses(inventory=inventory)
        camera = self.service.list()[0]
        self.assertNotIn("username", camera.__slots__)
        self.assertNotIn("password", camera.__slots__)
        self.assertNotIn("secret", repr(camera))

    def test_models_are_immutable(self) -> None:
        self.rpc.call.side_effect = _list_responses()
        camera = self.service.list()[0]
        with self.assertRaises(FrozenInstanceError):
            camera.name = "Changed"


class DahuaClientCameraServiceTests(TestCase):
    @patch("dahua_rpc.client._RpcConnection")
    def test_client_gives_camera_service_rpc_connection(self, rpc_type: Mock) -> None:
        rpc_type.return_value.call.side_effect = [
            {"params": {"updateSerial": "NVR"}},
            {"params": {"version": {}}},
            {"params": {}},
            {
                "params": {
                    "table": {"TimeZoneDesc": "America/Los_Angeles"}
                }
            },
        ]
        client = DahuaClient(host="recorder.example", username="admin", password="x")
        self.assertIs(client.cameras._connection, rpc_type.return_value)


def _camera(channel: int, *, configured: bool = True) -> dict:
    return {
        "Enable": configured,
        "Type": "Remote",
        "UniqueChannel": channel,
        "DeviceInfo": {
            "Address": "10.0.0.21",
            "DeviceType": "IPC-HDW3849H-AS-PV",
            "SerialNo": "8J0123456789",
            "Mac": "aa:bb:cc:dd:ee:01",
            "ProtocolType": "Private",
        },
    }


def _list_responses(*, inventory=None, states=None, second_channel=10) -> list[dict]:
    inventory = inventory or [
        _camera(0),
        _camera(second_channel, configured=False),
        {"Enable": True, "Type": "Compose", "UniqueChannel": 49},
    ]
    title_count = (
        max((item.get("UniqueChannel", 0) for item in inventory), default=0) + 1
    )
    titles = [{"Name": f"Camera {index + 1}"} for index in range(title_count)]
    titles[0] = {"Name": "Front Door"}
    states = states if states is not None else [
        {"channel": 0, "connectionState": "Connected"},
        {"channel": second_channel},
    ]
    return [
        {"params": {"camera": inventory}},
        {"params": {"table": titles}},
        {"params": {"states": states}},
    ]


def _encode() -> dict:
    return {
        "MainFormat": [_profile("H.265", 3840, 2160, 15, 8192, "VBR", True)],
        "ExtraFormat": [_profile("H.264", 704, 480, 29.97, 512, "CBR", False)],
    }


def _profile(codec, width, height, fps, bitrate, control, audio) -> dict:
    return {
        "Audio": {"Compression": "G.711A"},
        "AudioEnable": audio,
        "Video": {
            "Compression": codec, "Width": width, "Height": height,
            "FPS": fps, "BitRate": bitrate, "BitRateControl": control,
        },
    }
