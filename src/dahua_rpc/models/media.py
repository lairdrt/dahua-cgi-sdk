"""Immutable encoded-media transport models."""

from dataclasses import dataclass
from typing import Literal

MediaType = Literal["video", "audio"]
MediaPacketType = Literal["rtp", "rtcp"]


@dataclass(frozen=True, slots=True)
class MediaTrack:
    """One usable media track discovered in an RTSP SDP description."""

    media_type: MediaType
    control: str
    codec: str | None
    payload_type: int
    clock_rate: int | None
    channels: int | None
    fmtp: tuple[str, ...]
    direction: str | None


@dataclass(frozen=True, slots=True)
class RtcpPacketInfo:
    """Useful metadata from one packet in a compound RTCP payload."""

    packet_type: int
    ssrc: int | None
    ntp_seconds: int | None = None
    ntp_fraction: int | None = None
    rtp_timestamp: int | None = None


@dataclass(frozen=True, slots=True)
class EncodedMediaPacket:
    """One complete encoded RTP or compound RTCP transport packet."""

    media_type: MediaType
    packet_type: MediaPacketType
    interleaved_channel: int
    arrival_time: float
    data: bytes
    payload_type: int | None = None
    marker: bool | None = None
    sequence_number: int | None = None
    rtp_timestamp: int | None = None
    ssrc: int | None = None
    rtcp_packets: tuple[RtcpPacketInfo, ...] = ()
