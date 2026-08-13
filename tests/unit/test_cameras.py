from dataclasses import FrozenInstanceError
from unittest import TestCase
from unittest.mock import Mock, patch

from dahua_cgi.cameras import CameraService
from dahua_cgi.client import DahuaClient
from dahua_cgi.exceptions import InvalidResponseError
from dahua_cgi.models import Camera, StreamProfile


class CameraServiceTests(TestCase):
    def setUp(self) -> None:
        self.connection = Mock()
        self.service = CameraService(self.connection)

    def test_list_returns_all_exposed_slots_as_1_based_channels(self) -> None:
        self.connection.get.return_value = _response(
            "\n".join(
                [
                    "table.ChannelTitle[0].Name=Front Door",
                    "table.ChannelTitle[3].Name=Driveway",
                ]
            )
        )

        self.assertEqual(
            self.service.list(),
            (
                Camera(channel=1, name="Front Door"),
                Camera(channel=4, name="Driveway"),
            ),
        )
        self.connection.get.assert_called_once_with(
            "/cgi-bin/configManager.cgi",
            params={"action": "getConfig", "name": "ChannelTitle"},
        )

    def test_get_maps_channel_to_exact_config_index(self) -> None:
        self.connection.get.return_value = _response(
            "\n".join(
                [
                    "table.ChannelTitle[0].Name=Front Door",
                    "table.ChannelTitle[1].Name=Back Door",
                ]
            )
        )

        self.assertEqual(
            self.service.get(2),
            Camera(channel=2, name="Back Door"),
        )

    def test_get_does_not_return_adjacent_camera(self) -> None:
        self.connection.get.return_value = _response(
            "table.ChannelTitle[1].Name=Back Door"
        )

        with self.assertRaisesRegex(InvalidResponseError, "channel 1"):
            self.service.get(1)

    def test_streams_parse_main_and_first_substream(self) -> None:
        self.connection.get.return_value = _response(_encode_response(config_index=0))

        self.assertEqual(
            self.service.streams(1),
            (
                StreamProfile(
                    kind="main",
                    codec="H.265",
                    width=3840,
                    height=2160,
                    fps=15.0,
                    bitrate=8192,
                    bitrate_control="VBR",
                    audio_enabled=True,
                    audio_codec="G.711A",
                ),
                StreamProfile(
                    kind="sub",
                    codec="H.264",
                    width=704,
                    height=480,
                    fps=29.97,
                    bitrate=512,
                    bitrate_control="CBR",
                    audio_enabled=False,
                    audio_codec="G.711A",
                ),
            ),
        )
        self.connection.get.assert_called_once_with(
            "/cgi-bin/configManager.cgi",
            params={"action": "getConfig", "name": "Encode"},
        )

    def test_streams_use_channel_minus_one_as_config_index(self) -> None:
        self.connection.get.return_value = _response(_encode_response(config_index=2))

        profiles = self.service.streams(3)

        self.assertEqual(profiles[0].width, 3840)

    def test_snapshot_returns_jpeg_bytes(self) -> None:
        jpeg = b"\xff\xd8camera image\xff\xd9"
        self.connection.get.return_value = _response(
            content=jpeg,
            headers={"Content-Type": "image/jpeg; charset=binary"},
        )

        self.assertEqual(self.service.snapshot(2), jpeg)
        self.connection.get.assert_called_once_with(
            "/cgi-bin/snapshot.cgi",
            params={"channel": 2},
        )

    def test_snapshot_rejects_non_200_response(self) -> None:
        self.connection.get.return_value = _response(status_code=500)

        with self.assertRaisesRegex(InvalidResponseError, "500"):
            self.service.snapshot(1)

    def test_snapshot_rejects_wrong_content_type(self) -> None:
        self.connection.get.return_value = _response(
            content=b"\xff\xd8camera image\xff\xd9",
            headers={"Content-Type": "text/plain"},
        )

        with self.assertRaisesRegex(InvalidResponseError, "content type"):
            self.service.snapshot(1)

    def test_snapshot_rejects_malformed_jpeg(self) -> None:
        self.connection.get.return_value = _response(
            content=b"not a jpeg",
            headers={"Content-Type": "image/jpeg"},
        )

        with self.assertRaisesRegex(InvalidResponseError, "malformed JPEG"):
            self.service.snapshot(1)

    def test_operations_reject_channel_zero_without_requesting(self) -> None:
        operations = (self.service.get, self.service.streams, self.service.snapshot)
        for operation in operations:
            with self.subTest(operation=operation.__name__):
                with self.assertRaisesRegex(ValueError, "at least 1"):
                    operation(0)

        self.connection.get.assert_not_called()

    def test_models_are_immutable(self) -> None:
        camera = Camera(channel=1, name="Front Door")
        profile = StreamProfile(
            kind="main",
            codec="H.265",
            width=3840,
            height=2160,
            fps=15.0,
            bitrate=8192,
            bitrate_control="VBR",
            audio_enabled=True,
            audio_codec="G.711A",
        )

        with self.assertRaises(FrozenInstanceError):
            camera.name = "Changed"
        with self.assertRaises(FrozenInstanceError):
            profile.codec = "H.264"

    def test_config_request_requires_http_200(self) -> None:
        self.connection.get.return_value = _response(status_code=400)

        with self.assertRaisesRegex(InvalidResponseError, "400"):
            self.service.list()


class DahuaClientCameraServiceTests(TestCase):
    @patch("dahua_cgi.client._Connection")
    def test_client_exposes_camera_service(self, connection_type: Mock) -> None:
        connection_type.return_value.get.return_value = _response("deviceType=NVR")

        client = DahuaClient(host="recorder.example", username="admin", password="x")

        self.assertIsInstance(client.cameras, CameraService)


def _response(
    text: str = "",
    *,
    status_code: int = 200,
    content: bytes = b"",
    headers: dict[str, str] | None = None,
) -> Mock:
    return Mock(
        text=text,
        status_code=status_code,
        content=content,
        headers=headers or {},
    )


def _encode_response(*, config_index: int) -> str:
    main = f"table.Encode[{config_index}].MainFormat[0]"
    sub = f"table.Encode[{config_index}].ExtraFormat[0]"
    return "\n".join(
        [
            f"{main}.Audio.Compression=G.711A",
            f"{main}.AudioEnable=true",
            f"{main}.Video.BitRate=8192",
            f"{main}.Video.BitRateControl=VBR",
            f"{main}.Video.Compression=H.265",
            f"{main}.Video.FPS=15",
            f"{main}.Video.Height=2160",
            f"{main}.Video.Width=3840",
            f"{sub}.Audio.Compression=G.711A",
            f"{sub}.AudioEnable=false",
            f"{sub}.Video.BitRate=512",
            f"{sub}.Video.BitRateControl=CBR",
            f"{sub}.Video.Compression=H.264",
            f"{sub}.Video.FPS=29.97",
            f"{sub}.Video.Height=480",
            f"{sub}.Video.Width=704",
        ]
    )
