"""Internal synchronous RTSP transport for live and recorded video."""

from __future__ import annotations

import hashlib
import re
import secrets
import socket
import time
from collections.abc import Callable
from dataclasses import dataclass
from urllib.parse import urlsplit

from .exceptions import (
    AuthenticationError,
    InvalidResponseError,
    RecorderConnectionError,
    TransportError,
)
from .models import EncodedMediaPacket, MediaTrack, RtcpPacketInfo

_MAX_MESSAGE_BYTES = 256 * 1024


@dataclass(frozen=True, slots=True)
class _RtspResponse:
    status_code: int
    headers: tuple[tuple[str, str], ...]
    body: bytes

    def header(self, name: str) -> str | None:
        wanted = name.casefold()
        return next(
            (value for key, value in self.headers if key.casefold() == wanted), None
        )


@dataclass(frozen=True, slots=True)
class _SdpDescription:
    tracks: tuple[MediaTrack, ...]
    duration: float | None


@dataclass(frozen=True, slots=True)
class _MediaReceipt:
    packets: int
    bytes: int
    first_timestamp: int | None
    last_timestamp: int | None


class _RtspStream:
    def __init__(self, connection: socket.socket) -> None:
        self.connection = connection
        self.buffer = bytearray()

    def _receive(self, deadline: float) -> None:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise socket.timeout
        self.connection.settimeout(remaining)
        chunk = self.connection.recv(4096)
        if not chunk:
            raise InvalidResponseError("RTSP connection closed unexpectedly.")
        self.buffer.extend(chunk)
        if len(self.buffer) > _MAX_MESSAGE_BYTES:
            raise InvalidResponseError("Buffered RTSP data exceeded the size limit.")

    def _interleaved(self, deadline: float) -> tuple[int, bytes] | None:
        if not self.buffer or self.buffer[0] != ord("$"):
            return None
        while len(self.buffer) < 4:
            self._receive(deadline)
        length = int.from_bytes(self.buffer[2:4], "big")
        while len(self.buffer) < 4 + length:
            self._receive(deadline)
        channel = self.buffer[1]
        payload = bytes(self.buffer[4 : 4 + length])
        del self.buffer[: 4 + length]
        return channel, payload

    def response(self, timeout: float) -> _RtspResponse:
        deadline = time.monotonic() + timeout
        try:
            while True:
                packet = self._interleaved(deadline)
                if packet is not None:
                    continue
                if self.buffer and not self.buffer.startswith(b"RTSP/"):
                    raise InvalidResponseError(
                        "Unexpected data while awaiting an RTSP response."
                    )
                header_end = self.buffer.find(b"\r\n\r\n")
                if header_end < 0:
                    self._receive(deadline)
                    continue
                header = bytes(self.buffer[:header_end])
                lines = header.decode("iso-8859-1").split("\r\n")
                status = re.fullmatch(r"RTSP/\d\.\d\s+(\d{3})(?:\s+.*)?", lines[0])
                if status is None:
                    raise InvalidResponseError(
                        "Recorder returned malformed RTSP status."
                    )
                headers: list[tuple[str, str]] = []
                for line in lines[1:]:
                    name, separator, value = line.partition(":")
                    if not separator:
                        raise InvalidResponseError(
                            "Recorder returned a malformed RTSP header."
                        )
                    headers.append((name.strip(), value.strip()))
                length_text = next(
                    (
                        value
                        for name, value in headers
                        if name.casefold() == "content-length"
                    ),
                    "0",
                )
                try:
                    length = int(length_text)
                except ValueError as exc:
                    raise InvalidResponseError(
                        "Recorder returned an invalid RTSP Content-Length."
                    ) from exc
                if length < 0 or length > _MAX_MESSAGE_BYTES:
                    raise InvalidResponseError(
                        "Recorder returned an unsupported RTSP Content-Length."
                    )
                message_end = header_end + 4 + length
                while len(self.buffer) < message_end:
                    self._receive(deadline)
                body = bytes(self.buffer[header_end + 4 : message_end])
                del self.buffer[:message_end]
                return _RtspResponse(int(status.group(1)), tuple(headers), body)
        except socket.timeout as exc:
            raise TransportError("Timed out waiting for an RTSP response.") from exc
        except OSError as exc:
            raise TransportError("RTSP transport failed while receiving data.") from exc

    def media(self, duration: float) -> _MediaReceipt:
        packets = self.media_packets(duration, {0: ("video", "rtp")})
        video = [packet for packet in packets if packet.packet_type == "rtp"]
        timestamps = [
            packet.rtp_timestamp
            for packet in video
            if packet.rtp_timestamp is not None
        ]
        return _MediaReceipt(
            len(video),
            sum(len(packet.data) for packet in video),
            timestamps[0] if timestamps else None,
            timestamps[-1] if timestamps else None,
        )

    def media_packets(
        self,
        duration: float,
        channels: dict[int, tuple[str, str]],
    ) -> tuple[EncodedMediaPacket, ...]:
        deadline = time.monotonic() + duration
        packets: list[EncodedMediaPacket] = []
        while time.monotonic() < deadline:
            try:
                packet = self._interleaved(deadline)
                if packet is None:
                    if self.buffer.startswith(b"RTSP/"):
                        raise InvalidResponseError(
                            "Unexpected RTSP response while receiving media."
                        )
                    self._receive(deadline)
                    continue
            except socket.timeout:
                break
            except OSError as exc:
                raise TransportError("RTSP media transport failed.") from exc
            channel, payload = packet
            identity = channels.get(channel)
            if identity is None or not payload:
                continue
            media_type, packet_type = identity
            packets.append(
                _parse_media_packet(
                    media_type,
                    packet_type,
                    channel,
                    time.monotonic(),
                    payload,
                )
            )
        return tuple(packets)


class _DigestState:
    def __init__(self, challenge: dict[str, str]) -> None:
        self.challenge = challenge
        self.nonce_count = 0

    def update(self, challenge: dict[str, str]) -> None:
        self.challenge = challenge
        self.nonce_count = 0

    def authorization(
        self, username: str, password: str, method: str, uri: str
    ) -> str:
        self.nonce_count += 1
        return _authorization(
            username, password, method, uri, self.challenge, self.nonce_count
        )


class _RtspConnection:
    """Own one RTSP connection and TCP-interleaved video session."""

    def __init__(
        self,
        *,
        host: str,
        port: int,
        username: str,
        password: str,
        timeout: float,
        file_path: str | None = None,
        target_path: str | None = None,
        initial_range: str | None = "npt=0-",
        include_audio: bool = False,
        connector: Callable[..., socket.socket] = socket.create_connection,
    ) -> None:
        if (file_path is None) == (target_path is None):
            raise ValueError("exactly one of file_path or target_path is required")
        self._host = host
        self._port = port
        self._username = username
        self._password = password
        self._timeout = timeout
        self._connector = connector
        path = f"/{file_path}" if file_path is not None else target_path
        self.target = f"rtsp://{host}:{port}{path}"
        self._initial_range = initial_range
        self._include_audio = include_audio
        self._socket: socket.socket | None = None
        self._stream: _RtspStream | None = None
        self._digest: _DigestState | None = None
        self._cseq = 1
        self.session: str | None = None
        self.video_control: str | None = None
        self.video_codec: str | None = None
        self.media_tracks: tuple[MediaTrack, ...] = ()
        self._media_channels: dict[int, tuple[str, str]] = {}
        self.duration: float | None = None
        self.returned_range: str | None = None

    def start(self) -> None:
        try:
            self._socket = self._connector(
                (self._host, self._port), timeout=self._timeout
            )
        except socket.timeout as exc:
            raise TransportError("Timed out connecting to the RTSP server.") from exc
        except OSError as exc:
            raise RecorderConnectionError(
                f"Unable to connect to recorder RTSP at {self._host}:{self._port}."
            ) from exc
        self._stream = _RtspStream(self._socket)
        try:
            describe = self._initial_describe()
            description = _parse_sdp(describe.body)
            video = _require_track(description.tracks, "video")
            selected = [video]
            if self._include_audio:
                selected.append(_require_track(description.tracks, "audio"))
            self.media_tracks = description.tracks
            self.video_control = video.control
            self.video_codec = video.codec
            self.duration = description.duration
            for index, track in enumerate(selected):
                self._setup_track(describe, track, index * 2)
            play_headers = (
                (("Range", self._initial_range),)
                if self._initial_range is not None
                else ()
            )
            play = self._session_request("PLAY", play_headers)
            self._require_ok("PLAY", play)
            self.returned_range = play.header("Range")
        except Exception:
            self.close_socket()
            raise

    def pause(self) -> str | None:
        response = self._session_request("PAUSE")
        self._require_ok("PAUSE", response)
        return response.header("Range")

    def play(self, range_value: str | None = None) -> str | None:
        headers = (("Range", range_value),) if range_value is not None else ()
        response = self._session_request("PLAY", headers)
        self._require_ok("PLAY", response)
        returned_range = response.header("Range")
        if returned_range:
            self.returned_range = returned_range
        return returned_range

    def receive(self, duration: float) -> _MediaReceipt:
        if self._stream is None:
            raise InvalidResponseError("RTSP stream is not established.")
        return self._stream.media(duration)

    def receive_packets(self, duration: float) -> tuple[EncodedMediaPacket, ...]:
        if self._stream is None:
            raise InvalidResponseError("RTSP stream is not established.")
        return self._stream.media_packets(duration, self._media_channels)

    def _setup_track(
        self, describe: _RtspResponse, track: MediaTrack, rtp_channel: int
    ) -> None:
        channels = f"{rtp_channel}-{rtp_channel + 1}"
        track_uri = _control_uri(
            self.target, describe.header("Content-Base"), track.control
        )
        headers: tuple[tuple[str, str], ...] = (
            ("Transport", f"RTP/AVP/TCP;unicast;interleaved={channels}"),
        )
        if self.session is not None:
            headers = (("Session", self.session), *headers)
        setup = self._request("SETUP", track_uri, headers)
        self._require_ok("SETUP", setup)
        transport = setup.header("Transport")
        if transport is None or f"interleaved={channels}" not in transport.casefold():
            raise InvalidResponseError(
                f"SETUP did not confirm interleaved channels {channels}."
            )
        session_header = setup.header("Session")
        returned = session_header.partition(";")[0].strip() if session_header else ""
        if self.session is None:
            if not returned:
                raise InvalidResponseError("SETUP omitted a usable RTSP Session.")
            self.session = returned
        elif returned and returned != self.session:
            raise InvalidResponseError("SETUP changed the RTSP Session.")
        self._media_channels[rtp_channel] = (track.media_type, "rtp")
        self._media_channels[rtp_channel + 1] = (track.media_type, "rtcp")

    def teardown(self) -> None:
        if self.session is None or self._stream is None:
            return
        response = self._session_request("TEARDOWN")
        self._require_ok("TEARDOWN", response)
        self.session = None

    def close_socket(self) -> None:
        connection = self._socket
        self._socket = None
        self._stream = None
        if connection is not None:
            try:
                connection.close()
            except OSError:
                pass

    def _initial_describe(self) -> _RtspResponse:
        if self._stream is None:
            raise InvalidResponseError("RTSP stream is not established.")
        self._send("DESCRIBE", self.target, None, (("Accept", "application/sdp"),))
        challenge_response = self._stream.response(self._timeout)
        if challenge_response.status_code != 401:
            if challenge_response.status_code == 200:
                return challenge_response
            raise InvalidResponseError(
                f"DESCRIBE returned RTSP status {challenge_response.status_code}."
            )
        authenticate = challenge_response.header("WWW-Authenticate")
        if authenticate is None:
            raise AuthenticationError("RTSP authentication challenge was incomplete.")
        self._digest = _DigestState(_parse_digest_challenge(authenticate))
        response = self._request(
            "DESCRIBE", self.target, (("Accept", "application/sdp"),)
        )
        self._require_ok("DESCRIBE", response)
        return response

    def _session_request(
        self, method: str, headers: tuple[tuple[str, str], ...] = ()
    ) -> _RtspResponse:
        if self.session is None:
            raise InvalidResponseError("RTSP session is not established.")
        return self._request(method, self.target, (("Session", self.session), *headers))

    def _request(
        self,
        method: str,
        target: str,
        headers: tuple[tuple[str, str], ...] = (),
    ) -> _RtspResponse:
        if self._stream is None or self._digest is None:
            raise InvalidResponseError("RTSP authentication is not established.")
        digest_uri = _digest_uri(target)
        for attempt in range(2):
            authorization = self._digest.authorization(
                self._username, self._password, method, digest_uri
            )
            self._send(method, target, authorization, headers)
            response = self._stream.response(self._timeout)
            if response.status_code != 401:
                return response
            authenticate = response.header("WWW-Authenticate")
            if authenticate is None:
                raise AuthenticationError("RTSP authentication failed.")
            replacement = _parse_digest_challenge(authenticate)
            nonce_changed = replacement["nonce"] != self._digest.challenge["nonce"]
            stale = replacement.get("stale", "false").casefold() == "true"
            if attempt == 0 and (nonce_changed or stale):
                self._digest.update(replacement)
                continue
            raise AuthenticationError("RTSP authentication failed.")
        raise AssertionError("unreachable")

    def _send(
        self,
        method: str,
        target: str,
        authorization: str | None,
        headers: tuple[tuple[str, str], ...],
    ) -> None:
        if self._socket is None:
            raise InvalidResponseError("RTSP socket is not established.")
        request = _build_request(method, target, self._cseq, authorization, headers)
        self._cseq += 1
        try:
            self._socket.sendall(request)
        except socket.timeout as exc:
            raise TransportError("Timed out sending an RTSP request.") from exc
        except OSError as exc:
            raise TransportError("Failed to send an RTSP request.") from exc

    @staticmethod
    def _require_ok(method: str, response: _RtspResponse) -> None:
        if response.status_code == 401:
            raise AuthenticationError("RTSP authentication failed.")
        if response.status_code != 200:
            raise InvalidResponseError(
                f"{method} returned RTSP status {response.status_code}."
            )


def _parse_digest_challenge(value: str) -> dict[str, str]:
    scheme, separator, parameters = value.partition(" ")
    if not separator or scheme.casefold() != "digest":
        raise AuthenticationError("Recorder returned unsupported RTSP authentication.")
    pairs = re.findall(r'(\w+)\s*=\s*(?:"((?:\\.|[^"])*)"|([^,\s]+))', parameters)
    challenge = {
        key.casefold(): re.sub(r"\\(.)", r"\1", quoted) if quoted else bare
        for key, quoted, bare in pairs
    }
    if "realm" not in challenge or "nonce" not in challenge:
        raise AuthenticationError("RTSP authentication challenge was incomplete.")
    return challenge


def _authorization(
    username: str,
    password: str,
    method: str,
    uri: str,
    challenge: dict[str, str],
    nonce_count: int,
) -> str:
    if challenge.get("algorithm", "MD5").casefold() != "md5":
        raise AuthenticationError("Recorder requires unsupported RTSP authentication.")
    realm = challenge["realm"]
    nonce = challenge["nonce"]
    ha1 = hashlib.md5(f"{username}:{realm}:{password}".encode()).hexdigest()
    ha2 = hashlib.md5(f"{method}:{uri}".encode()).hexdigest()
    fields = [
        f'username="{_escape(username)}"',
        f'realm="{_escape(realm)}"',
        f'nonce="{_escape(nonce)}"',
        f'uri="{_escape(uri)}"',
        "algorithm=MD5",
    ]
    qop = challenge.get("qop")
    if qop is not None:
        if "auth" not in (choice.strip().casefold() for choice in qop.split(",")):
            raise AuthenticationError(
                "Recorder requires unsupported RTSP authentication."
            )
        count = f"{nonce_count:08x}"
        cnonce = secrets.token_hex(8)
        response = hashlib.md5(
            f"{ha1}:{nonce}:{count}:{cnonce}:auth:{ha2}".encode()
        ).hexdigest()
        fields.extend(("qop=auth", f"nc={count}", f'cnonce="{cnonce}"'))
    else:
        response = hashlib.md5(f"{ha1}:{nonce}:{ha2}".encode()).hexdigest()
    fields.append(f'response="{response}"')
    if opaque := challenge.get("opaque"):
        fields.append(f'opaque="{_escape(opaque)}"')
    return "Digest " + ", ".join(fields)


def _escape(value: str) -> str:
    return value.replace("\\", "\\\\").replace('"', '\\"')


def _build_request(
    method: str,
    target: str,
    cseq: int,
    authorization: str | None,
    headers: tuple[tuple[str, str], ...],
) -> bytes:
    lines = [
        f"{method} {target} RTSP/1.0",
        f"CSeq: {cseq}",
        "User-Agent: dahua-rpc-sdk/rtsp-playback",
    ]
    lines.extend(f"{name}: {value}" for name, value in headers)
    if authorization is not None:
        lines.append(f"Authorization: {authorization}")
    return ("\r\n".join(lines) + "\r\n\r\n").encode("ascii")


def _digest_uri(target: str) -> str:
    parsed = urlsplit(target)
    return parsed.path + (("?" + parsed.query) if parsed.query else "")


def _parse_sdp(body: bytes) -> _SdpDescription:
    duration: float | None = None
    sections: list[dict[str, object]] = []
    current: dict[str, object] | None = None
    for raw_line in body.decode("utf-8", errors="replace").splitlines():
        line = raw_line.strip()
        range_match = re.fullmatch(r"a=range:npt=[^-]*-(\d+(?:\.\d+)?)", line)
        if range_match is not None:
            duration = float(range_match.group(1))
        if line.startswith("m="):
            fields = line[2:].split()
            current = {
                "media_type": fields[0] if fields else "",
                "payload_type": fields[3] if len(fields) > 3 else "",
                "control": None,
                "rtpmap": None,
                "fmtp": [],
                "direction": None,
            }
            sections.append(current)
        elif current is not None and line.startswith("a=control:"):
            current["control"] = line.removeprefix("a=control:").strip()
        elif current is not None and line.startswith("a=rtpmap:"):
            current["rtpmap"] = line.partition(" ")[2]
        elif current is not None and line.startswith("a=fmtp:"):
            fmtp = current["fmtp"]
            assert isinstance(fmtp, list)
            fmtp.append(line.removeprefix("a=fmtp:").strip())
        elif current is not None and line in {
            "a=sendonly",
            "a=recvonly",
            "a=sendrecv",
            "a=inactive",
        }:
            current["direction"] = line[2:]
    tracks = tuple(
        track
        for section in sections
        if (track := _parse_sdp_track(section)) is not None
    )
    return _SdpDescription(tracks, duration)


def _parse_sdp_track(section: dict[str, object]) -> MediaTrack | None:
    media_type = section["media_type"]
    if media_type not in ("video", "audio"):
        return None
    control = section["control"]
    if not isinstance(control, str) or not control or control == "*":
        return None
    try:
        payload_type = int(str(section["payload_type"]))
    except ValueError:
        return None
    codec = None
    clock_rate = None
    channels = None
    mapping = section["rtpmap"]
    if isinstance(mapping, str) and mapping:
        parts = mapping.split("/")
        codec = parts[0] or None
        try:
            clock_rate = int(parts[1]) if len(parts) > 1 else None
            channels = int(parts[2]) if len(parts) > 2 else None
        except ValueError:
            clock_rate = None
            channels = None
    fmtp = section["fmtp"]
    assert isinstance(fmtp, list)
    direction = section["direction"]
    return MediaTrack(
        media_type=media_type,
        control=control,
        codec=codec,
        payload_type=payload_type,
        clock_rate=clock_rate,
        channels=channels,
        fmtp=tuple(str(value) for value in fmtp),
        direction=direction if isinstance(direction, str) else None,
    )


def _require_track(
    tracks: tuple[MediaTrack, ...], media_type: str
) -> MediaTrack:
    track = next((item for item in tracks if item.media_type == media_type), None)
    if track is None:
        raise InvalidResponseError(f"SDP omitted a usable {media_type} track.")
    return track


def _parse_media_packet(
    media_type: str,
    packet_type: str,
    channel: int,
    arrival_time: float,
    data: bytes,
) -> EncodedMediaPacket:
    if packet_type == "rtp":
        metadata = _parse_rtp(data)
        return EncodedMediaPacket(
            media_type=media_type,
            packet_type="rtp",
            interleaved_channel=channel,
            arrival_time=arrival_time,
            data=data,
            **metadata,
        )
    return EncodedMediaPacket(
        media_type=media_type,
        packet_type="rtcp",
        interleaved_channel=channel,
        arrival_time=arrival_time,
        data=data,
        rtcp_packets=_parse_rtcp(data),
    )


def _parse_rtp(data: bytes) -> dict[str, int | bool | None]:
    if len(data) < 12 or data[0] >> 6 != 2:
        return {
            "payload_type": None,
            "marker": None,
            "sequence_number": None,
            "rtp_timestamp": None,
            "ssrc": None,
        }
    return {
        "payload_type": data[1] & 0x7F,
        "marker": bool(data[1] & 0x80),
        "sequence_number": int.from_bytes(data[2:4], "big"),
        "rtp_timestamp": int.from_bytes(data[4:8], "big"),
        "ssrc": int.from_bytes(data[8:12], "big"),
    }


def _parse_rtcp(data: bytes) -> tuple[RtcpPacketInfo, ...]:
    packets = []
    offset = 0
    while offset + 4 <= len(data):
        if data[offset] >> 6 != 2:
            return ()
        packet_type = data[offset + 1]
        length = (int.from_bytes(data[offset + 2 : offset + 4], "big") + 1) * 4
        if length < 4 or offset + length > len(data):
            return ()
        packet = data[offset : offset + length]
        ssrc = int.from_bytes(packet[4:8], "big") if len(packet) >= 8 else None
        if packet_type == 200 and len(packet) >= 20:
            packets.append(
                RtcpPacketInfo(
                    packet_type=packet_type,
                    ssrc=ssrc,
                    ntp_seconds=int.from_bytes(packet[8:12], "big"),
                    ntp_fraction=int.from_bytes(packet[12:16], "big"),
                    rtp_timestamp=int.from_bytes(packet[16:20], "big"),
                )
            )
        else:
            packets.append(RtcpPacketInfo(packet_type=packet_type, ssrc=ssrc))
        offset += length
    return tuple(packets) if offset == len(data) else ()


def _control_uri(aggregate: str, content_base: str | None, control: str) -> str:
    if control.casefold().startswith("rtsp://"):
        return control
    if control.startswith("/"):
        parsed = urlsplit(aggregate)
        return f"{parsed.scheme}://{parsed.netloc}{control}"
    return (content_base or aggregate).rstrip("/") + "/" + control
