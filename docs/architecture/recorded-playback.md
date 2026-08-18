# Recorded Playback Internals

Recorded playback is split into deliberately narrow layers:

1. `RecordingPlayback` owns one file-specific authenticated NVR RTSP session,
   discovers SDP tracks, controls NPT, and maintains its RTSP keepalive. Its
   owning `DahuaClient` independently maintains the RPC2 session and keepalive.
2. `_media_fanout.EncodedPacketFanout` is the only caller of
   `receive_packets()`. It distributes immutable RTP/RTCP packets through
   bounded subscriptions without opening another NVR session.
3. `_rtp_continuity` maintains independent video and audio RTP/RTCP domains,
   each with a stable SSRC, payload type, sequence and timestamp space, and the
   actual SDP-discovered clock rate.
4. `_recorded_bridge.RecordedOutput` coordinates seek, pause/resume, prepared
   file handoff, packet rewriting, and one loopback RTSP presentation.
5. A future Home Assistant controller may select recordings/profiles, choose
   which output includes audio, and drive a master wall-clock. Those policies
   are not SDK responsibilities.

## Codec and Timing Rules

Main and Extra1 are profiles, not codec identities. Codec, payload type, clock
rate, channel count, and initialization come from each file's SDP. H.264
SPS/PPS and H.265 VPS/SPS/PPS FMTP values are the initialization contract. A
logical output rejects track-set, codec, clock-rate, channel-count, or
initialization changes. Incoming payload types may differ because packets map
to the established local payload type.

Audio is optional at output creation but otherwise first-class. Audio and video
never share SSRC, sequence, timestamp, or RTCP state, and no fixed A/V delay is
introduced. Upstream Sender Reports remain timing inputs; valid reports are
translated, and a local report may be generated immediately after a
discontinuity.

## File Handoff and Pressure

A `RecordingPlayback` never spans DAV files. `RecordedOutput.prepare()` starts
the next file, checks compatibility, seeks from timezone-aware recording
timestamps, and pauses it. `activate_prepared()` resumes it, waits for every
required RTP track in a bounded staging buffer, atomically switches the source,
re-anchors continuity, and closes the outgoing session. A prepared session can
be aborted and is always closed with the output.

All queues are bounded and prefer current media. Fan-out drops older RTP before
scarce RTCP where practical. The presentation queue also protects generated
codec initialization and RTCP where practical. Capacity, high-water, and drop
counters are internal observability, not a generalized QoS subsystem.

## Local Presentation Boundary

The private RTSP presentation binds to loopback only and implements OPTIONS,
DESCRIBE, SETUP, PLAY, PAUSE, and TEARDOWN with TCP-interleaved RTP/RTCP. SDP is
generated from discovered tracks. Recorder credentials, DAV paths, and NVR
session identifiers are never included in the local URL or SDP.

This phase supports one downstream RTSP client. The reusable fan-out supports
multiple bounded internal consumers without additional NVR sessions; complete
multi-client RTSP semantics are deferred. No decoding, transcoding, CGI, HLS,
WebRTC, go2rtc, or Home Assistant integration is part of this layer.
