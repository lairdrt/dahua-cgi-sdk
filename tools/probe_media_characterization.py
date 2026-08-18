"""Characterize recorded SDP and RTP continuity without retaining media."""

from __future__ import annotations

import hashlib
import json
import os
import socket
import time
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta
from types import MethodType
from typing import Any

from dahua_rpc import DahuaClient
from dahua_rpc.models import Camera, Recording, StreamProfile

PREFERRED_CAMERAS = ("Drive Down", "Drive Up")
SEARCH_DAYS = 4
PACKET_WINDOW = 0.8


@dataclass(frozen=True)
class RtpHeader:
    channel: int
    sequence: int
    timestamp: int
    marker: bool
    payload_type: int
    ssrc: int
    codec_init: str | None


def _fingerprint(recording: Recording) -> str:
    material = (
        f"{recording.cluster}:{recording.disk}:{recording.partition}:"
        f"{recording.file_path}"
    )
    return hashlib.sha256(material.encode()).hexdigest()[:16]


def _public_recording(recording: Recording) -> dict[str, Any]:
    return {
        "channel": recording.channel,
        "profile": recording.video_stream,
        "start": recording.start_time.isoformat(),
        "end": recording.end_time.isoformat(),
        "cluster": recording.cluster,
        "media_fingerprint": _fingerprint(recording),
    }


def _search(
    client: DahuaClient, camera: Camera, start: datetime, end: datetime
) -> list[Recording]:
    with client.media.recordings(channel=camera.channel, start=start, end=end) as found:
        return list(found)


def _overlap_seconds(left: Recording, right: Recording) -> float:
    start = max(left.start_time, right.start_time)
    end = min(left.end_time, right.end_time)
    return max(0.0, (end - start).total_seconds())


def _extra_match(main: Recording, recordings: list[Recording]) -> Recording | None:
    candidates = [item for item in recordings if item.video_stream == "Extra1"]
    return max(
        candidates,
        key=lambda item: (
            _overlap_seconds(main, item),
            -abs((main.start_time - item.start_time).total_seconds()),
        ),
        default=None,
    )


def _select(recordings: list[Recording]) -> dict[str, Recording]:
    main = sorted(
        (item for item in recordings if item.video_stream == "Main"),
        key=lambda item: (item.start_time, item.end_time),
    )
    pairs = []
    for left, right in zip(main, main[1:], strict=False):
        extra_left = _extra_match(left, recordings)
        extra_right = _extra_match(right, recordings)
        if extra_left is None or extra_right is None:
            continue
        gap = (right.start_time - left.end_time).total_seconds()
        overlap = min(
            _overlap_seconds(left, extra_left),
            _overlap_seconds(right, extra_right),
        )
        pairs.append((abs(gap), -overlap, -left.start_time.timestamp(), left, right,
                      extra_left, extra_right))
    if not pairs:
        raise RuntimeError("No adjacent Main pair with matching Extra1 recordings.")
    _, _, _, main_a, main_b, extra_a, extra_b = min(pairs)
    return {"main_a": main_a, "main_b": main_b,
            "extra1_a": extra_a, "extra1_b": extra_b}


def _parse_sdp(body: bytes) -> dict[str, Any]:
    session: dict[str, Any] = {"range": [], "direction": []}
    media: list[dict[str, Any]] = []
    current: dict[str, Any] | None = None
    for raw in body.decode("utf-8", errors="replace").splitlines():
        line = raw.strip()
        if line.startswith("m="):
            fields = line[2:].split()
            current = {
                "m": line[2:],
                "media": fields[0] if fields else None,
                "payload_types": fields[3:] if len(fields) > 3 else [],
                "control": [], "rtpmap": [], "fmtp": [],
                "range": [], "direction": [],
            }
            media.append(current)
            continue
        target = current if current is not None else session
        for prefix, name in (
            ("a=control:", "control"), ("a=rtpmap:", "rtpmap"),
            ("a=fmtp:", "fmtp"), ("a=range:", "range"),
        ):
            if line.startswith(prefix):
                target.setdefault(name, []).append(line.removeprefix(prefix))
                break
        else:
            if line in {"a=sendonly", "a=recvonly", "a=sendrecv", "a=inactive"}:
                target.setdefault("direction", []).append(line[2:])
    return {"session": session, "media": media}


def _capture_sdp(playback: Any) -> dict[str, Any]:
    connection = playback._connection
    original = connection._initial_describe
    captured: dict[str, bytes] = {}

    def wrapped(self: Any) -> Any:
        response = original()
        captured["body"] = response.body
        return response

    connection._initial_describe = MethodType(wrapped, connection)
    playback.start()
    if "body" not in captured:
        raise RuntimeError("DESCRIBE SDP was not captured.")
    return _parse_sdp(captured["body"])


def _codec_for(profile: str, profiles: tuple[StreamProfile, ...]) -> str:
    kind = "main" if profile == "Main" else "sub"
    match = next((item for item in profiles if item.kind == kind), None)
    if match is None:
        raise RuntimeError(f"Encode metadata omitted {profile}.")
    return match.codec


def _codec_init(codec: str | None, payload: bytes, header_size: int) -> str | None:
    if len(payload) <= header_size:
        return None
    data = payload[header_size:]
    if codec and codec.casefold() in {"h264", "h.264"}:
        kind = data[0] & 0x1F
        return {7: "SPS", 8: "PPS"}.get(kind)
    if codec and codec.casefold() in {"h265", "h.265", "hevc"}:
        kind = (data[0] >> 1) & 0x3F
        return {32: "VPS", 33: "SPS", 34: "PPS"}.get(kind)
    return None


def _rtp_header(channel: int, packet: bytes, codec: str | None) -> RtpHeader | None:
    if len(packet) < 12 or packet[0] >> 6 != 2:
        return None
    csrc_count = packet[0] & 0x0F
    header_size = 12 + 4 * csrc_count
    if packet[0] & 0x10:
        if len(packet) < header_size + 4:
            return None
        extension_words = int.from_bytes(packet[header_size + 2:header_size + 4], "big")
        header_size += 4 + 4 * extension_words
    return RtpHeader(
        channel=channel,
        sequence=int.from_bytes(packet[2:4], "big"),
        timestamp=int.from_bytes(packet[4:8], "big"),
        marker=bool(packet[1] & 0x80),
        payload_type=packet[1] & 0x7F,
        ssrc=int.from_bytes(packet[8:12], "big"),
        codec_init=_codec_init(codec, packet, header_size),
    )


def _packets(playback: Any, duration: float = PACKET_WINDOW) -> list[RtpHeader]:
    stream = playback._connection._stream
    if stream is None:
        raise RuntimeError("RTSP stream is not established.")
    deadline = time.monotonic() + duration
    headers: list[RtpHeader] = []
    while time.monotonic() < deadline:
        try:
            frame = stream._interleaved(deadline)
            if frame is None:
                if stream.buffer.startswith(b"RTSP/"):
                    raise RuntimeError("Unexpected RTSP response during media capture.")
                stream._receive(deadline)
                continue
        except socket.timeout:
            break
        channel, packet = frame
        if channel != 0:
            continue
        header = _rtp_header(channel, packet, playback.video_codec)
        if header is not None:
            headers.append(header)
    return headers


def _packet_summary(headers: list[RtpHeader]) -> dict[str, Any]:
    return {
        "count": len(headers),
        "first": asdict(headers[0]) if headers else None,
        "last": asdict(headers[-1]) if headers else None,
        "ssrcs": sorted({item.ssrc for item in headers}),
        "payload_types": sorted({item.payload_type for item in headers}),
        "interleaved_channels": sorted({item.channel for item in headers}),
        "codec_init_seen": sorted({item.codec_init for item in headers
                                   if item.codec_init is not None}),
    }


def _describe(client: DahuaClient, recording: Recording) -> dict[str, Any]:
    with client.media.playback(recording) as playback:
        sdp = _capture_sdp(playback)
        return {
            "recording": _public_recording(recording),
            "sdp_codec": playback.video_codec,
            "sdp": sdp,
        }


def _seek_probe(client: DahuaClient, recording: Recording) -> dict[str, Any]:
    with client.media.playback(recording) as playback:
        _capture_sdp(playback)
        before = _packets(playback)
        duration = playback.duration
        if duration is None or duration < 4:
            raise RuntimeError("Recording is too short for a substantial seek.")
        target = min(duration - 1, max(2.0, duration * 0.65))
        playback.seek(target)
        after = _packets(playback)
        return {"seek_npt": target, "before": _packet_summary(before),
                "after": _packet_summary(after)}


def _boundary_probe(
    client: DahuaClient, left: Recording, right: Recording
) -> dict[str, Any]:
    with client.media.playback(left) as outgoing:
        _capture_sdp(outgoing)
        duration = outgoing.duration
        if duration is None:
            raise RuntimeError("Outgoing SDP omitted duration.")
        outgoing.seek(max(0.0, duration - 2.0))
        final_a = _packets(outgoing)
        with client.media.playback(right) as incoming:
            _capture_sdp(incoming)
            incoming.seek(min(2.0, max(0.0, (incoming.duration or 2.0) - 0.1)))
            incoming.pause()
            incoming.resume()
            first_b = _packets(incoming)
    return {"final_a": _packet_summary(final_a),
            "first_b": _packet_summary(first_b)}


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
        camera = next((item for name in PREFERRED_CAMERAS for item in cameras
                       if item.name == name), None)
        if camera is None:
            raise RuntimeError("Preferred configured camera was not found.")
        profiles = client.cameras.streams(camera.channel)
        end = client.current_time
        selected = _select(
            _search(client, camera, end - timedelta(days=SEARCH_DAYS), end)
        )
        descriptions = {}
        for name, recording in selected.items():
            item = _describe(client, recording)
            item["configured_codec"] = _codec_for(recording.video_stream, profiles)
            descriptions[name] = item
        report = {
            "camera": {"name": camera.name, "channel": camera.channel},
            "encode_profiles": [asdict(item) for item in profiles],
            "recordings": descriptions,
            "seek": _seek_probe(client, selected["main_a"]),
            "boundary": _boundary_probe(
                client, selected["main_a"], selected["main_b"]
            ),
        }
    print(json.dumps(report, indent=2))


if __name__ == "__main__":
    main()
