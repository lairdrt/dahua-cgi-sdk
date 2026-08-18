import socket
import threading
import time
from unittest import TestCase

from dahua_rpc._rtsp_connection import _RtspConnection, _RtspStream
from dahua_rpc.exceptions import InvalidResponseError, TransportError


class RtspStreamMultiplexingTests(TestCase):
    def setUp(self) -> None:
        self.client, self.server = socket.socketpair()
        self.stream = _RtspStream(self.client, media_capacity=4)
        self.stream.start()

    def tearDown(self) -> None:
        self.stream.close()
        self.server.close()

    def test_all_interleaved_channels_are_preserved_before_response(self) -> None:
        pending = self.stream.register(7)
        frames = b"".join(_frame(channel, bytes((channel,))) for channel in range(4))
        self.server.sendall(frames + _response(7))

        response = self.stream.response(7, pending, 1.0)
        packets = self.stream.media_packets(
            0.01,
            {
                0: ("video", "rtp"),
                1: ("video", "rtcp"),
                2: ("audio", "rtp"),
                3: ("audio", "rtcp"),
            },
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            [packet.interleaved_channel for packet in packets], [0, 1, 2, 3]
        )
        self.assertEqual(self.stream.media_queue_drops, 0)
        self.assertEqual(self.stream.media_queue_high_water, 4)

    def test_bounded_queue_drops_oldest_and_preserves_remaining_order(self) -> None:
        self.server.sendall(b"".join(_frame(0, bytes((value,))) for value in range(6)))
        deadline = time.monotonic() + 1.0
        while self.stream.media_queue_drops < 2 and time.monotonic() < deadline:
            time.sleep(0.001)

        packets = self.stream.media_packets(0.01, {0: ("video", "rtp")})

        self.assertEqual(
            [packet.data for packet in packets],
            [b"\x02", b"\x03", b"\x04", b"\x05"],
        )
        self.assertEqual(self.stream.media_queue_drops, 2)
        self.assertEqual(self.stream.media_queue_high_water, 4)

    def test_missing_cseq_fails_pending_request(self) -> None:
        pending = self.stream.register(1)
        self.server.sendall(_response(None))

        with self.assertRaisesRegex(InvalidResponseError, "omitted a valid CSeq"):
            self.stream.response(1, pending, 1.0)

    def test_unexpected_cseq_fails_pending_request(self) -> None:
        pending = self.stream.register(1)
        self.server.sendall(_response(2))

        with self.assertRaisesRegex(InvalidResponseError, "Unexpected or duplicate"):
            self.stream.response(1, pending, 1.0)

    def test_timeout_removes_pending_request(self) -> None:
        pending = self.stream.register(1)

        with self.assertRaisesRegex(TransportError, "CSeq 1"):
            self.stream.response(1, pending, 0.01)
        self.assertNotIn(1, self.stream._pending)

    def test_socket_close_wakes_pending_request(self) -> None:
        pending = self.stream.register(1)
        self.server.close()

        with self.assertRaisesRegex(TransportError, "closed unexpectedly"):
            self.stream.response(1, pending, 1.0)


class KeepaliveLifecycleTests(TestCase):
    def test_keepalive_runs_while_playing_and_paused_then_stops(self) -> None:
        scripted = _AutoSocket()
        connection = _RtspConnection(
            host="recorder.example",
            port=554,
            username="admin",
            password="secret",
            timeout=0.5,
            file_path="/recording.dav",
            connector=lambda *args, **kwargs: scripted,
            keepalive_interval=0.02,
        )

        connection.start()
        self.assertTrue(_wait_for(lambda: connection.keepalive_count >= 1))
        connection.pause()
        paused_count = connection.keepalive_count
        self.assertTrue(_wait_for(lambda: connection.keepalive_count > paused_count))
        connection.play()
        connection.teardown()
        stopped_count = connection.keepalive_count
        time.sleep(0.04)
        connection.close_socket()

        methods = [request.split(b" ", 1)[0] for request in scripted.sent]
        self.assertGreaterEqual(methods.count(b"OPTIONS"), 2)
        self.assertEqual(connection.keepalive_count, stopped_count)
        self.assertFalse(connection._stream)
        self.assertTrue(scripted.closed)

    def test_session_timeout_derives_half_timeout_interval(self) -> None:
        scripted = _AutoSocket(session_timeout=80)
        connection = _RtspConnection(
            host="recorder.example",
            port=554,
            username="admin",
            password="secret",
            timeout=0.5,
            file_path="/recording.dav",
            connector=lambda *args, **kwargs: scripted,
        )

        connection.start()
        try:
            self.assertEqual(connection._session_timeout, 80)
            self.assertEqual(connection._keepalive_interval, 40)
        finally:
            connection.close_socket()


class _AutoSocket:
    def __init__(self, *, session_timeout: float = 60) -> None:
        self.session_timeout = session_timeout
        self.sent: list[bytes] = []
        self.incoming = bytearray()
        self.condition = threading.Condition()
        self.closed = False

    def sendall(self, request: bytes) -> None:
        method = request.split(b" ", 1)[0]
        cseq = int(request.split(b"CSeq: ", 1)[1].split(b"\r\n", 1)[0])
        if method == b"DESCRIBE" and b"Authorization:" not in request:
            reply = _response(
                cseq,
                401,
                (("WWW-Authenticate", 'Digest realm="r", nonce="n"'),),
            )
        elif method == b"DESCRIBE":
            reply = _response(cseq, body=_sdp())
        elif method == b"SETUP":
            reply = _response(
                cseq,
                headers=(
                    ("Session", f"session-id;timeout={self.session_timeout:g}"),
                    ("Transport", "RTP/AVP/TCP;unicast;interleaved=0-1"),
                ),
            )
        else:
            reply = _response(cseq)
        with self.condition:
            self.sent.append(request)
            self.incoming.extend(reply)
            self.condition.notify_all()

    def recv(self, count: int) -> bytes:
        with self.condition:
            if not self.incoming and not self.closed:
                self.condition.wait(0.01)
            if self.closed:
                return b""
            if not self.incoming:
                raise socket.timeout
            chunk = bytes(self.incoming[:count])
            del self.incoming[:count]
            return chunk

    def settimeout(self, timeout: float) -> None:
        pass

    def shutdown(self, how: int) -> None:
        with self.condition:
            self.closed = True
            self.condition.notify_all()

    def close(self) -> None:
        self.shutdown(socket.SHUT_RDWR)


def _wait_for(predicate) -> bool:
    deadline = time.monotonic() + 1.0
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.005)
    return False


def _frame(channel: int, payload: bytes) -> bytes:
    return b"$" + bytes((channel,)) + len(payload).to_bytes(2, "big") + payload


def _response(
    cseq: int | None,
    status: int = 200,
    headers: tuple[tuple[str, str], ...] = (),
    body: bytes = b"",
) -> bytes:
    reason = "OK" if status == 200 else "Unauthorized"
    lines = [f"RTSP/1.0 {status} {reason}"]
    if cseq is not None:
        lines.append(f"CSeq: {cseq}")
    lines.append(f"Content-Length: {len(body)}")
    lines.extend(f"{name}: {value}" for name, value in headers)
    return ("\r\n".join(lines) + "\r\n\r\n").encode() + body


def _sdp() -> bytes:
    return (
        b"v=0\r\na=range:npt=0-60\r\n"
        b"m=video 0 RTP/AVP 96\r\n"
        b"a=control:trackID=video\r\n"
        b"a=rtpmap:96 H264/90000\r\n"
    )
