import re
import socket
from unittest import TestCase

from dahua_rpc._rtsp_connection import (
    _parse_media_packet,
    _parse_sdp,
    _RtspConnection,
)
from dahua_rpc.exceptions import InvalidResponseError


class SdpMediaTrackTests(TestCase):
    def test_generic_video_and_audio_tracks_preserve_actual_values(self) -> None:
        description = _parse_sdp(
            b"v=0\r\n"
            b"a=range:npt=0-30.5\r\n"
            b"m=video 0 RTP/AVP 112\r\n"
            b"a=control:camera-video\r\n"
            b"a=rtpmap:112 AV1/90000\r\n"
            b"a=fmtp:112 profile=1\r\n"
            b"a=recvonly\r\n"
            b"m=audio 0 RTP/AVP 109\r\n"
            b"a=control:camera-audio\r\n"
            b"a=rtpmap:109 OPUS/48000/2\r\n"
            b"a=fmtp:109 minptime=10\r\n"
            b"a=sendrecv\r\n"
        )

        self.assertEqual(description.duration, 30.5)
        video, audio = description.tracks
        self.assertEqual(
            (
                video.media_type,
                video.control,
                video.codec,
                video.payload_type,
                video.clock_rate,
                video.channels,
                video.fmtp,
                video.direction,
            ),
            ("video", "camera-video", "AV1", 112, 90000, None,
             ("112 profile=1",), "recvonly"),
        )
        self.assertEqual(
            (
                audio.media_type,
                audio.control,
                audio.codec,
                audio.payload_type,
                audio.clock_rate,
                audio.channels,
                audio.fmtp,
                audio.direction,
            ),
            ("audio", "camera-audio", "OPUS", 109, 48000, 2,
             ("109 minptime=10",), "sendrecv"),
        )


class EncodedPacketParsingTests(TestCase):
    def test_video_and_audio_rtp_header_fields_are_exposed(self) -> None:
        for media_type, channel, payload_type in (
            ("video", 0, 98),
            ("audio", 2, 97),
        ):
            with self.subTest(media_type=media_type):
                data = _rtp(
                    payload_type=payload_type,
                    marker=True,
                    sequence=0x1234,
                    timestamp=0x23456789,
                    ssrc=0x34567890,
                )
                packet = _parse_media_packet(
                    media_type, "rtp", channel, 12.5, data
                )
                self.assertEqual(packet.data, data)
                self.assertEqual(packet.interleaved_channel, channel)
                self.assertEqual(packet.arrival_time, 12.5)
                self.assertEqual(packet.payload_type, payload_type)
                self.assertTrue(packet.marker)
                self.assertEqual(packet.sequence_number, 0x1234)
                self.assertEqual(packet.rtp_timestamp, 0x23456789)
                self.assertEqual(packet.ssrc, 0x34567890)

    def test_compound_rtcp_classifies_sr_and_sdes(self) -> None:
        data = _sender_report() + _sdes()
        packet = _parse_media_packet("audio", "rtcp", 3, 20.0, data)

        self.assertEqual(packet.data, data)
        self.assertEqual([item.packet_type for item in packet.rtcp_packets], [200, 202])
        sender = packet.rtcp_packets[0]
        self.assertEqual(sender.ssrc, 0x11223344)
        self.assertEqual(sender.ntp_seconds, 0x01020304)
        self.assertEqual(sender.ntp_fraction, 0x05060708)
        self.assertEqual(sender.rtp_timestamp, 0x10203040)

    def test_short_or_malformed_packets_are_preserved_without_metadata(self) -> None:
        rtp = _parse_media_packet("video", "rtp", 0, 1.0, b"short")
        rtcp = _parse_media_packet("audio", "rtcp", 3, 2.0, b"bad")

        self.assertIsNone(rtp.sequence_number)
        self.assertIsNone(rtp.rtp_timestamp)
        self.assertEqual(rtp.data, b"short")
        self.assertEqual(rtcp.rtcp_packets, ())
        self.assertEqual(rtcp.data, b"bad")


class MultiTrackRtspTests(TestCase):
    def test_audio_opt_in_uses_shared_session_and_ordered_packets(self) -> None:
        scripted = _ScriptedSocket(_dual_replies())
        connection = _connection(scripted, include_audio=True)

        connection.start()
        packets = connection.receive_packets(0.01)
        connection.pause()
        connection.play("npt=10-")
        after_seek = connection.receive_packets(0.01)
        connection.teardown()
        connection.close_socket()

        requests = [request.decode("ascii") for request in scripted.sent]
        self.assertIn("interleaved=0-1", requests[2])
        self.assertIn("trackID=video", requests[2])
        self.assertIn("interleaved=2-3", requests[3])
        self.assertIn("trackID=audio", requests[3])
        self.assertIn("Session: shared-session", requests[3])
        self.assertEqual(
            [(packet.media_type, packet.packet_type) for packet in packets],
            [
                ("video", "rtp"),
                ("video", "rtcp"),
                ("audio", "rtp"),
                ("audio", "rtcp"),
            ],
        )
        self.assertEqual(after_seek[0].sequence_number, 50)
        self.assertEqual(after_seek[0].rtp_timestamp, 900000)
        self.assertEqual(after_seek[0].ssrc, 100)
        self.assertEqual(after_seek[1].sequence_number, 60)
        self.assertEqual(after_seek[1].rtp_timestamp, 80000)
        self.assertEqual(after_seek[1].ssrc, 200)
        self.assertEqual(
            [(track.media_type, track.codec) for track in connection.media_tracks],
            [("video", "H264"), ("audio", "MPEG4-GENERIC")],
        )

    def test_video_only_remains_one_setup(self) -> None:
        replies = _dual_replies()
        del replies[3]
        scripted = _ScriptedSocket(replies)
        connection = _connection(scripted)

        connection.start()
        connection.close_socket()

        setup = [request for request in scripted.sent if request.startswith(b"SETUP")]
        self.assertEqual(len(setup), 1)
        self.assertIn(b"trackID=video", setup[0])

    def test_requested_missing_audio_fails_explicitly_before_setup(self) -> None:
        scripted = _ScriptedSocket(_describe_only_replies())
        connection = _connection(scripted, include_audio=True)

        with self.assertRaisesRegex(InvalidResponseError, "audio track"):
            connection.start()

        self.assertFalse(any(item.startswith(b"SETUP") for item in scripted.sent))
        self.assertTrue(scripted.closed)


class _ScriptedSocket:
    def __init__(self, replies: list[bytes]) -> None:
        self.replies = list(replies)
        self.incoming = bytearray()
        self.sent: list[bytes] = []
        self.closed = False

    def sendall(self, request: bytes) -> None:
        self.sent.append(request)
        if self.replies:
            reply = self.replies.pop(0)
            cseq = re.search(rb"\r\nCSeq: (\d+)\r\n", request).group(1)
            if reply.startswith(b"RTSP/") and b"\r\nCSeq:" not in reply:
                line, remainder = reply.split(b"\r\n", 1)
                reply = line + b"\r\nCSeq: " + cseq + b"\r\n" + remainder
            self.incoming.extend(reply)

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


def _connection(
    scripted: _ScriptedSocket, *, include_audio: bool = False
) -> _RtspConnection:
    return _RtspConnection(
        host="recorder.example",
        port=554,
        username="admin",
        password="secret",
        timeout=2.0,
        file_path="/recording.dav",
        include_audio=include_audio,
        connector=lambda *args, **kwargs: scripted,
    )


def _sdp(*, audio: bool = True) -> bytes:
    value = (
        b"v=0\r\na=range:npt=0-60.0\r\n"
        b"m=video 0 RTP/AVP 96\r\n"
        b"a=control:trackID=video\r\n"
        b"a=rtpmap:96 H264/90000\r\n"
    )
    if audio:
        value += (
            b"m=audio 0 RTP/AVP 97\r\n"
            b"a=control:trackID=audio\r\n"
            b"a=rtpmap:97 MPEG4-GENERIC/8000\r\n"
        )
    return value


def _dual_replies() -> list[bytes]:
    media = (
        _frame(0, _rtp(98, False, 1, 1000, 100))
        + _frame(1, _sender_report(ssrc=100))
        + _frame(2, _rtp(97, True, 2, 800, 200))
        + _frame(3, _sender_report(ssrc=200))
    )
    sought = (
        _frame(0, _rtp(98, False, 50, 900000, 100))
        + _frame(2, _rtp(97, True, 60, 80000, 200))
    )
    return [
        _challenge(),
        _response(200, headers=(("Content-Base", "rtsp://r/base/"),), body=_sdp()),
        _response(200, headers=(("Session", "shared-session;timeout=60"),
                                ("Transport", "RTP/AVP/TCP;interleaved=0-1"))),
        _response(200, headers=(("Session", "shared-session;timeout=60"),
                                ("Transport", "RTP/AVP/TCP;interleaved=2-3"))),
        _response(200) + media,
        _response(200),
        _response(200) + sought,
        _response(200),
    ]


def _describe_only_replies() -> list[bytes]:
    return [_challenge(), _response(200, body=_sdp(audio=False))]


def _challenge() -> bytes:
    return _response(
        401, headers=(("WWW-Authenticate", 'Digest realm="r", nonce="n"'),)
    )


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


def _frame(channel: int, data: bytes) -> bytes:
    return b"$" + bytes((channel,)) + len(data).to_bytes(2, "big") + data


def _rtp(
    payload_type: int,
    marker: bool,
    sequence: int,
    timestamp: int,
    ssrc: int,
) -> bytes:
    return (
        b"\x80"
        + bytes((payload_type | (0x80 if marker else 0),))
        + sequence.to_bytes(2, "big")
        + timestamp.to_bytes(4, "big")
        + ssrc.to_bytes(4, "big")
        + b"encoded"
    )


def _sender_report(ssrc: int = 0x11223344) -> bytes:
    return (
        b"\x80\xc8\x00\x06"
        + ssrc.to_bytes(4, "big")
        + b"\x01\x02\x03\x04"
        + b"\x05\x06\x07\x08"
        + b"\x10\x20\x30\x40"
        + b"\x00" * 8
    )


def _sdes() -> bytes:
    return b"\x81\xca\x00\x01\x11\x22\x33\x44"
