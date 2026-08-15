import socket
from unittest import TestCase
from unittest.mock import Mock, patch

from dahua_cgi._rtsp_connection import _MediaReceipt, _RtspConnection
from dahua_cgi.cameras import CameraService
from dahua_cgi.exceptions import InvalidResponseError, PlaybackStateError
from dahua_cgi.live import LiveStream
from dahua_cgi.models import Camera, StreamProfile
from dahua_cgi.playback import RtpReceipt


class CameraLiveStreamSelectionTests(TestCase):
    def setUp(self) -> None:
        self.factory = Mock(return_value=Mock())
        self.service = CameraService(Mock(), live_stream_factory=self.factory)
        self.camera = Camera(
            channel=1,
            name="Front Door",
            configured=True,
            connected=True,
            address="10.0.0.21",
            device_type="IPC",
            serial_number="serial",
            mac_address="aa:bb:cc:dd:ee:ff",
            protocol="Private",
        )
        self.main = _profile("main", "H.265")
        self.extra = _profile("sub", "H.264")

    def test_main_and_extra1_map_to_proven_subtypes(self) -> None:
        with (
            patch.object(self.service, "get", return_value=self.camera),
            patch.object(self.service, "streams", return_value=(self.main, self.extra)),
        ):
            self.service.live_stream(channel=1, stream="Main")
            self.factory.assert_called_with(1, "Main", 0, self.main)
            self.service.live_stream(channel=1, stream="Extra1")
            self.factory.assert_called_with(1, "Extra1", 1, self.extra)

    def test_invalid_channel_stream_and_unavailable_profile_are_rejected(self) -> None:
        with self.assertRaises(ValueError):
            self.service.live_stream(channel=0)
        with self.assertRaises(ValueError):
            self.service.live_stream(channel=1, stream="Extra2")  # type: ignore[arg-type]
        with (
            patch.object(self.service, "get", return_value=self.camera),
            patch.object(self.service, "streams", return_value=(self.main,)),
        ):
            with self.assertRaisesRegex(InvalidResponseError, "Extra1"):
                self.service.live_stream(channel=1, stream="Extra1")
        self.factory.assert_not_called()

    def test_unconfigured_camera_is_rejected_before_encode_lookup(self) -> None:
        camera = Camera(
            channel=1,
            name="Camera 1",
            configured=False,
            connected=False,
            address=None,
            device_type=None,
            serial_number=None,
            mac_address=None,
            protocol=None,
        )
        with (
            patch.object(self.service, "get", return_value=camera),
            patch.object(self.service, "streams") as streams,
        ):
            with self.assertRaisesRegex(InvalidResponseError, "not configured"):
                self.service.live_stream(channel=1)
        streams.assert_not_called()


class LiveRtspLifecycleTests(TestCase):
    def test_live_target_shared_protocol_and_media_lifecycle(self) -> None:
        scripted = _ScriptedSocket(_live_replies())
        connector = Mock(return_value=scripted)
        connection = _RtspConnection(
            host="recorder.example",
            port=554,
            username="admin",
            password="secret",
            timeout=2.0,
            target_path="/cam/realmonitor?channel=1&subtype=0",
            initial_range=None,
            connector=connector,
        )
        stream = LiveStream(
            connection,
            channel=1,
            stream="Main",
            profile=_profile("main", "H.265"),
        )

        with stream:
            stream.start()
            receipt = stream.receive(0.01)

        requests = [request.decode("ascii") for request in scripted.sent]
        target = "rtsp://recorder.example:554/cam/realmonitor?channel=1&subtype=0"
        self.assertEqual(stream.target, target)
        self.assertNotIn("admin", target)
        self.assertNotIn("secret", target)
        self.assertTrue(requests[0].startswith(f"DESCRIBE {target} RTSP/1.0"))
        self.assertNotIn("Authorization:", requests[0])
        self.assertIn("Authorization: Digest ", requests[1])
        self.assertIn("SETUP rtsp://recorder.example:554/live/trackID=0", requests[2])
        self.assertIn("Transport: RTP/AVP/TCP;unicast;interleaved=0-1", requests[2])
        self.assertTrue(requests[3].startswith(f"PLAY {target}"))
        self.assertNotIn("Range:", requests[3])
        self.assertTrue(requests[4].startswith(f"TEARDOWN {target}"))
        self.assertEqual(stream.video_codec, "H265")
        self.assertEqual(stream.video_control, "trackID=0")
        self.assertEqual(receipt, RtpReceipt(2, 32, 100, 101))
        self.assertEqual(stream.packets_received, 2)
        self.assertEqual(stream.bytes_received, 32)
        self.assertTrue(scripted.closed)
        connector.assert_called_once_with(("recorder.example", 554), timeout=2.0)


class LiveStreamStateTests(TestCase):
    def setUp(self) -> None:
        self.connection = Mock()
        self.connection.video_codec = "H265"
        self.connection.video_control = "trackID=0"
        self.connection.target = "rtsp://recorder/live"
        self.connection.receive.return_value = _MediaReceipt(2, 100, 10, 20)
        self.closed = Mock()
        self.stream = LiveStream(
            self.connection,
            channel=1,
            stream="Main",
            profile=_profile("main", "H.265"),
            on_close=self.closed,
        )

    def test_state_and_idempotent_cleanup(self) -> None:
        with self.assertRaises(PlaybackStateError):
            self.stream.receive()
        self.stream.start()
        with self.assertRaises(PlaybackStateError):
            self.stream.start()
        self.assertEqual(self.stream.receive(), RtpReceipt(2, 100, 10, 20))
        self.stream.close()
        self.stream.close()
        self.connection.teardown.assert_called_once_with()
        self.connection.close_socket.assert_called_once_with()
        self.closed.assert_called_once_with(self.stream)
        with self.assertRaises(PlaybackStateError):
            self.stream.receive()

    def test_context_cleanup_does_not_mask_body_error(self) -> None:
        self.connection.teardown.side_effect = InvalidResponseError("failed")
        with self.assertRaisesRegex(RuntimeError, "body"):
            with self.stream as stream:
                stream.start()
                raise RuntimeError("body")
        self.connection.close_socket.assert_called_once_with()


class _ScriptedSocket:
    def __init__(self, replies: list[bytes]) -> None:
        self.replies = list(replies)
        self.incoming = bytearray()
        self.sent: list[bytes] = []
        self.closed = False

    def sendall(self, request: bytes) -> None:
        self.sent.append(request)
        if self.replies:
            self.incoming.extend(self.replies.pop(0))

    def recv(self, count: int) -> bytes:
        if not self.incoming:
            raise socket.timeout
        chunk = bytes(self.incoming[:count])
        del self.incoming[:count]
        return chunk

    def settimeout(self, timeout: float) -> None:
        pass

    def close(self) -> None:
        self.closed = True


def _live_replies() -> list[bytes]:
    sdp = (
        b"v=0\r\n"
        b"a=range:npt=now-\r\n"
        b"m=video 0 RTP/AVP 98\r\n"
        b"a=control:trackID=0\r\n"
        b"a=rtpmap:98 H265/90000\r\n"
    )
    return [
        _response(
            401,
            headers=(('WWW-Authenticate', 'Digest realm="r", nonce="n"'),),
        ),
        _response(
            200,
            headers=(("Content-Base", "rtsp://recorder.example:554/live/"),),
            body=sdp,
        ),
        _response(
            200,
            headers=(
                ("Session", "session-id;timeout=60"),
                ("Transport", "RTP/AVP/TCP;unicast;interleaved=0-1"),
            ),
        ),
        _response(200) + _frame(100) + _frame(101),
        _response(200),
    ]


def _response(
    status: int,
    *,
    headers: tuple[tuple[str, str], ...] = (),
    body: bytes = b"",
) -> bytes:
    reason = "OK" if status == 200 else "Unauthorized"
    lines = [f"RTSP/1.0 {status} {reason}", f"Content-Length: {len(body)}"]
    lines.extend(f"{name}: {value}" for name, value in headers)
    return ("\r\n".join(lines) + "\r\n\r\n").encode() + body


def _frame(timestamp: int) -> bytes:
    payload = b"\x80\x62\x00\x01" + timestamp.to_bytes(4, "big") + b"\x00" * 8
    return b"$\x00" + len(payload).to_bytes(2, "big") + payload


def _profile(kind: str, codec: str) -> StreamProfile:
    return StreamProfile(
        kind=kind,  # type: ignore[arg-type]
        codec=codec,
        width=3840,
        height=2160,
        fps=15.0,
        bitrate=8192,
        bitrate_control="VBR",
        audio_enabled=True,
        audio_codec="G.711A",
    )
