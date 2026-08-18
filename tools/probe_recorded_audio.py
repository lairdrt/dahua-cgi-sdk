"""Probe recorded audio RTP/RTCP without retaining or decoding media."""

from __future__ import annotations

import hashlib
import json
import os
import socket
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta
from typing import Any

from dahua_rpc import DahuaClient
from dahua_rpc._rtsp_connection import (
    _control_uri,
    _parse_sdp_video,
    _RtspConnection,
    _RtspStream,
)
from dahua_rpc.exceptions import InvalidResponseError, RecorderConnectionError
from dahua_rpc.models import Camera, Recording, StreamProfile

PREFERRED_CAMERA = "Drive Down"
SEARCH_DAYS = 4
SAMPLE_SECONDS = 2.0
PAUSE_SAMPLE_SECONDS = 0.5
FIRST_MEDIA_TIMEOUT = 2.0


@dataclass(frozen=True)
class Track:
    media: str
    control: str
    payload_type: int
    codec: str
    clock_rate: int
    channels: int | None
    fmtp: tuple[str, ...]


@dataclass(frozen=True)
class RtpObservation:
    received_at: float
    channel: int
    version: int
    marker: bool
    payload_type: int
    sequence: int
    timestamp: int
    ssrc: int
    payload_bytes: int


@dataclass(frozen=True)
class RtcpObservation:
    received_at: float
    channel: int
    packet_type: int
    kind: str
    ssrc: int | None
    ntp_seconds: int | None = None
    ntp_fraction: int | None = None
    rtp_timestamp: int | None = None


@dataclass
class Capture:
    rtp: dict[str, list[RtpObservation]] = field(
        default_factory=lambda: {"video": [], "audio": []}
    )
    rtcp: dict[str, list[RtcpObservation]] = field(
        default_factory=lambda: {"video": [], "audio": []}
    )


def _fingerprint(recording: Recording) -> str:
    value = f"{recording.cluster}:{recording.file_path}"
    return hashlib.sha256(value.encode()).hexdigest()[:16]


def _recording(recording: Recording) -> dict[str, Any]:
    return {
        "profile": recording.video_stream,
        "start": recording.start_time.isoformat(),
        "end": recording.end_time.isoformat(),
        "cluster": recording.cluster,
        "fingerprint": _fingerprint(recording),
    }


def _parse_tracks(body: bytes) -> tuple[Track, ...]:
    tracks: list[Track] = []
    section: dict[str, Any] | None = None
    for raw in body.decode("utf-8", errors="replace").splitlines():
        line = raw.strip()
        if line.startswith("m="):
            if section is not None:
                tracks.append(_track(section))
            fields = line[2:].split()
            section = {
                "media": fields[0],
                "payload_type": int(fields[3]),
                "control": None,
                "rtpmap": None,
                "fmtp": [],
            }
        elif section is not None and line.startswith("a=control:"):
            section["control"] = line.removeprefix("a=control:").strip()
        elif section is not None and line.startswith("a=rtpmap:"):
            section["rtpmap"] = line.partition(" ")[2]
        elif section is not None and line.startswith("a=fmtp:"):
            section["fmtp"].append(line.removeprefix("a=fmtp:").strip())
    if section is not None:
        tracks.append(_track(section))
    return tuple(tracks)


def _track(section: dict[str, Any]) -> Track:
    control = section["control"]
    mapping = section["rtpmap"]
    if not control or not mapping:
        raise InvalidResponseError("SDP media section omitted control or rtpmap.")
    parts = mapping.split("/")
    return Track(
        media=section["media"],
        control=control,
        payload_type=section["payload_type"],
        codec=parts[0],
        clock_rate=int(parts[1]),
        channels=int(parts[2]) if len(parts) > 2 else None,
        fmtp=tuple(section["fmtp"]),
    )


class ProbeConnection(_RtspConnection):
    tracks: tuple[Track, ...] = ()

    def _open_and_describe(self) -> Any:
        try:
            self._socket = self._connector(
                (self._host, self._port), timeout=self._timeout
            )
        except OSError as exc:
            raise RecorderConnectionError(
                "Unable to connect to recorder RTSP."
            ) from exc
        self._stream = _RtspStream(self._socket)
        response = self._initial_describe()
        self.tracks = _parse_tracks(response.body)
        video = _parse_sdp_video(response.body)
        self.video_control = video.control
        self.video_codec = video.codec
        self.duration = video.duration
        return response

    def _setup(self, response: Any, track: Track, channels: str) -> None:
        headers = []
        if self.session is not None:
            headers.append(("Session", self.session))
        headers.append(
            ("Transport", f"RTP/AVP/TCP;unicast;interleaved={channels}")
        )
        target = _control_uri(
            self.target, response.header("Content-Base"), track.control
        )
        setup = self._request("SETUP", target, tuple(headers))
        self._require_ok("SETUP", setup)
        transport = setup.header("Transport") or ""
        if f"interleaved={channels}" not in transport.casefold():
            raise InvalidResponseError(f"SETUP did not confirm channels {channels}.")
        returned = setup.header("Session")
        returned_session = returned.partition(";")[0].strip() if returned else ""
        if self.session is None:
            if not returned_session:
                raise InvalidResponseError("First SETUP omitted RTSP Session.")
            self.session = returned_session
        elif returned_session and returned_session != self.session:
            raise InvalidResponseError("Second SETUP changed RTSP Session.")

    def start_dual(self) -> None:
        try:
            response = self._open_and_describe()
            video = self.track("video")
            audio = self.track("audio")
            self._setup(response, video, "0-1")
            self._setup(response, audio, "2-3")
            play = self._session_request("PLAY", (("Range", "npt=0-"),))
            self._require_ok("PLAY", play)
            self.returned_range = play.header("Range")
        except Exception:
            self.close_socket()
            raise

    def start_video_only(self) -> None:
        try:
            response = self._open_and_describe()
            self._setup(response, self.track("video"), "0-1")
            play = self._session_request("PLAY", (("Range", "npt=0-"),))
            self._require_ok("PLAY", play)
            self.returned_range = play.header("Range")
        except Exception:
            self.close_socket()
            raise

    def add_audio(self) -> float:
        started = time.monotonic()
        response = type("Describe", (), {"header": lambda _self, _name: None})()
        self._setup(response, self.track("audio"), "2-3")
        return time.monotonic() - started

    def track(self, media: str) -> Track:
        match = next((item for item in self.tracks if item.media == media), None)
        if match is None:
            raise InvalidResponseError(f"SDP omitted {media} track.")
        return match


def _rtp(channel: int, packet: bytes, now: float) -> RtpObservation | None:
    if len(packet) < 12 or packet[0] >> 6 != 2:
        return None
    csrc_count = packet[0] & 0x0F
    header_bytes = 12 + 4 * csrc_count
    if packet[0] & 0x10:
        if len(packet) < header_bytes + 4:
            return None
        words = int.from_bytes(packet[header_bytes + 2 : header_bytes + 4], "big")
        header_bytes += 4 + 4 * words
    return RtpObservation(
        received_at=now,
        channel=channel,
        version=packet[0] >> 6,
        marker=bool(packet[1] & 0x80),
        payload_type=packet[1] & 0x7F,
        sequence=int.from_bytes(packet[2:4], "big"),
        timestamp=int.from_bytes(packet[4:8], "big"),
        ssrc=int.from_bytes(packet[8:12], "big"),
        payload_bytes=max(0, len(packet) - header_bytes),
    )


def _rtcp(channel: int, packet: bytes, now: float) -> list[RtcpObservation]:
    observations = []
    offset = 0
    names = {200: "SR", 201: "RR", 202: "SDES", 203: "BYE"}
    while offset + 4 <= len(packet):
        version = packet[offset] >> 6
        packet_type = packet[offset + 1]
        size = (int.from_bytes(packet[offset + 2 : offset + 4], "big") + 1) * 4
        if version != 2 or size < 4 or offset + size > len(packet):
            break
        body = packet[offset : offset + size]
        ssrc = int.from_bytes(body[4:8], "big") if len(body) >= 8 else None
        ntp_seconds = ntp_fraction = rtp_timestamp = None
        if packet_type == 200 and len(body) >= 20:
            ntp_seconds = int.from_bytes(body[8:12], "big")
            ntp_fraction = int.from_bytes(body[12:16], "big")
            rtp_timestamp = int.from_bytes(body[16:20], "big")
        observations.append(
            RtcpObservation(
                now, channel, packet_type, names.get(packet_type, "other"), ssrc,
                ntp_seconds, ntp_fraction, rtp_timestamp,
            )
        )
        offset += size
    return observations


def _capture(connection: ProbeConnection, duration: float) -> Capture:
    stream = connection._stream
    if stream is None:
        raise RuntimeError("RTSP stream is not established.")
    result = Capture()
    deadline = time.monotonic() + duration
    while time.monotonic() < deadline:
        try:
            frame = stream._interleaved(deadline)
            if frame is None:
                if stream.buffer.startswith(b"RTSP/"):
                    raise RuntimeError("Unexpected RTSP response during capture.")
                stream._receive(deadline)
                continue
        except socket.timeout:
            break
        channel, packet = frame
        now = time.monotonic()
        media = "video" if channel < 2 else "audio"
        if channel % 2 == 0:
            observation = _rtp(channel, packet, now)
            if observation is not None:
                result.rtp[media].append(observation)
        else:
            result.rtcp[media].extend(_rtcp(channel, packet, now))
    return result


def _summary(values: list[RtpObservation], clock: int) -> dict[str, Any]:
    elapsed = values[-1].received_at - values[0].received_at if len(values) > 1 else 0
    payload = sum(item.payload_bytes for item in values)
    timestamp_delta = ((values[-1].timestamp - values[0].timestamp) & 0xFFFFFFFF) \
        if len(values) > 1 else 0
    sequence_gaps = sum(
        ((right.sequence - left.sequence) & 0xFFFF) != 1
        for left, right in zip(values, values[1:], strict=False)
    )
    return {
        "packet_count": len(values),
        "payload_bytes": payload,
        "first": asdict(values[0]) if values else None,
        "last": asdict(values[-1]) if values else None,
        "ssrcs": sorted({item.ssrc for item in values}),
        "payload_types": sorted({item.payload_type for item in values}),
        "marker_values": sorted({item.marker for item in values}),
        "wall_seconds": elapsed,
        "rtp_progress_seconds": timestamp_delta / clock if clock and values else None,
        "sequence_gap_count": sequence_gaps,
        "packets_per_second": len(values) / elapsed if elapsed else None,
        "payload_bitrate_bps": payload * 8 / elapsed if elapsed else None,
    }


def _rtcp_summary(values: list[RtcpObservation]) -> dict[str, Any]:
    return {
        "packet_count": len(values),
        "types": sorted({item.kind for item in values}),
        "sender_reports": [asdict(item) for item in values if item.kind == "SR"],
    }


def _capture_summary(connection: ProbeConnection, capture: Capture) -> dict[str, Any]:
    return {
        media: {
            "rtp": _summary(capture.rtp[media], connection.track(media).clock_rate),
            "rtcp": _rtcp_summary(capture.rtcp[media]),
        }
        for media in ("video", "audio")
    }


def _first_both(connection: ProbeConnection) -> tuple[Capture, dict[str, float | None]]:
    started = time.monotonic()
    combined = Capture()
    first: dict[str, float | None] = {"video": None, "audio": None}
    while time.monotonic() - started < FIRST_MEDIA_TIMEOUT and None in first.values():
        sample = _capture(connection, 0.1)
        for media in ("video", "audio"):
            combined.rtp[media].extend(sample.rtp[media])
            combined.rtcp[media].extend(sample.rtcp[media])
            if first[media] is None and sample.rtp[media]:
                first[media] = sample.rtp[media][0].received_at - started
    return combined, first


def _connection(client: DahuaClient, recording: Recording) -> ProbeConnection:
    playback = client.media.playback(recording)
    connection = playback._connection
    playback._state = type(playback._state).CLOSED
    client._playbacks.discard(playback)
    return ProbeConnection(
        host=connection._host,
        port=connection._port,
        username=connection._username,
        password=connection._password,
        timeout=connection._timeout,
        file_path=recording.file_path,
    )


def _close(connection: ProbeConnection) -> None:
    try:
        connection.teardown()
    finally:
        connection.close_socket()


def _dual_lifecycle(client: DahuaClient, recording: Recording) -> dict[str, Any]:
    connection = _connection(client, recording)
    try:
        connection.start_dual()
        baseline = _capture(connection, SAMPLE_SECONDS)
        before_pause = _capture_summary(connection, baseline)
        connection.pause()
        paused = _capture(connection, PAUSE_SAMPLE_SECONDS)
        resume_started = time.monotonic()
        connection.play()
        resumed, resume_latency = _first_both(connection)
        resume_completion = time.monotonic() - resume_started
        seek_target = min((connection.duration or 10) * 0.65,
                          max(0.0, (connection.duration or 10) - 1))
        seek_started = time.monotonic()
        connection.play(f"npt={seek_target:g}-")
        sought, seek_latency = _first_both(connection)
        seek_completion = time.monotonic() - seek_started
        return {
            "tracks": [asdict(item) for item in connection.tracks],
            "baseline": before_pause,
            "paused_rtp_packets": {
                media: len(paused.rtp[media]) for media in ("video", "audio")
            },
            "resume": {
                "first_rtp_latency": resume_latency,
                "observation_seconds": resume_completion,
                "capture": _capture_summary(connection, resumed),
            },
            "seek": {
                "npt": seek_target,
                "first_rtp_latency": seek_latency,
                "observation_seconds": seek_completion,
                "capture": _capture_summary(connection, sought),
            },
        }
    finally:
        _close(connection)


def _boundary(
    client: DahuaClient, left: Recording, right: Recording
) -> dict[str, Any]:
    outgoing = _connection(client, left)
    incoming = _connection(client, right)
    try:
        outgoing.start_dual()
        outgoing.play(f"npt={max(0.0, (outgoing.duration or 2) - 2):g}-")
        final_a = _capture(outgoing, SAMPLE_SECONDS)
        incoming.start_dual()
        incoming.play("npt=2-")
        incoming.pause()
        activated = time.monotonic()
        incoming.play()
        first_b, first_latency = _first_both(incoming)
        return {
            "a": _capture_summary(outgoing, final_a),
            "b": _capture_summary(incoming, first_b),
            "activation": {
                "play_completion_and_first_observation_seconds": (
                    time.monotonic() - activated
                ),
                "first_rtp_latency": first_latency,
            },
            "audio_fmtp_identical": (
                outgoing.track("audio").fmtp == incoming.track("audio").fmtp
            ),
        }
    finally:
        _close(outgoing)
        _close(incoming)


def _dynamic_audio(client: DahuaClient, recording: Recording) -> dict[str, Any]:
    connection = _connection(client, recording)
    try:
        connection.start_video_only()
        before = _capture(connection, 0.8)
        setup_seconds = connection.add_audio()
        after, latency = _first_both(connection)
        return {
            "setup_seconds": setup_seconds,
            "before": _capture_summary(connection, before),
            "after": _capture_summary(connection, after),
            "first_rtp_latency_after_setup": latency,
        }
    finally:
        _close(connection)


def _search(client: DahuaClient, camera: Camera) -> list[Recording]:
    end = client.current_time
    with client.media.recordings(
        channel=camera.channel, start=end - timedelta(days=SEARCH_DAYS), end=end
    ) as results:
        return list(results)


def _select(recordings: list[Recording]) -> tuple[Recording, Recording, Recording]:
    main = sorted(
        (item for item in recordings if item.video_stream == "Main"),
        key=lambda item: item.start_time,
    )
    target_a = datetime.fromisoformat("2026-08-17T16:30:00-07:00")
    exact = next((item for item in main if item.start_time == target_a), None)
    if exact is not None:
        index = main.index(exact)
        if index + 1 < len(main):
            left, right = exact, main[index + 1]
        else:
            raise RuntimeError("Exact Main A has no adjacent Main B.")
    else:
        pairs = list(zip(main, main[1:], strict=False))
        if not pairs:
            raise RuntimeError("No adjacent Main recordings found.")
        left, right = min(
            pairs,
            key=lambda pair: abs(
                (pair[1].start_time - pair[0].end_time).total_seconds()
            ),
        )
    extra = [item for item in recordings if item.video_stream == "Extra1"]
    extra_match = min(
        extra,
        key=lambda item: abs((item.start_time - left.start_time).total_seconds()),
        default=None,
    )
    if extra_match is None:
        raise RuntimeError("No matching Extra1 recording found.")
    return left, right, extra_match


def _codecs(profiles: tuple[StreamProfile, ...]) -> dict[str, Any]:
    return {item.kind: {"video": item.codec, "audio": item.audio_codec,
                        "audio_enabled": item.audio_enabled} for item in profiles}


def main() -> None:
    credentials = {name: os.environ.get(name) for name in (
        "DAHUA_HOST", "DAHUA_USERNAME", "DAHUA_PASSWORD"
    )}
    if not all(credentials.values()):
        raise SystemExit("Set DAHUA_HOST, DAHUA_USERNAME, and DAHUA_PASSWORD.")
    with DahuaClient(
        host=credentials["DAHUA_HOST"], username=credentials["DAHUA_USERNAME"],
        password=credentials["DAHUA_PASSWORD"],
    ) as client:
        cameras = tuple(item for item in client.cameras.list() if item.configured)
        camera = next((item for item in cameras if item.name == PREFERRED_CAMERA), None)
        if camera is None:
            raise RuntimeError("Drive Down is not configured.")
        profiles = client.cameras.streams(camera.channel)
        main_a, main_b, extra = _select(_search(client, camera))
        report = {
            "camera": {"name": camera.name, "channel": camera.channel},
            "configured_codecs": _codecs(profiles),
            "recordings": {
                "main_a": _recording(main_a), "main_b": _recording(main_b),
                "extra1": _recording(extra),
            },
            "channels": {"video_rtp": 0, "video_rtcp": 1,
                         "audio_rtp": 2, "audio_rtcp": 3},
            "main_lifecycle": _dual_lifecycle(client, main_a),
            "boundary": _boundary(client, main_a, main_b),
            "extra1": _dual_lifecycle(client, extra),
            "dynamic_audio": _dynamic_audio(client, main_b),
        }
    print(json.dumps(report, indent=2))


if __name__ == "__main__":
    main()
