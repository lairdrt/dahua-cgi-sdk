import base64
import socket
import time
from datetime import datetime, timezone
from unittest import TestCase

from dahua_rpc import EncodedMediaPacket, MediaTrack
from dahua_rpc._recorded_bridge import (
    OutboundPacket,
    RecordedOutput,
    StartupTimeoutError,
    _OutboundQueue,
    _StartupGate,
    build_sdp,
)
from dahua_rpc._rtp_continuity import (
    CodecCompatibilityError,
    CodecConfigurationCache,
    RtpContinuityMapper,
    require_compatible,
    rewrite_rtcp,
    track_configurations,
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

    def test_stale_pre_seek_sender_report_is_rejected_after_new_rtp(self) -> None:
        mapper = RtpContinuityMapper(90000, ssrc=1)
        mapper.rewrite(_packet("video", 1, 1000, 10, 1.0))
        mapper.discontinuity()
        mapper.rewrite(_packet("video", 2, 90_000_000, 20, 1.1))

        self.assertIsNone(rewrite_rtcp(_sender_report(10, 1000), mapper))

    def test_compound_upstream_receiver_report_is_not_copied(self) -> None:
        mapper = RtpContinuityMapper(90000, ssrc=1)
        mapper.rewrite(_packet("video", 1, 1000, 10, 1.0))
        receiver_report = b"\x80\xc9\x00\x01" + (99).to_bytes(4, "big")

        rewritten = rewrite_rtcp(_sender_report(10, 1000) + receiver_report, mapper)

        self.assertIsNotNone(rewritten)
        assert rewritten is not None
        self.assertEqual(len(rewritten), 28)
        self.assertNotIn((99).to_bytes(4, "big"), rewritten)

    def test_generated_sender_report_uses_new_mapping_for_each_track(self) -> None:
        for clock_rate, media_type in ((90000, "video"), (8000, "audio")):
            mapper = RtpContinuityMapper(clock_rate, ssrc=clock_rate)
            mapper.rewrite(_packet(media_type, 1, 1000, 10, 1.0))
            mapper.discontinuity()
            mapped = mapper.rewrite(_packet(media_type, 2, 999999, 20, 1.1))

            report = mapper.generated_sender_report(now=100.25)

            self.assertIsNotNone(report)
            assert report is not None
            self.assertEqual(int.from_bytes(report[4:8], "big"), clock_rate)
            self.assertEqual(int.from_bytes(report[16:20], "big"), _timestamp(mapped))
            self.assertEqual(int.from_bytes(report[8:12], "big"), 2_208_988_900)
            self.assertEqual(int.from_bytes(report[12:16], "big"), 1 << 30)
            self.assertIsNone(mapper.generated_sender_report(now=101.0))


class CodecInitializationTests(TestCase):
    def test_h264_parameter_sets_are_read_from_fmtp(self) -> None:
        sps, pps = b"\x67\x64\x00\x1f", b"\x68\xee\x3c\x80"
        track = _video_track(
            "H264",
            96,
            (f"96 packetization-mode=1;sprop-parameter-sets="
             f"{_b64(sps)},{_b64(pps)}",),
        )

        initialization = CodecConfigurationCache((track,)).get("video")

        self.assertIsNotNone(initialization)
        assert initialization is not None
        self.assertEqual(initialization.codec, "H264")
        self.assertEqual(initialization.parameter_sets, (sps, pps))

    def test_h265_parameter_sets_are_read_from_fmtp(self) -> None:
        vps, sps, pps = b"\x40\x01", b"\x42\x01", b"\x44\x01"
        track = _video_track(
            "H265",
            98,
            (f"98 sprop-vps={_b64(vps)};sprop-sps={_b64(sps)};"
             f"sprop-pps={_b64(pps)}",),
        )

        initialization = CodecConfigurationCache((track,)).get("video")

        self.assertIsNotNone(initialization)
        assert initialization is not None
        self.assertEqual(initialization.parameter_sets, (vps, sps, pps))

    def test_payload_type_may_change_but_initialization_may_not(self) -> None:
        fmtp_96 = ("96 sprop-parameter-sets=Z2QAHw==,aO48gA==",)
        fmtp_112 = ("112 sprop-parameter-sets=Z2QAHw==,aO48gA==",)
        established = track_configurations((_video_track("H264", 96, fmtp_96),))

        require_compatible(
            established, (_video_track("H264", 112, fmtp_112),)
        )
        with self.assertRaisesRegex(CodecCompatibilityError, "incompatible video"):
            require_compatible(
                established,
                (_video_track(
                    "H264", 112,
                    ("112 sprop-parameter-sets=Z2QAIQ==,aO48gA==",),
                ),),
            )

    def test_seek_reinjects_valid_single_nal_rtp_before_media(self) -> None:
        bridge = _configured_bridge((_video_track(
            "H264", 96,
            ("96 sprop-parameter-sets=Z2QAHw==,aO48gA==",),
        ),))
        mapper = bridge._mappers["video"]
        mapper.next_sequence = 10
        bridge._begin_discontinuity(1.0)

        bridge._process_packet(_packet("video", 1, 9000, 99, 1.1))

        packets = [bridge._queue.get(0) for _ in range(4)]
        self.assertEqual([item.packet_type for item in packets], [
            "rtp", "rtp", "rtcp", "rtp",
        ])
        rtp_packets = [item for item in packets if item.packet_type == "rtp"]
        self.assertEqual(
            [_sequence(item.data) for item in rtp_packets], [10, 11, 12]
        )
        self.assertEqual(packets[0].data[12:], b"\x67\x64\x00\x1f")
        self.assertEqual(packets[1].data[12:], b"\x68\xee\x3c\x80")
        self.assertEqual(
            [item.data[1] & 0x7F for item in rtp_packets], [96, 96, 96]
        )


class PreparedTransitionTests(TestCase):
    def test_seek_gates_inter_frames_until_complete_h265_irap(self) -> None:
        tracks = (_h265_track(),)
        playback = _FakePlayback(tracks, [])
        bridge = _configured_bridge(tracks, playback)
        bridge._downstream_playing.set()
        bridge._mappers["video"].next_sequence = 10
        bridge._process_packet(
            _packet("video", 1, 9000, 1, 1.0, b"\x02\x01before")
        )
        before = bridge._queue.get(0)
        assert before is not None

        bridge.seek(10.0)
        for sequence in range(2, 102):
            bridge._process_packet(_packet(
                "video", sequence, 18000, 2, 2.0, b"\x02\x01inter"
            ))
        self.assertIsNone(bridge._queue.get(0))
        bridge._process_packet(_packet(
            "video", 102, 27000, 2, 3.0,
            b"\x62\x01\x93irap-a", marker=False,
        ))
        bridge._process_packet(_packet(
            "video", 103, 27000, 2, 3.01, b"\x62\x01\x53irap-b"
        ))

        queued = _drain_queue(bridge)
        rtp = [item.data for item in queued if item.packet_type == "rtp"]
        self.assertEqual(
            [item[12:] for item in rtp],
            [
                b"\x40\x01", b"\x42\x01", b"\x44\x01",
                b"\x62\x01\x93irap-a", b"\x62\x01\x53irap-b",
            ],
        )
        self.assertEqual([_sequence(item) for item in rtp], [11, 12, 13, 14, 15])
        self.assertEqual(len({_timestamp(item) for item in rtp}), 1)
        self.assertEqual(_ssrc(rtp[-1]), _ssrc(before.data))
        sender_reports = [
            item.data for item in queued
            if item.packet_type == "rtcp" and item.data[1] == 200
        ]
        self.assertEqual(len(sender_reports), 1)
        self.assertEqual(
            int.from_bytes(sender_reports[0][16:20], "big"),
            _timestamp(rtp[-1]),
        )
        self.assertTrue(bridge._downstream_playing.is_set())

    def test_seek_audio_hold_and_timeout_are_bounded(self) -> None:
        tracks = (
            _h265_track(),
            MediaTrack(
                "audio", "a", "AAC", 97, 8000, None, (), "recvonly"
            ),
        )
        playback = _FakePlayback(tracks, [])
        bridge = _configured_bridge(tracks, playback)
        bridge._downstream_playing.set()

        bridge.seek(10.0)
        for sequence in range(300):
            bridge._process_packet(_packet(
                "audio", sequence, sequence * 160, 2, 2.0 + sequence / 100
            ))
        self.assertEqual(bridge.startup_audio_high_water, 256)
        self.assertIsNone(bridge._queue.get(0))
        bridge._process_packet(_packet(
            "video", 1, 27000, 3, 3.0, b"\x26\x01irap"
        ))
        queued = _drain_queue(bridge)
        self.assertLessEqual(
            sum(item.media_type == "audio" for item in queued), 256
        )

        bridge.seek(20.0)
        assert bridge._startup_gate is not None
        bridge._startup_gate.deadline = 0.0
        bridge._expire_startup()
        self.assertIsInstance(bridge.startup_error, StartupTimeoutError)
        self.assertFalse(bridge._downstream_playing.is_set())
        self.assertFalse(bridge._startup_gate.active)

    def test_prepared_handoff_gates_incoming_inter_frames(self) -> None:
        tracks = (_h265_track(),)
        outgoing = _FakePlayback(tracks, [])
        bridge = _configured_bridge(tracks, outgoing)
        bridge._downstream_playing.set()
        bridge._mappers["video"].next_sequence = 20
        bridge._process_packet(
            _packet("video", 1, 9000, 1, 1.0, b"\x02\x01before")
        )
        before = bridge._queue.get(0)
        assert before is not None
        incoming_packets = [
            _packet("video", value, 18000, 2, 2.0, b"\x02\x01inter")
            for value in range(100, 200)
        ]
        incoming_packets.extend((
            _packet(
                "video", 200, 27000, 2, 3.0,
                b"\x62\x01\x93irap-a", marker=False,
            ),
            _packet(
                "video", 201, 27000, 2, 3.01,
                b"\x62\x01\x53irap-b",
            ),
        ))
        incoming = _FakePlayback(tracks, [incoming_packets])
        boundary = datetime(2026, 8, 17, tzinfo=timezone.utc)
        bridge.prepare(
            incoming, target_time=boundary, recording_start=boundary
        )

        bridge.activate_prepared()

        queued = _drain_queue(bridge)
        rtp = [item.data for item in queued if item.packet_type == "rtp"]
        self.assertEqual(
            [item[12:] for item in rtp],
            [
                b"\x40\x01", b"\x42\x01", b"\x44\x01",
                b"\x62\x01\x93irap-a", b"\x62\x01\x53irap-b",
            ],
        )
        self.assertEqual([_sequence(item) for item in rtp], [21, 22, 23, 24, 25])
        self.assertEqual(len({_timestamp(item) for item in rtp}), 1)
        self.assertEqual(_ssrc(rtp[-1]), _ssrc(before.data))
        self.assertTrue(bridge._downstream_playing.is_set())

    def test_dual_track_boundary_keeps_identity_and_continuity(self) -> None:
        tracks = _tracks_with_initialization()
        outgoing = _FakePlayback(tracks, [])
        bridge = _configured_bridge(tracks, outgoing)
        video_before = bridge._mappers["video"].rewrite(
            _packet("video", 1, 9000, 1, 1.0)
        )
        audio_before = bridge._mappers["audio"].rewrite(
            _packet("audio", 1, 800, 2, 1.0)
        )
        incoming = _FakePlayback(tracks, [[
            _packet("video", 400, 4_000_000, 30, 2.10),
            _packet("audio", 500, 800_000, 40, 2.12),
        ]])
        boundary = datetime(2026, 8, 17, 16, 0, tzinfo=timezone.utc)

        offset = bridge.prepare(
            incoming,
            target_time=boundary,
            recording_start=boundary,
        )
        result = bridge.activate_prepared()

        self.assertEqual(offset, 0.0)
        self.assertTrue(outgoing.closed)
        self.assertIs(bridge._playback, incoming)
        self.assertEqual(set(result.readiness_seconds), {"video", "audio"})
        queued = []
        while (item := bridge._queue.get(0)) is not None:
            queued.append(item)
        video_after = next(
            item.data for item in reversed(queued)
            if item.media_type == "video" and item.packet_type == "rtp"
        )
        audio_after = next(
            item.data for item in reversed(queued)
            if item.media_type == "audio" and item.packet_type == "rtp"
        )
        self.assertEqual(_ssrc(video_after), _ssrc(video_before))
        self.assertEqual(_ssrc(audio_after), _ssrc(audio_before))
        self.assertLess(
            (_timestamp(video_after) - _timestamp(video_before)) & 0xFFFFFFFF,
            90000,
        )
        self.assertLess(
            (_timestamp(audio_after) - _timestamp(audio_before)) & 0xFFFFFFFF,
            8000,
        )
        video_payloads = [
            item.data[12:] for item in queued
            if item.media_type == "video" and item.packet_type == "rtp"
        ]
        self.assertIn(b"\x67\x64\x00\x1f", video_payloads)
        self.assertIn(b"\x68\xee\x3c\x80", video_payloads)

    def test_handoff_buffer_is_bounded(self) -> None:
        tracks = _tracks()
        packets = [_packet("video", value, value * 100, 1, 2.0)
                   for value in range(300)]
        packets.append(_packet("audio", 1, 100, 2, 2.1))
        bridge = _configured_bridge(tracks, _FakePlayback(tracks, []))
        incoming = _FakePlayback(tracks, [packets])
        boundary = datetime(2026, 8, 17, tzinfo=timezone.utc)
        bridge.prepare(
            incoming, target_time=boundary, recording_start=boundary
        )

        result = bridge.activate_prepared()

        self.assertEqual(result.buffered_packets, 256)

    def test_incompatible_boundary_is_closed_and_rejected(self) -> None:
        tracks = (_video_track(
            "H264", 96, ("96 sprop-parameter-sets=Z2QAHw==,aO48gA==",)
        ),)
        bridge = _configured_bridge(tracks)
        incoming = _FakePlayback(
            (_video_track(
                "H265", 98,
                ("98 sprop-vps=QAE=;sprop-sps=QgE=;sprop-pps=RAE=",),
            ),),
            [],
        )
        boundary = datetime(2026, 8, 17, tzinfo=timezone.utc)

        with self.assertRaises(CodecCompatibilityError):
            bridge.prepare(
                incoming, target_time=boundary, recording_start=boundary
            )

        self.assertTrue(incoming.closed)


class QueueAndSdpTests(TestCase):
    def test_queue_drops_oldest_at_fixed_capacity(self) -> None:
        queue = _OutboundQueue(2)
        for value in (b"one", b"two", b"three"):
            queue.put(OutboundPacket("video", "rtp", 1.0, value))

        self.assertEqual(queue.drops, 1)
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

        video_only = RecordedOutput(playback)
        dual = RecordedOutput(playback, include_audio=True)

        self.assertEqual([track.media_type for track in video_only.tracks], ["video"])
        self.assertEqual(
            [track.media_type for track in dual.tracks], ["video", "audio"]
        )


class ServerLifecycleTests(TestCase):
    def test_non_loopback_bind_is_rejected(self) -> None:
        with self.assertRaisesRegex(ValueError, "loopback"):
            RecordedOutput(_FakePlayback(_tracks(), []), host="0.0.0.0")

    def test_describe_hides_upstream_control_and_close_stops_workers(self) -> None:
        playback = _FakePlayback(_tracks_with_initialization(), [])
        output = RecordedOutput(playback)
        output.start()
        client = socket.create_connection(("127.0.0.1", _port(output.url)))
        client.sendall(
            f"DESCRIBE {output.url} RTSP/1.0\r\nCSeq: 1\r\n\r\n".encode()
        )

        response = client.recv(4096)

        self.assertIn(b"RTSP/1.0 200", response)
        self.assertIn(b"a=control:trackID=video", response)
        self.assertNotIn(b"upstream", response)
        client.close()
        _wait_for(lambda: output._client is None)
        output.close()
        self.assertTrue(playback.closed)
        self.assertTrue(all(not thread.is_alive() for thread in output._threads))

    def test_late_h265_client_receives_initialization_before_current_media(
        self,
    ) -> None:
        tracks = (
            _video_track(
                "H265",
                98,
                ("98 sprop-vps=QAE=;sprop-sps=QgE=;sprop-pps=RAE=",),
            ),
        )
        playback = _FakePlayback(
            tracks, [[_packet("video", 1, 9000, 1, 1.0, b"early")]]
        )
        output = RecordedOutput(playback)
        output.start()
        try:
            _wait_for(lambda: not playback.batches)
            with socket.create_connection((output.host, output.port)) as client:
                client.settimeout(1.0)
                self.assertIn(
                    b"RTSP/1.0 200",
                    _rtsp_request(client, "DESCRIBE", output.url, 1),
                )
                self.assertIn(
                    b"RTSP/1.0 200",
                    _rtsp_request(
                        client,
                        "SETUP",
                        f"{output.url}/trackID=video",
                        2,
                        "Transport: RTP/AVP/TCP;unicast;interleaved=0-1\r\n",
                    ),
                )
                self.assertIn(
                    b"RTSP/1.0 200",
                    _rtsp_request(
                        client,
                        "PLAY",
                        output.url,
                        3,
                        f"Session: {output.session}\r\n",
                    ),
                )
                playback.batches.append([
                    _packet("video", 2, 18000, 1, 2.0, b"\x02\x01inter"),
                    _packet(
                        "video", 3, 27000, 1, 3.0,
                        b"\x62\x01\x93irap-a", marker=False,
                    ),
                    _packet(
                        "video", 4, 27000, 1, 3.01,
                        b"\x62\x01\x53irap-b",
                    ),
                ])
                payloads = []
                packets = []
                while len(payloads) < 5:
                    channel, data = _interleaved_frame(client)
                    if channel == 0:
                        payloads.append(data[12:])
                        packets.append(data)

            self.assertEqual(
                payloads,
                [
                    b"\x40\x01", b"\x42\x01", b"\x44\x01",
                    b"\x62\x01\x93irap-a", b"\x62\x01\x53irap-b",
                ],
            )
            self.assertEqual(
                [_sequence(item) for item in packets],
                list(range(_sequence(packets[0]), _sequence(packets[0]) + 5)),
            )
            self.assertEqual(len({_timestamp(item) for item in packets}), 1)
            self.assertLessEqual(output.startup_packet_high_water, 2048)
        finally:
            output.close()

    def test_startup_timeout_disconnects_without_stalling_upstream(self) -> None:
        tracks = (
            _video_track(
                "H265", 98,
                ("98 sprop-vps=QAE=;sprop-sps=QgE=;sprop-pps=RAE=",),
            ),
        )
        playback = _FakePlayback(tracks, [])
        output = RecordedOutput(playback, startup_timeout=0.05)
        output.start()
        try:
            with socket.create_connection((output.host, output.port)) as client:
                client.settimeout(1.0)
                _rtsp_request(client, "DESCRIBE", output.url, 1)
                _rtsp_request(
                    client, "SETUP", f"{output.url}/trackID=video", 2,
                    "Transport: RTP/AVP/TCP;unicast;interleaved=0-1\r\n",
                )
                _rtsp_request(
                    client, "PLAY", output.url, 3,
                    f"Session: {output.session}\r\n",
                )
                self.assertEqual(client.recv(1), b"")
            self.assertIsInstance(output.startup_error, StartupTimeoutError)
            self.assertTrue(output._upstream_enabled.is_set())
            self.assertIsNone(output.error)
        finally:
            output.close()


class StartupGateTests(TestCase):
    def test_h265_discards_inter_au_and_requires_complete_fragmented_irap(
        self,
    ) -> None:
        gate = _StartupGate("h265", 4, 2)
        gate.arm(time.monotonic() + 1)
        self.assertIsNone(gate.add_video(
            _packet("video", 1, 100, 1, 1.0, b"\x02\x01inter")
        ))
        self.assertIsNone(gate.add_video(_packet(
            "video", 2, 200, 1, 2.0, b"\x62\x01\x93a", marker=False
        )))
        ready = gate.add_video(_packet(
            "video", 3, 200, 1, 2.1, b"\x62\x01\x53b"
        ))
        self.assertIsNotNone(ready)
        video, _ = ready or ((), ())
        self.assertEqual([item.sequence_number for item in video], [2, 3])

    def test_h264_idr_uses_same_gate(self) -> None:
        gate = _StartupGate("h264", 4, 2)
        gate.arm(time.monotonic() + 1)
        ready = gate.add_video(
            _packet("video", 1, 100, 1, 1.0, b"\x65idr")
        )
        self.assertIsNotNone(ready)

    def test_candidate_and_audio_buffers_are_bounded(self) -> None:
        gate = _StartupGate("h265", 2, 2)
        gate.arm(time.monotonic() + 1)
        for sequence in range(1, 5):
            gate.add_audio(_packet("audio", sequence, sequence, 2, sequence))
        gate.add_video(_packet(
            "video", 1, 100, 1, 1.0, b"\x62\x01\x93a", marker=False
        ))
        gate.add_video(_packet(
            "video", 2, 100, 1, 1.1, b"\x62\x01\x13b", marker=False
        ))
        self.assertIsNone(gate.add_video(_packet(
            "video", 3, 100, 1, 1.2, b"\x62\x01\x53c"
        )))
        self.assertEqual(gate.audio_high_water, 2)
        self.assertEqual(gate.packet_high_water, 2)

    def test_audio_before_keyframe_is_not_released(self) -> None:
        gate = _StartupGate("h265", 4, 4)
        gate.arm(time.monotonic() + 1)
        gate.add_audio(_packet("audio", 1, 10, 2, 1.0))
        gate.add_audio(_packet("audio", 2, 20, 2, 2.1))
        ready = gate.add_video(
            _packet("video", 3, 100, 1, 2.0, b"\x26\x01irap")
        )
        self.assertIsNotNone(ready)
        _, audio = ready or ((), ())
        self.assertEqual([item.sequence_number for item in audio], [2])

    def test_prepared_session_can_be_aborted(self) -> None:
        output = _configured_bridge(_tracks())
        prepared = _FakePlayback(_tracks(), [])
        boundary = datetime(2026, 8, 17, tzinfo=timezone.utc)
        output.prepare(
            prepared, target_time=boundary, recording_start=boundary
        )

        output.abort_prepared()

        self.assertTrue(prepared.closed)
        self.assertIsNone(output._prepared)

    def test_close_records_upstream_cleanup_error_without_leaking_it(self) -> None:
        playback = _FakePlayback(_tracks(), [])
        output = _configured_bridge(_tracks(), playback)
        playback.close_error = RuntimeError("teardown failed")

        output.close()

        self.assertIsInstance(output.error, RuntimeError)
        self.assertEqual(str(output.error), "teardown failed")


def _packet(
    media_type: str,
    sequence: int,
    timestamp: int,
    ssrc: int,
    arrival: float,
    payload: bytes = b"payload",
    marker: bool = True,
) -> EncodedMediaPacket:
    data = (
        b"\x80" + bytes((0x62 | (0x80 if marker else 0),))
        + sequence.to_bytes(2, "big")
        + timestamp.to_bytes(4, "big")
        + ssrc.to_bytes(4, "big")
        + payload
    )
    return EncodedMediaPacket(
        media_type=media_type,
        packet_type="rtp",
        interleaved_channel=0 if media_type == "video" else 2,
        arrival_time=arrival,
        data=data,
        payload_type=98 if media_type == "video" else 97,
        marker=marker,
        sequence_number=sequence,
        rtp_timestamp=timestamp,
        ssrc=ssrc,
    )


def _tracks() -> tuple[MediaTrack, ...]:
    return (
        MediaTrack("video", "v", "H264", 96, 90000, None, (), "recvonly"),
        MediaTrack("audio", "a", "AAC", 97, 8000, None, (), "recvonly"),
    )


def _tracks_with_initialization() -> tuple[MediaTrack, ...]:
    return (
        _video_track(
            "H264", 96,
            ("96 packetization-mode=1;sprop-parameter-sets=Z2QAHw==,aO48gA==",),
        ),
        MediaTrack("audio", "a", "AAC", 97, 8000, None, (), "recvonly"),
    )


def _video_track(
    codec: str, payload_type: int, fmtp: tuple[str, ...]
) -> MediaTrack:
    return MediaTrack(
        "video", "v", codec, payload_type, 90000, None, fmtp, "recvonly"
    )


def _h265_track() -> MediaTrack:
    return _video_track(
        "H265", 98,
        ("98 sprop-vps=QAE=;sprop-sps=QgE=;sprop-pps=RAE=",),
    )


def _b64(value: bytes) -> str:
    return base64.b64encode(value).decode()


def _configured_bridge(
    tracks: tuple[MediaTrack, ...], playback: object | None = None
) -> RecordedOutput:
    selected = playback or _FakePlayback(tracks, [])
    bridge = RecordedOutput(selected, include_audio=any(
        track.media_type == "audio" for track in tracks
    ))
    bridge._tracks = tracks
    bridge._configurations = track_configurations(tracks)
    bridge._cache = CodecConfigurationCache(tracks)
    video = bridge._configurations.get("video")
    if video is not None and video.codec.upper() in ("H264", "H265"):
        bridge._startup_gate = _StartupGate(video.codec, 2048, 256)
    bridge._mappers = {
        track.media_type: RtpContinuityMapper(
            track.clock_rate or 1,
            payload_type=track.payload_type,
            ssrc=index,
        )
        for index, track in enumerate(tracks, start=1)
    }
    return bridge


def _drain_queue(bridge: RecordedOutput) -> list[OutboundPacket]:
    packets = []
    while (packet := bridge._queue.get(0)) is not None:
        packets.append(packet)
    return packets


class _FakePlayback:
    duration = 60.0

    def __init__(
        self,
        tracks: tuple[MediaTrack, ...],
        batches: list[list[EncodedMediaPacket]],
    ) -> None:
        self.media_tracks = tracks
        self.batches = batches
        self.closed = False
        self.started = False
        self.paused = False
        self.seek_seconds: float | None = None
        self.close_error: Exception | None = None

    def start(self) -> None:
        self.started = True

    def seek(self, seconds: float) -> None:
        self.seek_seconds = seconds

    def pause(self) -> None:
        self.paused = True

    def resume(self) -> None:
        self.paused = False

    def receive_packets(self, duration: float) -> tuple[EncodedMediaPacket, ...]:
        del duration
        return tuple(self.batches.pop(0)) if self.batches else ()

    def close(self) -> None:
        self.closed = True
        if self.close_error is not None:
            raise self.close_error


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


def _port(url: str) -> int:
    return int(url.split(":", 2)[2].split("/", 1)[0])


def _wait_for(predicate: object) -> None:
    assert callable(predicate)
    deadline = time.monotonic() + 1.0
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.01)
    raise AssertionError("condition was not reached")


def _rtsp_request(
    client: socket.socket,
    method: str,
    target: str,
    cseq: int,
    headers: str = "",
) -> bytes:
    client.sendall(
        f"{method} {target} RTSP/1.0\r\nCSeq: {cseq}\r\n{headers}\r\n".encode()
    )
    response = bytearray()
    while b"\r\n\r\n" not in response:
        response.extend(client.recv(4096))
    header, body = bytes(response).split(b"\r\n\r\n", 1)
    content_length = 0
    for line in header.split(b"\r\n")[1:]:
        name, _, value = line.partition(b":")
        if name.lower() == b"content-length":
            content_length = int(value.strip())
    if len(body) < content_length:
        body += _recv_exact(client, content_length - len(body))
    return header + b"\r\n\r\n" + body


def _interleaved_frame(client: socket.socket) -> tuple[int, bytes]:
    header = _recv_exact(client, 4)
    if header[0] != 0x24:
        raise AssertionError("expected an interleaved RTP/RTCP frame")
    return header[1], _recv_exact(client, int.from_bytes(header[2:4], "big"))


def _recv_exact(client: socket.socket, length: int) -> bytes:
    data = bytearray()
    while len(data) < length:
        chunk = client.recv(length - len(data))
        if not chunk:
            raise AssertionError("RTSP client disconnected")
        data.extend(chunk)
    return bytes(data)
