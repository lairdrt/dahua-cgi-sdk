"""Internal synchronous RTSP transport for recorded media playback."""

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
class _SdpVideo:
    control: str
    codec: str | None
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
        deadline = time.monotonic() + duration
        packets = 0
        byte_count = 0
        first_timestamp: int | None = None
        last_timestamp: int | None = None
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
            if channel != 0 or not payload:
                continue
            packets += 1
            byte_count += len(payload)
            if len(payload) >= 12 and payload[0] >> 6 == 2:
                timestamp = int.from_bytes(payload[4:8], "big")
                if first_timestamp is None:
                    first_timestamp = timestamp
                last_timestamp = timestamp
        return _MediaReceipt(packets, byte_count, first_timestamp, last_timestamp)


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
    """Own one file-specific RTSP connection and streaming session."""

    def __init__(
        self,
        *,
        host: str,
        port: int,
        username: str,
        password: str,
        timeout: float,
        file_path: str,
        connector: Callable[..., socket.socket] = socket.create_connection,
    ) -> None:
        self._host = host
        self._port = port
        self._username = username
        self._password = password
        self._timeout = timeout
        self._connector = connector
        self.target = f"rtsp://{host}:{port}/{file_path}"
        self._socket: socket.socket | None = None
        self._stream: _RtspStream | None = None
        self._digest: _DigestState | None = None
        self._cseq = 1
        self.session: str | None = None
        self.video_control: str | None = None
        self.video_codec: str | None = None
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
            video = _parse_sdp_video(describe.body)
            self.video_control = video.control
            self.video_codec = video.codec
            self.duration = video.duration
            track_uri = _control_uri(
                self.target, describe.header("Content-Base"), video.control
            )
            setup = self._request(
                "SETUP",
                track_uri,
                (("Transport", "RTP/AVP/TCP;unicast;interleaved=0-1"),),
            )
            self._require_ok("SETUP", setup)
            transport = setup.header("Transport")
            if transport is None or "interleaved=0-1" not in transport.casefold():
                raise InvalidResponseError(
                    "SETUP did not confirm interleaved RTP transport."
                )
            session_header = setup.header("Session")
            session = session_header.partition(";")[0].strip() if session_header else ""
            if not session:
                raise InvalidResponseError("SETUP omitted a usable RTSP Session.")
            self.session = session
            play = self._session_request("PLAY", (("Range", "npt=0-"),))
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
        "User-Agent: dahua-cgi-sdk/rtsp-playback",
    ]
    lines.extend(f"{name}: {value}" for name, value in headers)
    if authorization is not None:
        lines.append(f"Authorization: {authorization}")
    return ("\r\n".join(lines) + "\r\n\r\n").encode("ascii")


def _digest_uri(target: str) -> str:
    parsed = urlsplit(target)
    return parsed.path + (("?" + parsed.query) if parsed.query else "")


def _parse_sdp_video(body: bytes) -> _SdpVideo:
    duration: float | None = None
    in_video = False
    control: str | None = None
    codec: str | None = None
    for raw_line in body.decode("utf-8", errors="replace").splitlines():
        line = raw_line.strip()
        range_match = re.fullmatch(r"a=range:npt=[^-]*-(\d+(?:\.\d+)?)", line)
        if range_match is not None:
            duration = float(range_match.group(1))
        if line.startswith("m="):
            in_video = line.startswith("m=video ")
        elif in_video and line.startswith("a=control:"):
            value = line.removeprefix("a=control:").strip()
            if value and value != "*":
                control = value
        elif in_video and line.startswith("a=rtpmap:"):
            mapping = line.partition(" ")[2]
            if mapping:
                codec = mapping.partition("/")[0]
    if control is None:
        raise InvalidResponseError("SDP omitted a usable video control track.")
    return _SdpVideo(control, codec, duration)


def _control_uri(aggregate: str, content_base: str | None, control: str) -> str:
    if control.casefold().startswith("rtsp://"):
        return control
    if control.startswith("/"):
        parsed = urlsplit(aggregate)
        return f"{parsed.scheme}://{parsed.netloc}{control}"
    return (content_base or aggregate).rstrip("/") + "/" + control
