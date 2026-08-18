"""Internal RTP continuity, RTCP, and codec compatibility primitives."""

from __future__ import annotations

import base64
import secrets
import time
from dataclasses import dataclass

from .models import EncodedMediaPacket, MediaTrack

MAX_PARAMETER_SET_PAYLOAD = 1400


class CodecCompatibilityError(RuntimeError):
    """An incoming track cannot continue an established presentation."""


@dataclass(frozen=True, slots=True)
class CodecInitialization:
    codec: str
    parameter_sets: tuple[bytes, ...]


@dataclass(frozen=True, slots=True)
class TrackConfiguration:
    media_type: str
    codec: str
    clock_rate: int
    channels: int | None
    payload_type: int
    initialization: CodecInitialization | None


class CodecConfigurationCache:
    """SDP-authoritative H.264/H.265 initialization and compatibility."""

    def __init__(self, tracks: tuple[MediaTrack, ...]) -> None:
        self._values = {
            track.media_type: _codec_initialization(track) for track in tracks
        }

    def get(self, media_type: str) -> CodecInitialization | None:
        return self._values.get(media_type)


def track_configurations(
    tracks: tuple[MediaTrack, ...],
) -> dict[str, TrackConfiguration]:
    cache = CodecConfigurationCache(tracks)
    result: dict[str, TrackConfiguration] = {}
    for track in tracks:
        if track.codec is None or track.clock_rate is None:
            raise CodecCompatibilityError(
                f"{track.media_type} lacks codec or clock-rate metadata"
            )
        if track.media_type in result:
            raise CodecCompatibilityError(
                f"multiple {track.media_type} tracks are not supported"
            )
        result[track.media_type] = TrackConfiguration(
            track.media_type,
            track.codec.casefold(),
            track.clock_rate,
            track.channels,
            track.payload_type,
            cache.get(track.media_type),
        )
    return result


def require_compatible(
    established: dict[str, TrackConfiguration],
    incoming_tracks: tuple[MediaTrack, ...],
) -> None:
    incoming = track_configurations(incoming_tracks)
    if set(incoming) != set(established):
        raise CodecCompatibilityError("incoming media track set changed")
    for media_type, expected in established.items():
        actual = incoming[media_type]
        if (
            actual.media_type,
            actual.codec,
            actual.clock_rate,
            actual.channels,
            actual.initialization,
        ) != (
            expected.media_type,
            expected.codec,
            expected.clock_rate,
            expected.channels,
            expected.initialization,
        ):
            raise CodecCompatibilityError(
                f"incompatible {media_type} track at file boundary"
            )


def _codec_initialization(track: MediaTrack) -> CodecInitialization | None:
    codec = (track.codec or "").upper().replace(".", "")
    if codec == "H264":
        names = ("sprop-parameter-sets",)
    elif codec == "H265":
        names = ("sprop-vps", "sprop-sps", "sprop-pps")
    else:
        return None
    parameters: dict[str, str] = {}
    for line in track.fmtp:
        _, _, body = line.partition(" ")
        for item in body.split(";"):
            name, separator, value = item.strip().partition("=")
            if separator:
                parameters[name.casefold()] = value.strip()
    encoded: list[str] = []
    for name in names:
        value = parameters.get(name)
        if value is None:
            return None
        encoded.extend(part.strip() for part in value.split(",") if part.strip())
    try:
        decoded = tuple(base64.b64decode(value, validate=True) for value in encoded)
    except (ValueError, base64.binascii.Error) as exc:
        raise CodecCompatibilityError(
            f"invalid {codec} parameter-set data in SDP"
        ) from exc
    if not decoded or any(not value for value in decoded):
        return None
    return CodecInitialization(codec, decoded)


class RtpContinuityMapper:
    """Map one changing upstream track into one stable downstream domain."""

    def __init__(
        self,
        clock_rate: int,
        *,
        payload_type: int = 96,
        ssrc: int | None = None,
        sequence: int | None = None,
    ) -> None:
        if clock_rate <= 0:
            raise ValueError("clock_rate must be positive")
        if not 0 <= payload_type <= 127:
            raise ValueError("payload_type must be between 0 and 127")
        self.clock_rate = clock_rate
        self.payload_type = payload_type
        self.ssrc = secrets.randbits(32) if ssrc is None else ssrc
        self.next_sequence = secrets.randbits(16) if sequence is None else sequence
        self.last_upstream_timestamp: int | None = None
        self.last_upstream_ssrc: int | None = None
        self.last_downstream_timestamp: int | None = None
        self.last_arrival: float | None = None
        self.pending_discontinuity = False
        self.discontinuity_time: float | None = None
        self.needs_sender_report = False

    def discontinuity(self, at: float | None = None) -> None:
        self.pending_discontinuity = True
        self.discontinuity_time = at
        self.needs_sender_report = True

    def rewrite(self, packet: EncodedMediaPacket) -> bytes:
        if (
            packet.packet_type != "rtp"
            or packet.rtp_timestamp is None
            or len(packet.data) < 12
        ):
            return packet.data
        timestamp = self._map_timestamp(packet.rtp_timestamp, packet.arrival_time)
        self.last_upstream_ssrc = packet.ssrc
        data = bytearray(packet.data)
        data[1] = (data[1] & 0x80) | self.payload_type
        data[2:4] = self.next_sequence.to_bytes(2, "big")
        data[4:8] = timestamp.to_bytes(4, "big")
        data[8:12] = self.ssrc.to_bytes(4, "big")
        self.next_sequence = (self.next_sequence + 1) & 0xFFFF
        return bytes(data)

    def initialization_packets(
        self, timestamp: int, parameter_sets: tuple[bytes, ...]
    ) -> tuple[bytes, ...]:
        packets = []
        for payload in parameter_sets:
            if len(payload) > MAX_PARAMETER_SET_PAYLOAD:
                raise CodecCompatibilityError(
                    "parameter set exceeds the single-NAL RTP limit"
                )
            header = bytearray(12)
            header[0] = 0x80
            header[1] = self.payload_type
            header[2:4] = self.next_sequence.to_bytes(2, "big")
            header[4:8] = timestamp.to_bytes(4, "big")
            header[8:12] = self.ssrc.to_bytes(4, "big")
            packets.append(bytes(header) + payload)
            self.next_sequence = (self.next_sequence + 1) & 0xFFFF
        return tuple(packets)

    def translate_rtcp_timestamp(
        self, upstream_ssrc: int, upstream_timestamp: int
    ) -> int | None:
        if self.pending_discontinuity or self.last_downstream_timestamp is None:
            return None
        if upstream_ssrc != self.last_upstream_ssrc:
            return None
        assert self.last_upstream_timestamp is not None
        delta = (upstream_timestamp - self.last_upstream_timestamp) & 0xFFFFFFFF
        if delta > self.clock_rate * 2:
            return None
        return (self.last_downstream_timestamp + delta) & 0xFFFFFFFF

    def generated_sender_report(self, now: float | None = None) -> bytes | None:
        if not self.needs_sender_report or self.last_downstream_timestamp is None:
            return None
        unix_time = time.time() if now is None else now
        ntp = unix_time + 2_208_988_800
        seconds = int(ntp)
        fraction = int((ntp - seconds) * (1 << 32))
        self.needs_sender_report = False
        return (
            b"\x80\xc8\x00\x06"
            + self.ssrc.to_bytes(4, "big")
            + seconds.to_bytes(4, "big")
            + fraction.to_bytes(4, "big")
            + self.last_downstream_timestamp.to_bytes(4, "big")
            + b"\x00" * 8
        )

    def _map_timestamp(self, upstream: int, arrival: float) -> int:
        if self.last_downstream_timestamp is None:
            downstream = secrets.randbits(32)
        elif self.pending_discontinuity:
            anchor = (
                arrival
                if self.discontinuity_time is None
                else self.discontinuity_time
            )
            downstream = (
                self.last_downstream_timestamp + self._arrival_increment(anchor)
            ) & 0xFFFFFFFF
        else:
            assert self.last_upstream_timestamp is not None
            delta = (upstream - self.last_upstream_timestamp) & 0xFFFFFFFF
            if delta <= self.clock_rate * 2:
                downstream = (self.last_downstream_timestamp + delta) & 0xFFFFFFFF
            else:
                downstream = (
                    self.last_downstream_timestamp + self._arrival_increment(arrival)
                ) & 0xFFFFFFFF
        self.last_upstream_timestamp = upstream
        self.last_downstream_timestamp = downstream
        self.last_arrival = arrival
        self.pending_discontinuity = False
        self.discontinuity_time = None
        return downstream

    def _arrival_increment(self, arrival: float) -> int:
        if self.last_arrival is None:
            return 1
        elapsed = min(max(arrival - self.last_arrival, 0.0), 0.25)
        return max(1, round(elapsed * self.clock_rate))


def rewrite_rtcp(data: bytes, mapper: RtpContinuityMapper) -> bytes | None:
    """Translate valid Sender Reports and discard upstream-domain RTCP."""
    rewritten = bytearray()
    offset = 0
    while offset + 4 <= len(data):
        if data[offset] >> 6 != 2:
            return None
        packet_type = data[offset + 1]
        length = (int.from_bytes(data[offset + 2 : offset + 4], "big") + 1) * 4
        if length < 4 or offset + length > len(data):
            return None
        if packet_type == 200:
            if length < 28:
                return None
            packet = bytearray(data[offset : offset + length])
            upstream_ssrc = int.from_bytes(packet[4:8], "big")
            upstream_timestamp = int.from_bytes(packet[16:20], "big")
            downstream = mapper.translate_rtcp_timestamp(
                upstream_ssrc, upstream_timestamp
            )
            if downstream is None:
                return None
            packet[4:8] = mapper.ssrc.to_bytes(4, "big")
            packet[16:20] = downstream.to_bytes(4, "big")
            packet[0] &= 0xE0
            packet[2:4] = (6).to_bytes(2, "big")
            rewritten.extend(packet[:28])
        offset += length
    if offset != len(data) or not rewritten:
        return None
    return bytes(rewritten)
