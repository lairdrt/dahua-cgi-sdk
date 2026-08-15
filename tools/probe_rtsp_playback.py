"""Disposable Dahua file-specific RTSP playback lifecycle probe."""

from __future__ import annotations

import hashlib
import os
import re
import secrets
import socket
import time
from dataclasses import dataclass
from urllib.parse import urlsplit

HOST = os.environ.get("DAHUA_HOST", "192.168.1.34")
PORT = 554
TIMEOUT = 10.0
MEDIA_WINDOW = 3.0
MAX_RESPONSE_BYTES = 256 * 1024
FILE_PATH = (
    "//mnt/dvr/2026-08-13/000/dav/10/0/1/85629/"
    "10.21.42-10.22.32[M][0@0][0].dav"
)


class ProbeFailure(RuntimeError):
    """A fail-fast RTSP probe failure."""


@dataclass(frozen=True)
class RtspResponse:
    status_code: int
    status_line: str
    headers: tuple[tuple[str, str], ...]
    body: bytes

    def header(self, name: str) -> str | None:
        wanted = name.casefold()
        return next(
            (value for key, value in self.headers if key.casefold() == wanted), None
        )


@dataclass
class DigestState:
    challenge: dict[str, str]
    nonce_count: int = 0

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


@dataclass
class Feasibility:
    file_playback: bool = False
    interleaved_rtp: bool = False
    pause: bool = False
    seek: bool = False
    resume: bool = False
    teardown: bool = False
    issue: str = "Not started"


class RtspStream:
    """Buffered parser for RTSP responses mixed with interleaved packets."""

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
            raise ProbeFailure("RTSP connection closed unexpectedly")
        self.buffer.extend(chunk)
        if len(self.buffer) > MAX_RESPONSE_BYTES:
            raise ProbeFailure("Buffered RTSP data exceeded size limit")

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

    def response(self, timeout: float = TIMEOUT) -> RtspResponse:
        deadline = time.monotonic() + timeout
        while True:
            packet = self._interleaved(deadline)
            if packet is not None:
                continue
            if self.buffer and not self.buffer.startswith(b"RTSP/"):
                raise ProbeFailure("Unexpected data while awaiting RTSP response")
            header_end = self.buffer.find(b"\r\n\r\n")
            if header_end < 0:
                self._receive(deadline)
                continue
            header_bytes = bytes(self.buffer[:header_end])
            lines = header_bytes.decode("iso-8859-1").split("\r\n")
            status_match = re.fullmatch(
                r"RTSP/\d\.\d\s+(\d{3})(?:\s+.*)?", lines[0]
            )
            if status_match is None:
                raise ProbeFailure(f"Malformed RTSP status line: {lines[0]!r}")
            headers: list[tuple[str, str]] = []
            for line in lines[1:]:
                name, separator, value = line.partition(":")
                if not separator:
                    raise ProbeFailure(f"Malformed RTSP response header: {line!r}")
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
                raise ProbeFailure(f"Invalid Content-Length: {length_text!r}") from exc
            if length < 0 or length > MAX_RESPONSE_BYTES:
                raise ProbeFailure(f"Unsupported Content-Length: {length}")
            message_end = header_end + 4 + length
            while len(self.buffer) < message_end:
                self._receive(deadline)
            body = bytes(self.buffer[header_end + 4 : message_end])
            del self.buffer[:message_end]
            return RtspResponse(
                int(status_match.group(1)), lines[0], tuple(headers), body
            )

    def media(self, duration: float) -> tuple[int, int, int | None]:
        deadline = time.monotonic() + duration
        packets = 0
        byte_count = 0
        first_timestamp: int | None = None
        while time.monotonic() < deadline:
            try:
                packet = self._interleaved(deadline)
                if packet is None:
                    if self.buffer.startswith(b"RTSP/"):
                        raise ProbeFailure(
                            "Unexpected RTSP response while awaiting media"
                        )
                    self._receive(deadline)
                    continue
            except socket.timeout:
                break
            channel, payload = packet
            if channel != 0 or not payload:
                continue
            packets += 1
            byte_count += len(payload)
            if (
                first_timestamp is None
                and len(payload) >= 12
                and payload[0] >> 6 == 2
            ):
                first_timestamp = int.from_bytes(payload[4:8], "big")
        return packets, byte_count, first_timestamp


def _md5(value: str) -> str:
    return hashlib.md5(value.encode()).hexdigest()


def _parse_digest_challenge(value: str) -> dict[str, str]:
    scheme, separator, parameters = value.partition(" ")
    if not separator or scheme.casefold() != "digest":
        raise ProbeFailure(f"Unsupported WWW-Authenticate scheme: {scheme!r}")
    pairs = re.findall(r'(\w+)\s*=\s*(?:"((?:\\.|[^"])*)"|([^,\s]+))', parameters)
    challenge = {
        key.casefold(): re.sub(r"\\(.)", r"\1", quoted) if quoted else bare
        for key, quoted, bare in pairs
    }
    if "realm" not in challenge or "nonce" not in challenge:
        raise ProbeFailure("Digest challenge did not include realm and nonce")
    return challenge


def _quoted(value: str) -> str:
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'


def _authorization(
    username: str,
    password: str,
    method: str,
    uri: str,
    challenge: dict[str, str],
    nonce_count: int,
) -> str:
    algorithm = challenge.get("algorithm", "MD5")
    if algorithm.casefold() != "md5":
        raise ProbeFailure(f"Unsupported Digest algorithm: {algorithm}")
    realm = challenge["realm"]
    nonce = challenge["nonce"]
    ha1 = _md5(f"{username}:{realm}:{password}")
    ha2 = _md5(f"{method}:{uri}")
    fields = [
        f"username={_quoted(username)}",
        f"realm={_quoted(realm)}",
        f"nonce={_quoted(nonce)}",
        f"uri={_quoted(uri)}",
        "algorithm=MD5",
    ]
    qop = challenge.get("qop")
    if qop is not None:
        choices = [choice.strip() for choice in qop.split(",")]
        if "auth" not in (choice.casefold() for choice in choices):
            raise ProbeFailure(f"Unsupported Digest qop: {qop}")
        nonce_count_text = f"{nonce_count:08x}"
        cnonce = secrets.token_hex(8)
        response = _md5(f"{ha1}:{nonce}:{nonce_count_text}:{cnonce}:auth:{ha2}")
        fields.extend(
            ("qop=auth", f"nc={nonce_count_text}", f"cnonce={_quoted(cnonce)}")
        )
    else:
        response = _md5(f"{ha1}:{nonce}:{ha2}")
    fields.append(f"response={_quoted(response)}")
    if "opaque" in challenge:
        fields.append(f"opaque={_quoted(challenge['opaque'])}")
    return "Digest " + ", ".join(fields)


def _request(
    method: str,
    target: str,
    cseq: int,
    authorization: str | None,
    headers: tuple[tuple[str, str], ...] = (),
) -> bytes:
    lines = [
        f"{method} {target} RTSP/1.0",
        f"CSeq: {cseq}",
        "User-Agent: dahua-rtsp-playback-probe/2.0",
    ]
    lines.extend(f"{name}: {value}" for name, value in headers)
    if authorization is not None:
        lines.append(f"Authorization: {authorization}")
    return ("\r\n".join(lines) + "\r\n\r\n").encode("ascii")


def _digest_uri(target: str) -> str:
    parsed = urlsplit(target)
    return parsed.path + (("?" + parsed.query) if parsed.query else "")


def _authenticated_request(
    stream: RtspStream,
    digest: DigestState,
    username: str,
    password: str,
    method: str,
    target: str,
    cseq: int,
    headers: tuple[tuple[str, str], ...] = (),
) -> tuple[RtspResponse, int]:
    digest_uri = _digest_uri(target)
    for attempt in range(2):
        authorization = digest.authorization(username, password, method, digest_uri)
        stream.connection.sendall(
            _request(method, target, cseq, authorization, headers)
        )
        response = stream.response()
        cseq += 1
        if response.status_code != 401:
            return response, cseq
        authenticate = response.header("WWW-Authenticate")
        if authenticate is None:
            raise ProbeFailure(f"{method} 401 omitted WWW-Authenticate")
        replacement = _parse_digest_challenge(authenticate)
        nonce_changed = replacement["nonce"] != digest.challenge["nonce"]
        stale = replacement.get("stale", "false").casefold() == "true"
        if attempt == 0 and (nonce_changed or stale):
            digest.update(replacement)
            continue
        raise ProbeFailure(f"{method} returned an unrefreshable 401")
    raise AssertionError("unreachable")


def _acquire_challenge(host: str, target: str) -> DigestState:
    with socket.create_connection((host, PORT), timeout=TIMEOUT) as connection:
        stream = RtspStream(connection)
        connection.sendall(
            _request(
                "DESCRIBE", target, 1, None, (("Accept", "application/sdp"),)
            )
        )
        response = stream.response()
    if response.status_code != 401:
        raise ProbeFailure(
            f"Challenge acquisition expected 401, received {response.status_line}"
        )
    authenticate = response.header("WWW-Authenticate")
    if authenticate is None:
        raise ProbeFailure("Challenge acquisition omitted WWW-Authenticate")
    return DigestState(_parse_digest_challenge(authenticate))


def _video_control(sdp: bytes) -> str:
    in_video = False
    for raw_line in sdp.decode("utf-8", errors="replace").splitlines():
        line = raw_line.strip()
        if line.startswith("m="):
            in_video = line.startswith("m=video ")
        elif in_video and line.startswith("a=control:"):
            control = line.removeprefix("a=control:").strip()
            if control and control != "*":
                return control
    raise ProbeFailure("SDP lacks a usable video track control")


def _control_uri(aggregate: str, content_base: str | None, control: str) -> str:
    if control.casefold().startswith("rtsp://"):
        return control
    if control.startswith("/"):
        parsed = urlsplit(aggregate)
        return f"{parsed.scheme}://{parsed.netloc}{control}"
    base = content_base or aggregate
    return base.rstrip("/") + "/" + control


def _print_status(label: str, response: RtspResponse) -> None:
    result = "PASS" if response.status_code == 200 else "FAIL"
    print(f"{label:<24} {response.status_code} {result}")


def _print_play_headers(response: RtspResponse) -> None:
    if value := response.header("Range"):
        print(f"  Range: {value}")
    if value := response.header("RTP-Info"):
        print(f"  RTP-Info: {value}")


def _require_ok(label: str, response: RtspResponse) -> None:
    _print_status(label, response)
    if response.status_code != 200:
        raise ProbeFailure(f"{label} returned {response.status_line}")


def _print_report(result: Feasibility) -> None:
    print("\n=== RTSP FILE PLAYBACK FEASIBILITY ===\n")
    print(f"File playback:        {'PASS' if result.file_playback else 'FAIL'}")
    print(f"TCP interleaved RTP:  {'PASS' if result.interleaved_rtp else 'FAIL'}")
    print(f"Pause:                {'PASS' if result.pause else 'FAIL'}")
    print(f"Seek to npt=25:       {'PASS' if result.seek else 'FAIL'}")
    print(f"Resume after seek:    {'PASS' if result.resume else 'FAIL'}")
    print(f"Teardown:             {'PASS' if result.teardown else 'FAIL'}")
    print("\nExact unresolved issue:")
    print(f"    {result.issue}")


def _run(username: str, password: str, result: Feasibility) -> None:
    aggregate = f"rtsp://{HOST}:{PORT}{FILE_PATH}"
    digest = _acquire_challenge(HOST, aggregate)
    with socket.create_connection((HOST, PORT), timeout=TIMEOUT) as connection:
        stream = RtspStream(connection)
        cseq = 1
        describe, cseq = _authenticated_request(
            stream,
            digest,
            username,
            password,
            "DESCRIBE",
            aggregate,
            cseq,
            (("Accept", "application/sdp"),),
        )
        _require_ok("DESCRIBE", describe)
        control = _video_control(describe.body)
        print(f"{'Video control':<24} {control}")
        track_uri = _control_uri(aggregate, describe.header("Content-Base"), control)

        setup, cseq = _authenticated_request(
            stream,
            digest,
            username,
            password,
            "SETUP",
            track_uri,
            cseq,
            (("Transport", "RTP/AVP/TCP;unicast;interleaved=0-1"),),
        )
        _require_ok("SETUP", setup)
        session_header = setup.header("Session")
        if session_header is None or not session_header.partition(";")[0].strip():
            raise ProbeFailure("SETUP 200 omitted a usable Session header")
        session = session_header.partition(";")[0].strip()
        print(f"{'Session':<24} <redacted>")

        play, cseq = _authenticated_request(
            stream,
            digest,
            username,
            password,
            "PLAY",
            aggregate,
            cseq,
            (("Session", session), ("Range", "npt=0-")),
        )
        _require_ok("PLAY npt=0-", play)
        _print_play_headers(play)
        packets, byte_count, initial_timestamp = stream.media(MEDIA_WINDOW)
        print(f"{'Initial media packets':<24} {packets}")
        print(f"{'Initial media bytes':<24} {byte_count}")
        if not packets or not byte_count:
            raise ProbeFailure(
                "No non-empty interleaved RTP arrived after initial PLAY"
            )
        result.file_playback = True
        result.interleaved_rtp = True

        pause, cseq = _authenticated_request(
            stream,
            digest,
            username,
            password,
            "PAUSE",
            aggregate,
            cseq,
            (("Session", session),),
        )
        _require_ok("PAUSE", pause)
        result.pause = True

        seek, cseq = _authenticated_request(
            stream,
            digest,
            username,
            password,
            "PLAY",
            aggregate,
            cseq,
            (("Session", session), ("Range", "npt=25-")),
        )
        _require_ok("PLAY npt=25-", seek)
        _print_play_headers(seek)
        result.seek = True
        packets, byte_count, seek_timestamp = stream.media(MEDIA_WINDOW)
        print(f"{'Seek media packets':<24} {packets}")
        print(f"{'Seek media bytes':<24} {byte_count}")
        if initial_timestamp is not None and seek_timestamp is not None:
            changed = "yes" if initial_timestamp != seek_timestamp else "no"
            print(f"{'RTP timestamp changed':<24} {changed}")
        if not packets or not byte_count:
            raise ProbeFailure("No non-empty interleaved RTP arrived after seek PLAY")
        result.resume = True

        teardown, _ = _authenticated_request(
            stream,
            digest,
            username,
            password,
            "TEARDOWN",
            aggregate,
            cseq,
            (("Session", session),),
        )
        _require_ok("TEARDOWN", teardown)
        result.teardown = True
        result.issue = "None"


def main() -> None:
    username = os.environ.get("DAHUA_USERNAME")
    password = os.environ.get("DAHUA_PASSWORD")
    if not username or not password:
        raise SystemExit("Set DAHUA_USERNAME and DAHUA_PASSWORD before running.")
    result = Feasibility()
    try:
        _run(username, password, result)
    except (OSError, ProbeFailure) as exc:
        result.issue = str(exc)
    _print_report(result)


if __name__ == "__main__":
    main()
