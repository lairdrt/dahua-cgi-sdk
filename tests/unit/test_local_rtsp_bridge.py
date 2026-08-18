from unittest import TestCase

from dahua_rpc import EncodedMediaPacket, MediaTrack
from tools.probe_local_rtsp_bridge import (
    BoundedPacketQueue,
    LocalRtspBridge,
    OutboundPacket,
    RtpContinuityMapper,
    build_sdp,
    rewrite_rtcp,
)


class ContinuityMapperTests(TestCase):
    def test_stable_ssrc_sequence_and_timestamp_continuity(self) -> None:
        mapper = RtpContinuityMapper(90000, ssrc=0x11223344)
        mapper.next_sequence = 100

        first = mapper.rewrite(_packet("video", 10, 5000, 1, 1.0))
        second = mapper.rewrite(_packet("video", 11, 9500, 1, 1.05))

        self.assertEqual(_sequence(first), 100)
        self.assertEqual(_sequence(second), 101)
        self.assertEqual(_ssrc(first), 0x11223344)
        self.assertEqual(_ssrc(second), 0x11223344)
        self.assertEqual((_timestamp(second) - _timestamp(first)) & 0xFFFFFFFF, 4500)

    def test_seek_jump_is_reanchored_separately_for_video_and_audio(self) -> None:
        video = RtpContinuityMapper(90000, ssrc=1)
        audio = RtpContinuityMapper(8000, ssrc=2)
        before_video = video.rewrite(_packet("video", 1, 1000, 10, 1.0))
        before_audio = audio.rewrite(_packet("audio", 1, 100, 20, 1.0))
        video.discontinuity()
        audio.discontinuity()

        after_video = video.rewrite(_packet("video", 900, 90000000, 99, 1.1))
        after_audio = audio.rewrite(_packet("audio", 700, 8000000, 88, 1.12))

        self.assertEqual(
            (_timestamp(after_video) - _timestamp(before_video)) & 0xFFFFFFFF,
            9000,
        )
        self.assertEqual(
            (_timestamp(after_audio) - _timestamp(before_audio)) & 0xFFFFFFFF,
            960,
        )
        self.assertEqual(_ssrc(after_video), 1)
        self.assertEqual(_ssrc(after_audio), 2)


class RtcpRewriteTests(TestCase):
    def test_sender_report_uses_downstream_identity_and_original_ntp(self) -> None:
        mapper = RtpContinuityMapper(8000, ssrc=0xAABBCCDD)
        mapped = mapper.rewrite(_packet("audio", 1, 1000, 10, 1.0))
        report = _sender_report(10, 1000)

        rewritten = rewrite_rtcp(report, mapper)

        self.assertIsNotNone(rewritten)
        assert rewritten is not None
        self.assertEqual(int.from_bytes(rewritten[4:8], "big"), 0xAABBCCDD)
        self.assertEqual(rewritten[8:16], report[8:16])
        self.assertEqual(int.from_bytes(rewritten[16:20], "big"), _timestamp(mapped))

    def test_sender_report_is_dropped_while_seek_mapping_is_pending(self) -> None:
        mapper = RtpContinuityMapper(90000, ssrc=1)
        mapper.rewrite(_packet("video", 1, 1000, 10, 1.0))
        mapper.discontinuity()

        self.assertIsNone(rewrite_rtcp(_sender_report(10, 1000), mapper))


class QueueAndSdpTests(TestCase):
    def test_queue_drops_oldest_at_fixed_capacity(self) -> None:
        queue = BoundedPacketQueue(2)
        for value in (b"one", b"two", b"three"):
            queue.put(OutboundPacket("video", "rtp", 1.0, value))

        self.assertEqual(queue.dropped, 1)
        self.assertEqual(queue.get(0).data, b"two")
        self.assertEqual(queue.get(0).data, b"three")

    def test_sdp_uses_configurable_track_metadata(self) -> None:
        tracks = (
            MediaTrack(
                "video", "upstream-video", "AV1", 112, 90000, None,
                ("112 profile=1",), "recvonly",
            ),
            MediaTrack(
                "audio", "upstream-audio", "OPUS", 109, 48000, 2,
                ("109 minptime=10",), "recvonly",
            ),
        )

        sdp = build_sdp(tracks, 60.0)

        self.assertIn("a=rtpmap:112 AV1/90000", sdp)
        self.assertIn("a=rtpmap:109 OPUS/48000/2", sdp)
        self.assertIn("a=fmtp:112 profile=1", sdp)
        self.assertIn("a=control:trackID=video", sdp)
        self.assertIn("a=control:trackID=audio", sdp)
        self.assertNotIn("upstream-video", sdp)
        self.assertNotIn("upstream-audio", sdp)

    def test_video_only_bridge_does_not_advertise_discovered_audio(self) -> None:
        playback = type("Playback", (), {"media_tracks": _tracks()})()

        video_only = LocalRtspBridge(playback)
        dual = LocalRtspBridge(playback, include_audio=True)

        self.assertEqual([track.media_type for track in video_only.tracks], ["video"])
        self.assertEqual(
            [track.media_type for track in dual.tracks], ["video", "audio"]
        )


def _packet(
    media_type: str,
    sequence: int,
    timestamp: int,
    ssrc: int,
    arrival: float,
) -> EncodedMediaPacket:
    data = (
        b"\x80\x62"
        + sequence.to_bytes(2, "big")
        + timestamp.to_bytes(4, "big")
        + ssrc.to_bytes(4, "big")
        + b"payload"
    )
    return EncodedMediaPacket(
        media_type=media_type,
        packet_type="rtp",
        interleaved_channel=0 if media_type == "video" else 2,
        arrival_time=arrival,
        data=data,
        payload_type=98 if media_type == "video" else 97,
        marker=False,
        sequence_number=sequence,
        rtp_timestamp=timestamp,
        ssrc=ssrc,
    )


def _tracks() -> tuple[MediaTrack, ...]:
    return (
        MediaTrack("video", "v", "H264", 96, 90000, None, (), "recvonly"),
        MediaTrack("audio", "a", "AAC", 97, 8000, None, (), "recvonly"),
    )


def _sender_report(ssrc: int, timestamp: int) -> bytes:
    return (
        b"\x80\xc8\x00\x06"
        + ssrc.to_bytes(4, "big")
        + b"\x01\x02\x03\x04\x05\x06\x07\x08"
        + timestamp.to_bytes(4, "big")
        + b"\x00" * 8
    )


def _sequence(data: bytes) -> int:
    return int.from_bytes(data[2:4], "big")


def _timestamp(data: bytes) -> int:
    return int.from_bytes(data[4:8], "big")


def _ssrc(data: bytes) -> int:
    return int.from_bytes(data[8:12], "big")
