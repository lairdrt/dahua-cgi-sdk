import socket
from datetime import datetime
from unittest import TestCase
from unittest.mock import Mock

from dahua_rpc._rtsp_connection import _MediaReceipt, _RtspConnection
from dahua_rpc.exceptions import (
    AuthenticationError,
    InvalidResponseError,
    PlaybackStateError,
    RecorderConnectionError,
    TransportError,
)
from dahua_rpc.models import EncodedMediaPacket, Recording
from dahua_rpc.playback import RecordingPlayback, RtpReceipt


class RtspLifecycleTests(TestCase):
    def setUp(self) -> None:
        self.socket = _ScriptedSocket(_successful_replies())
        self.connector_calls = []

        def connector(address, *, timeout):
            self.connector_calls.append((address, timeout))
            return self.socket

        self.connection = _RtspConnection(
            host="recorder.example",
            port=554,
            username="admin",
            password="secret",
            timeout=2.0,
            file_path=_recording().file_path,
            connector=connector,
        )
        self.playback = RecordingPlayback(self.connection, _recording())

    def test_complete_lifecycle_uses_exact_file_and_interleaved_media(self) -> None:
        self.playback.start()
        initial = self.playback.receive(0.01)
        self.playback.pause()
        self.playback.seek(25)
        seek = self.playback.receive(0.01)
        self.playback.pause()
        self.playback.resume()
        self.playback.close()

        self.assertEqual(self.connector_calls, [(('recorder.example', 554), 2.0)])
        requests = [item.decode("ascii") for item in self.socket.sent]
        target = (
            "rtsp://recorder.example:554//mnt/dvr/recording.dav"
        )
        self.assertTrue(requests[0].startswith(f"DESCRIBE {target} RTSP/1.0"))
        self.assertNotIn("admin:secret", target)
        self.assertNotIn("Authorization:", requests[0])
        self.assertIn("Authorization: Digest ", requests[1])
        self.assertIn("SETUP rtsp://recorder.example:554/base/trackID=0", requests[2])
        self.assertIn("Transport: RTP/AVP/TCP;unicast;interleaved=0-1", requests[2])
        self.assertIn("PLAY", requests[3])
        self.assertIn("Range: npt=0-", requests[3])
        self.assertIn("PAUSE", requests[4])
        self.assertIn("Range: npt=25-", requests[5])
        self.assertIn("PLAY", requests[7])
        self.assertNotIn("Range:", requests[7])
        self.assertIn("TEARDOWN", requests[8])
        self.assertEqual(self.playback.duration, 75.0)
        self.assertEqual(self.playback.video_codec, "H265")
        self.assertEqual(self.playback.video_control, "trackID=0")
        self.assertEqual(self.playback.returned_range, "npt=25.000000-50.000000")
        self.assertEqual(initial.packets, 2)
        self.assertGreater(initial.bytes, 0)
        self.assertEqual(initial.first_timestamp, 100)
        self.assertEqual(seek.first_timestamp, 200)
        self.assertGreater(self.playback.packets_received, 0)
        self.assertGreater(self.playback.bytes_received, 0)
        self.assertTrue(self.socket.closed)

    def test_context_manager_tears_down_and_close_is_idempotent(self) -> None:
        with self.playback as playback:
            playback.start()
        self.playback.close()
        teardown = [
            request
            for request in self.socket.sent
            if request.startswith(b"TEARDOWN")
        ]
        self.assertEqual(len(teardown), 1)


class RtspErrorTests(TestCase):
    def test_repeated_401_maps_to_authentication_error_on_same_socket(self) -> None:
        challenge = _response(
            401, headers=(('WWW-Authenticate', 'Digest realm="r", nonce="n"'),)
        )
        scripted = _ScriptedSocket([challenge, challenge])
        connection = _connection(scripted)
        with self.assertRaises(AuthenticationError):
            connection.start()
        self.assertEqual(len(scripted.sent), 2)
        self.assertTrue(scripted.closed)

    def test_malformed_response_maps_to_invalid_response(self) -> None:
        scripted = _ScriptedSocket([b"not rtsp\r\n\r\n"])
        with self.assertRaises(InvalidResponseError):
            _connection(scripted).start()

    def test_connection_and_timeout_failures_map_to_sdk_errors(self) -> None:
        for error, expected in (
            (OSError("refused"), RecorderConnectionError),
            (socket.timeout(), TransportError),
        ):
            with self.subTest(error=error):
                connection = _RtspConnection(
                    host="recorder.example",
                    port=554,
                    username="admin",
                    password="secret",
                    timeout=2.0,
                    file_path="/mnt/dvr/recording.dav",
                    connector=Mock(side_effect=error),
                )
                with self.assertRaises(expected):
                    connection.start()


class PlaybackStateTests(TestCase):
    def setUp(self) -> None:
        self.connection = Mock()
        self.connection.duration = 50.0
        self.connection.video_codec = "H265"
        self.connection.video_control = "trackID=0"
        self.connection.returned_range = None
        self.connection.pause.return_value = None
        self.connection.play.return_value = None
        self.connection.receive.return_value = _MediaReceipt(2, 100, 10, 20)
        self.packet = EncodedMediaPacket(
            media_type="video",
            packet_type="rtp",
            interleaved_channel=0,
            arrival_time=1.0,
            data=b"rtp",
        )
        self.connection.receive_packets.return_value = (self.packet,)
        self.closed = Mock()
        self.playback = RecordingPlayback(
            self.connection, _recording(), on_close=self.closed
        )

    def test_state_transitions_and_receipt(self) -> None:
        self.playback.start()
        receipt = self.playback.receive()
        self.assertEqual(receipt, RtpReceipt(2, 100, 10, 20))
        self.playback.pause()
        self.playback.resume()
        self.playback.seek(25)
        self.connection.play.assert_any_call()
        self.connection.play.assert_called_with("npt=25-")

    def test_invalid_transitions_and_seek_values_fail_cleanly(self) -> None:
        for operation in (
            self.playback.pause,
            self.playback.resume,
            lambda: self.playback.seek(1),
            self.playback.receive,
            self.playback.receive_packets,
        ):
            with self.subTest(operation=operation):
                with self.assertRaises(PlaybackStateError):
                    operation()
        with self.assertRaises(ValueError):
            self.playback.seek(-1)
        self.playback.start()
        with self.assertRaises(PlaybackStateError):
            self.playback.start()
        with self.assertRaises(ValueError):
            self.playback.seek(51)

    def test_encoded_packets_remain_available_after_resume_and_seek(self) -> None:
        self.playback.start()
        self.assertEqual(self.playback.receive_packets(0.25), (self.packet,))
        self.connection.receive_packets.assert_called_with(0.25)
        self.playback.pause()
        with self.assertRaises(PlaybackStateError):
            self.playback.receive_packets()
        self.playback.resume()
        self.playback.seek(25)
        self.assertEqual(self.playback.receive_packets(), (self.packet,))

    def test_encoded_packet_duration_is_validated(self) -> None:
        self.playback.start()
        with self.assertRaises(ValueError):
            self.playback.receive_packets(0)

    def test_close_is_idempotent_and_operations_after_close_fail(self) -> None:
        self.playback.start()
        self.playback.close()
        self.playback.close()
        self.connection.teardown.assert_called_once_with()
        self.connection.close_socket.assert_called_once_with()
        self.closed.assert_called_once_with(self.playback)
        with self.assertRaises(PlaybackStateError):
            self.playback.pause()

    def test_cleanup_failure_does_not_mask_body_error(self) -> None:
        self.connection.teardown.side_effect = InvalidResponseError("teardown failed")
        with self.assertRaisesRegex(RuntimeError, "body failed"):
            with self.playback as playback:
                playback.start()
                raise RuntimeError("body failed")
        self.connection.close_socket.assert_called_once_with()


class RelativeSeekTests(TestCase):
    def setUp(self) -> None:
        self.clock = _Clock(100.0)
        self.connection = Mock()
        self.connection.duration = 75.0
        self.connection.video_codec = "H265"
        self.connection.video_control = "trackID=0"
        self.connection.returned_range = "npt=0.000000-75.000000"
        self.connection.pause.return_value = None
        self.connection.play.return_value = None
        self.playback = RecordingPlayback(
            self.connection,
            _recording(),
            clock=self.clock,
        )
        self.playback.start()

    def test_absolute_seek_semantics_and_server_range_override(self) -> None:
        self.connection.play.return_value = "npt=24.500000-75.000000"
        self.playback.seek(25)
        self.connection.play.assert_called_once_with("npt=25-")
        self.assertEqual(self.playback.position, 24.5)

    def test_relative_forward_and_backward_from_confirmed_position(self) -> None:
        self.playback.seek(25)
        self.playback.seek_relative(10)
        self.connection.play.assert_called_with("npt=35-")
        self.playback.seek_relative(-10)
        self.connection.play.assert_called_with("npt=25-")

    def test_relative_seek_while_playing_includes_monotonic_elapsed_time(self) -> None:
        self.playback.seek(25)
        self.clock.advance(2.5)
        self.playback.seek_relative(10)
        self.connection.play.assert_called_with("npt=37.5-")

    def test_relative_seek_clamps_to_zero_and_duration(self) -> None:
        self.playback.seek(5)
        self.playback.seek_relative(-10)
        self.connection.play.assert_called_with("npt=0-")
        self.playback.seek(70)
        self.playback.seek_relative(10)
        self.connection.play.assert_called_with("npt=75-")

    def test_paused_position_is_fixed_and_relative_seek_uses_it(self) -> None:
        self.clock.advance(5)
        self.playback.pause()
        paused = self.playback.position
        self.clock.advance(20)
        self.assertEqual(self.playback.position, paused)
        self.playback.seek_relative(-2)
        self.connection.play.assert_called_with("npt=3-")

    def test_resume_reanchors_position(self) -> None:
        self.clock.advance(5)
        self.playback.pause()
        self.clock.advance(20)
        self.playback.resume()
        self.assertEqual(self.playback.position, 5)
        self.clock.advance(2)
        self.assertEqual(self.playback.position, 7)

    def test_nonfinite_delta_and_invalid_states_are_rejected(self) -> None:
        for value in (float("nan"), float("inf"), float("-inf")):
            with self.subTest(value=value):
                with self.assertRaises(ValueError):
                    self.playback.seek_relative(value)
        new = RecordingPlayback(self.connection, _recording(), clock=self.clock)
        with self.assertRaises(PlaybackStateError):
            new.seek_relative(1)
        self.playback.close()
        with self.assertRaises(PlaybackStateError):
            self.playback.seek_relative(1)

    def test_position_is_clamped_and_preserved_after_close(self) -> None:
        self.playback.seek(74)
        self.clock.advance(5)
        self.assertEqual(self.playback.position, 75)
        self.playback.close()
        preserved = self.playback.position
        self.clock.advance(10)
        self.assertEqual(self.playback.position, preserved)


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


class _Clock:
    def __init__(self, value: float) -> None:
        self.value = value

    def __call__(self) -> float:
        return self.value

    def advance(self, seconds: float) -> None:
        self.value += seconds


def _connection(scripted: _ScriptedSocket) -> _RtspConnection:
    return _RtspConnection(
        host="recorder.example",
        port=554,
        username="admin",
        password="secret",
        timeout=2.0,
        file_path="/mnt/dvr/recording.dav",
        connector=lambda *args, **kwargs: scripted,
    )


def _successful_replies() -> list[bytes]:
    sdp = (
        b"v=0\r\n"
        b"a=range:npt=0-75.000000\r\n"
        b"m=video 0 RTP/AVP 98\r\n"
        b"a=control:trackID=0\r\n"
        b"a=rtpmap:98 H265/90000\r\n"
        b"m=audio 0 RTP/AVP 0\r\n"
        b"a=control:trackID=1\r\n"
    )
    return [
        _response(
            401,
            headers=(('WWW-Authenticate', 'Digest realm="r", nonce="n"'),),
        ),
        _response(
            200,
            headers=(("Content-Base", "rtsp://recorder.example:554/base/"),),
            body=sdp,
        ),
        _response(
            200,
            headers=(
                ("Session", "session-id;timeout=60"),
                ("Transport", "RTP/AVP/TCP;unicast;interleaved=0-1"),
            ),
        ),
        _response(200, headers=(("Range", "npt=0.000000-75.000000"),))
        + _frame(100)
        + _frame(101),
        _response(200),
        _response(200, headers=(("Range", "npt=25.000000-50.000000"),))
        + _frame(200),
        _response(200),
        _response(200),
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


def _recording() -> Recording:
    return Recording(
        channel=1,
        cluster=108756,
        disk=1,
        partition=1,
        start_time=datetime(2026, 8, 14, 5, 27, 10),
        end_time=datetime(2026, 8, 14, 5, 28, 25),
        file_path="/mnt/dvr/recording.dav",
        type="dav",
        video_stream="Main",
        events=("VideoMotion",),
        flags=("Event",),
        length=41943040,
        cut_length=0,
    )
