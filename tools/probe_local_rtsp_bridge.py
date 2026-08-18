"""One-upstream, one-client recorded RTSP bridge prototype."""

from __future__ import annotations

import json
import os
import secrets
import socket
import statistics
import threading
import time
from collections import deque
from dataclasses import dataclass
from datetime import timedelta
from typing import Any

from dahua_rpc import DahuaClient, EncodedMediaPacket, MediaTrack

HOST = "127.0.0.1"
PATH = "/recorded"
QUEUE_CAPACITY = 2048
UPSTREAM_WINDOW = 0.05


@dataclass(frozen=True, slots=True)
class OutboundPacket:
    media_type: str
    packet_type: str
    arrival_time: float
    data: bytes


class BoundedPacketQueue:
    """Fixed-capacity FIFO which drops the oldest stale packet on overflow."""

    def __init__(self, capacity: int) -> None:
        if capacity < 1:
            raise ValueError("capacity must be positive")
        self.capacity = capacity
        self._items: deque[OutboundPacket] = deque()
        self._condition = threading.Condition()
        self.closed = False
        self.dropped = 0

    def put(self, item: OutboundPacket) -> None:
        with self._condition:
            if self.closed:
                return
            if len(self._items) == self.capacity:
                self._items.popleft()
                self.dropped += 1
            self._items.append(item)
            self._condition.notify()

    def get(self, timeout: float) -> OutboundPacket | None:
        with self._condition:
            if not self._items and not self.closed:
                self._condition.wait(timeout)
            return self._items.popleft() if self._items else None

    def clear(self) -> None:
        with self._condition:
            self._items.clear()

    def close(self) -> None:
        with self._condition:
            self.closed = True
            self._items.clear()
            self._condition.notify_all()


class RtpContinuityMapper:
    """Map one upstream RTP source into one stable downstream source."""

    def __init__(self, clock_rate: int, *, ssrc: int | None = None) -> None:
        if clock_rate <= 0:
            raise ValueError("clock_rate must be positive")
        self.clock_rate = clock_rate
        self.ssrc = ssrc if ssrc is not None else secrets.randbits(32)
        self.next_sequence = secrets.randbits(16)
        self.last_upstream_timestamp: int | None = None
        self.last_downstream_timestamp: int | None = None
        self.last_arrival: float | None = None
        self.pending_discontinuity = False

    def discontinuity(self) -> None:
        self.pending_discontinuity = True

    def rewrite(self, packet: EncodedMediaPacket) -> bytes:
        if (
            packet.packet_type != "rtp"
            or packet.rtp_timestamp is None
            or len(packet.data) < 12
        ):
            return packet.data
        timestamp = self._map_timestamp(packet.rtp_timestamp, packet.arrival_time)
        data = bytearray(packet.data)
        data[2:4] = self.next_sequence.to_bytes(2, "big")
        data[4:8] = timestamp.to_bytes(4, "big")
        data[8:12] = self.ssrc.to_bytes(4, "big")
        self.next_sequence = (self.next_sequence + 1) & 0xFFFF
        return bytes(data)

    def _map_timestamp(self, upstream: int, arrival: float) -> int:
        if self.last_downstream_timestamp is None:
            downstream = secrets.randbits(32)
        elif self.pending_discontinuity:
            downstream = (
                self.last_downstream_timestamp + self._arrival_increment(arrival)
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
        return downstream

    def _arrival_increment(self, arrival: float) -> int:
        if self.last_arrival is None:
            return 1
        elapsed = min(max(arrival - self.last_arrival, 0.0), 0.25)
        return max(1, round(elapsed * self.clock_rate))

    def translate_rtcp_timestamp(self, upstream: int) -> int | None:
        if self.pending_discontinuity or self.last_downstream_timestamp is None:
            return None
        assert self.last_upstream_timestamp is not None
        delta = (upstream - self.last_upstream_timestamp) & 0xFFFFFFFF
        if delta > self.clock_rate * 2:
            return None
        return (self.last_downstream_timestamp + delta) & 0xFFFFFFFF


def rewrite_rtcp(data: bytes, mapper: RtpContinuityMapper) -> bytes | None:
    """Rewrite compound RTCP SSRCs and SR RTP timestamps, retaining NTP."""

    rewritten = bytearray(data)
    offset = 0
    while offset + 4 <= len(rewritten):
        if rewritten[offset] >> 6 != 2:
            return None
        packet_type = rewritten[offset + 1]
        length = (
            int.from_bytes(rewritten[offset + 2 : offset + 4], "big") + 1
        ) * 4
        if length < 4 or offset + length > len(rewritten):
            return None
        if length >= 8:
            rewritten[offset + 4 : offset + 8] = mapper.ssrc.to_bytes(4, "big")
        if packet_type == 200:
            if length < 20:
                return None
            upstream = int.from_bytes(rewritten[offset + 16 : offset + 20], "big")
            downstream = mapper.translate_rtcp_timestamp(upstream)
            if downstream is None:
                return None
            rewritten[offset + 16 : offset + 20] = downstream.to_bytes(4, "big")
        offset += length
    return bytes(rewritten) if offset == len(rewritten) else None


def build_sdp(tracks: tuple[MediaTrack, ...], duration: float | None) -> str:
    lines = [
        "v=0",
        "o=- 0 0 IN IP4 127.0.0.1",
        "s=Dahua recorded bridge",
        "c=IN IP4 127.0.0.1",
        "t=0 0",
        f"a=range:npt=0-{duration:g}" if duration is not None else "a=range:npt=0-",
        "a=control:*",
    ]
    for track in tracks:
        lines.append(f"m={track.media_type} 0 RTP/AVP {track.payload_type}")
        if track.codec and track.clock_rate:
            mapping = f"{track.codec}/{track.clock_rate}"
            if track.channels is not None:
                mapping += f"/{track.channels}"
            lines.append(f"a=rtpmap:{track.payload_type} {mapping}")
        lines.extend(f"a=fmtp:{value}" for value in track.fmtp)
        lines.append(f"a=control:trackID={track.media_type}")
        if track.direction:
            lines.append(f"a={track.direction}")
    return "\r\n".join(lines) + "\r\n"


class LocalRtspBridge:
    """Purpose-built loopback bridge for one playback and one client."""

    def __init__(
        self,
        playback: Any,
        *,
        host: str = HOST,
        port: int = 0,
        include_audio: bool = False,
        queue_capacity: int = QUEUE_CAPACITY,
    ) -> None:
        self.playback = playback
        self.host = host
        self.requested_port = port
        self.include_audio = include_audio
        self.queue = BoundedPacketQueue(queue_capacity)
        self.stop_event = threading.Event()
        self.play_event = threading.Event()
        self.control_lock = threading.Lock()
        self.send_lock = threading.Lock()
        self.server: socket.socket | None = None
        self.client: socket.socket | None = None
        self.session = secrets.token_hex(8)
        self.channels: dict[tuple[str, str], int] = {}
        self.mappers: dict[str, RtpContinuityMapper] = {}
        self.pending_rtcp: dict[str, EncodedMediaPacket] = {}
        self.threads: list[threading.Thread] = []
        self.latency_ms: dict[str, list[float]] = {"video": [], "audio": []}
        self.error: str | None = None
        self.upstream_paused = False

    @property
    def port(self) -> int:
        if self.server is None:
            raise RuntimeError("bridge is not started")
        return self.server.getsockname()[1]

    @property
    def url(self) -> str:
        return f"rtsp://{self.host}:{self.port}{PATH}"

    def start(self) -> None:
        self.playback.start()
        for track in self.tracks:
            if track.clock_rate:
                self.mappers[track.media_type] = RtpContinuityMapper(track.clock_rate)
        self.server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.server.bind((self.host, self.requested_port))
        self.server.listen(1)
        self.server.settimeout(0.2)
        self._thread("rtsp-accept", self._accept_loop)
        self._thread("upstream-reader", self._reader_loop)
        self._thread("downstream-sender", self._sender_loop)

    @property
    def tracks(self) -> tuple[MediaTrack, ...]:
        return tuple(
            track
            for track in self.playback.media_tracks
            if track.media_type == "video" or self.include_audio
        )

    def seek(self, seconds: float) -> None:
        with self.control_lock:
            self.playback.seek(seconds)
            for mapper in self.mappers.values():
                mapper.discontinuity()
            self.queue.clear()

    def close(self) -> None:
        self.stop_event.set()
        self.play_event.clear()
        self.queue.close()
        for connection in (self.client, self.server):
            if connection is not None:
                try:
                    connection.shutdown(socket.SHUT_RDWR)
                except OSError:
                    pass
                connection.close()
        self.client = None
        self.server = None
        for thread in self.threads:
            if thread is not threading.current_thread():
                thread.join(timeout=2.0)
        self.playback.close()

    def _thread(self, name: str, target: Any) -> None:
        thread = threading.Thread(name=name, target=target)
        thread.start()
        self.threads.append(thread)

    def _accept_loop(self) -> None:
        assert self.server is not None
        while not self.stop_event.is_set():
            try:
                connection, _ = self.server.accept()
            except socket.timeout:
                continue
            except OSError:
                return
            if self.client is not None:
                connection.sendall(_response(453, "Not Enough Bandwidth"))
                connection.close()
                continue
            self.client = connection
            connection.settimeout(0.2)
            try:
                self._client_loop(connection)
            except (OSError, RuntimeError) as exc:
                if not self.stop_event.is_set():
                    self.error = str(exc)
            finally:
                self.play_event.clear()
                try:
                    connection.close()
                except OSError:
                    pass
                self.client = None

    def _client_loop(self, connection: socket.socket) -> None:
        buffer = bytearray()
        while not self.stop_event.is_set():
            request = _read_request(connection, buffer)
            if request is None:
                continue
            method, target, headers = request
            cseq = headers.get("cseq", "1")
            if method == "OPTIONS":
                self._send_response(
                    connection,
                    200,
                    cseq,
                    (("Public", "OPTIONS, DESCRIBE, SETUP, PLAY, PAUSE, TEARDOWN"),),
                )
            elif method == "DESCRIBE":
                body = build_sdp(self.tracks, self.playback.duration)
                self._send_response(
                    connection,
                    200,
                    cseq,
                    (("Content-Type", "application/sdp"),
                     ("Content-Base", self.url + "/")),
                    body.encode(),
                )
            elif method == "SETUP":
                media_type = _media_from_target(target)
                if not any(track.media_type == media_type for track in self.tracks):
                    raise RuntimeError("SETUP requested an unavailable track")
                requested = _interleaved(headers.get("transport", ""))
                self.channels[(media_type, "rtp")] = requested[0]
                self.channels[(media_type, "rtcp")] = requested[1]
                self._send_response(
                    connection,
                    200,
                    cseq,
                    (("Session", self.session),
                     ("Transport", "RTP/AVP/TCP;unicast;"
                                   f"interleaved={requested[0]}-{requested[1]}")),
                )
            elif method == "PLAY":
                if self.upstream_paused:
                    with self.control_lock:
                        self.playback.resume()
                    self.upstream_paused = False
                self._send_response(
                    connection, 200, cseq, (("Session", self.session),)
                )
                self.play_event.set()
            elif method == "PAUSE":
                self.play_event.clear()
                with self.control_lock:
                    self.playback.pause()
                self.upstream_paused = True
                self._send_response(
                    connection, 200, cseq, (("Session", self.session),)
                )
            elif method == "TEARDOWN":
                self.play_event.clear()
                self._send_response(
                    connection, 200, cseq, (("Session", self.session),)
                )
                return
            else:
                self._send_response(connection, 405, cseq)

    def _send_response(
        self,
        connection: socket.socket,
        status: int,
        cseq: str,
        headers: tuple[tuple[str, str], ...] = (),
        body: bytes = b"",
    ) -> None:
        with self.send_lock:
            connection.sendall(_response(status, "OK", cseq, headers, body))

    def _reader_loop(self) -> None:
        while not self.stop_event.is_set():
            if not self.play_event.wait(0.1):
                continue
            try:
                with self.control_lock:
                    packets = self.playback.receive_packets(UPSTREAM_WINDOW)
            except Exception as exc:
                if not self.stop_event.is_set():
                    self.error = str(exc)
                    self.stop_event.set()
                return
            for packet in packets:
                mapper = self.mappers.get(packet.media_type)
                if mapper is None:
                    continue
                if packet.packet_type == "rtp":
                    data = mapper.rewrite(packet)
                    self._queue_packet(packet, data)
                    pending = self.pending_rtcp.pop(packet.media_type, None)
                    if pending is not None:
                        translated = rewrite_rtcp(pending.data, mapper)
                        if translated is not None:
                            self._queue_packet(pending, translated)
                else:
                    data = rewrite_rtcp(packet.data, mapper)
                    if data is None:
                        self.pending_rtcp[packet.media_type] = packet
                        continue
                    self._queue_packet(packet, data)

    def _queue_packet(self, packet: EncodedMediaPacket, data: bytes) -> None:
        self.queue.put(
            OutboundPacket(
                packet.media_type,
                packet.packet_type,
                packet.arrival_time,
                data,
            )
        )

    def _sender_loop(self) -> None:
        while not self.stop_event.is_set():
            packet = self.queue.get(0.1)
            if packet is None:
                continue
            connection = self.client
            channel = self.channels.get((packet.media_type, packet.packet_type))
            if connection is None or channel is None or not self.play_event.is_set():
                continue
            frame = b"$" + bytes((channel,)) + len(packet.data).to_bytes(2, "big")
            try:
                with self.send_lock:
                    connection.sendall(frame + packet.data)
            except OSError as exc:
                if not self.stop_event.is_set():
                    self.error = str(exc)
                self.play_event.clear()
                continue
            self.latency_ms[packet.media_type].append(
                (time.monotonic() - packet.arrival_time) * 1000
            )


def _read_request(
    connection: socket.socket, buffer: bytearray
) -> tuple[str, str, dict[str, str]] | None:
    while b"\r\n\r\n" not in buffer:
        try:
            chunk = connection.recv(4096)
        except socket.timeout:
            return None
        if not chunk:
            raise RuntimeError("downstream client disconnected")
        buffer.extend(chunk)
    raw, _, remainder = buffer.partition(b"\r\n\r\n")
    buffer[:] = remainder
    lines = raw.decode("iso-8859-1").split("\r\n")
    method, target, _ = lines[0].split(" ", 2)
    headers = {}
    for line in lines[1:]:
        name, separator, value = line.partition(":")
        if separator:
            headers[name.casefold()] = value.strip()
    return method, target, headers


def _response(
    status: int,
    reason: str,
    cseq: str = "1",
    headers: tuple[tuple[str, str], ...] = (),
    body: bytes = b"",
) -> bytes:
    lines = [f"RTSP/1.0 {status} {reason}", f"CSeq: {cseq}"]
    lines.extend(f"{name}: {value}" for name, value in headers)
    lines.append(f"Content-Length: {len(body)}")
    return ("\r\n".join(lines) + "\r\n\r\n").encode() + body


def _media_from_target(target: str) -> str:
    for media_type in ("video", "audio"):
        if target.rstrip("/").endswith(f"trackID={media_type}"):
            return media_type
    raise RuntimeError("SETUP target did not identify a supported track")


def _interleaved(transport: str) -> tuple[int, int]:
    marker = "interleaved="
    value = next(
        (part[len(marker):] for part in transport.split(";")
         if part.casefold().startswith(marker)),
        None,
    )
    if value is None:
        raise RuntimeError("only TCP-interleaved transport is supported")
    left, separator, right = value.partition("-")
    if not separator:
        raise RuntimeError("invalid interleaved channel pair")
    return int(left), int(right)


def _client_request(
    connection: socket.socket,
    method: str,
    target: str,
    cseq: int,
    headers: tuple[tuple[str, str], ...] = (),
) -> tuple[int, dict[str, str], bytes]:
    lines = [f"{method} {target} RTSP/1.0", f"CSeq: {cseq}"]
    lines.extend(f"{name}: {value}" for name, value in headers)
    connection.sendall(("\r\n".join(lines) + "\r\n\r\n").encode())
    return _client_response(connection)


def _client_response(
    connection: socket.socket,
) -> tuple[int, dict[str, str], bytes]:
    buffer = bytearray()
    while True:
        while buffer and buffer[0] == ord("$"):
            if len(buffer) < 4:
                break
            frame_length = int.from_bytes(buffer[2:4], "big")
            if len(buffer) < 4 + frame_length:
                break
            del buffer[: 4 + frame_length]
        if buffer.startswith(b"RTSP/") and b"\r\n\r\n" in buffer:
            break
        buffer.extend(connection.recv(65536))
    head, _, remainder = buffer.partition(b"\r\n\r\n")
    lines = head.decode().split("\r\n")
    status = int(lines[0].split()[1])
    headers = {
        name.casefold(): value.strip()
        for line in lines[1:]
        for name, separator, value in (line.partition(":"),)
        if separator
    }
    length = int(headers.get("content-length", "0"))
    while len(remainder) < length:
        remainder += connection.recv(4096)
    return status, headers, bytes(remainder[:length])


def _receive_frames(
    connection: socket.socket, duration: float
) -> list[tuple[int, bytes]]:
    deadline = time.monotonic() + duration
    buffer = bytearray()
    frames = []
    while time.monotonic() < deadline:
        connection.settimeout(max(0.01, deadline - time.monotonic()))
        try:
            buffer.extend(connection.recv(65536))
        except socket.timeout:
            break
        while len(buffer) >= 4 and buffer[0] == ord("$"):
            length = int.from_bytes(buffer[2:4], "big")
            if len(buffer) < 4 + length:
                break
            frames.append((buffer[1], bytes(buffer[4 : 4 + length])))
            del buffer[: 4 + length]
    return frames


def _rtp_identity(frames: list[tuple[int, bytes]], channel: int) -> dict[str, Any]:
    packets = [data for item_channel, data in frames
               if item_channel == channel and len(data) >= 12]
    return {
        "count": len(packets),
        "ssrcs": sorted({int.from_bytes(data[8:12], "big") for data in packets}),
        "first_sequence": int.from_bytes(packets[0][2:4], "big") if packets else None,
        "last_sequence": int.from_bytes(packets[-1][2:4], "big") if packets else None,
        "first_timestamp": int.from_bytes(packets[0][4:8], "big") if packets else None,
        "last_timestamp": int.from_bytes(packets[-1][4:8], "big") if packets else None,
    }


def _rtcp_identity(frames: list[tuple[int, bytes]], channel: int) -> dict[str, Any]:
    packets = [data for item_channel, data in frames if item_channel == channel]
    sender_reports = [
        data for data in packets if len(data) >= 20 and data[1] == 200
    ]
    return {
        "count": len(packets),
        "sender_report_count": len(sender_reports),
        "sender_report_ssrcs": sorted(
            {int.from_bytes(data[4:8], "big") for data in sender_reports}
        ),
    }


def validate_bridge(playback: Any, *, audio: bool) -> dict[str, Any]:
    bridge = LocalRtspBridge(playback, include_audio=audio)
    bridge.start()
    try:
        with socket.create_connection(
            (bridge.host, bridge.port), timeout=2.0
        ) as client:
            client.settimeout(2.0)
            status, _, sdp = _client_request(client, "DESCRIBE", bridge.url, 1)
            if status != 200:
                raise RuntimeError("DESCRIBE failed")
            tracks = ("video", "audio") if audio else ("video",)
            cseq = 2
            for index, media_type in enumerate(tracks):
                status, _, _ = _client_request(
                    client,
                    "SETUP",
                    f"{bridge.url}/trackID={media_type}",
                    cseq,
                    (("Transport", "RTP/AVP/TCP;unicast;"
                                   f"interleaved={index * 2}-{index * 2 + 1}"),),
                )
                if status != 200:
                    raise RuntimeError(f"{media_type} SETUP failed")
                cseq += 1
            status, _, _ = _client_request(
                client, "PLAY", bridge.url, cseq, (("Session", bridge.session),)
            )
            if status != 200:
                raise RuntimeError("PLAY failed")
            before = _receive_frames(client, 2.0)
            target = min(max((playback.duration or 10) * 0.65, 1.0),
                         playback.duration or 10)
            bridge.seek(target)
            after = _receive_frames(client, 2.0)
            cseq += 1
            _client_request(
                client, "TEARDOWN", bridge.url, cseq,
                (("Session", bridge.session),),
            )
        result = {
            "url": bridge.url,
            "sdp": sdp.decode(),
            "before": {
                media: {
                    "rtp": _rtp_identity(before, index * 2),
                    "rtcp": _rtcp_identity(before, index * 2 + 1),
                }
                for index, media in enumerate(tracks)
            },
            "after": {
                media: {
                    "rtp": _rtp_identity(after, index * 2),
                    "rtcp": _rtcp_identity(after, index * 2 + 1),
                }
                for index, media in enumerate(tracks)
            },
            "queue_dropped": bridge.queue.dropped,
            "latency_ms": {
                media: _latency(values)
                for media, values in bridge.latency_ms.items()
                if values
            },
            "bridge_error": bridge.error,
        }
        return result
    finally:
        bridge.close()


def _latency(values: list[float]) -> dict[str, float]:
    ordered = sorted(values)
    index = min(len(ordered) - 1, int(len(ordered) * 0.95))
    return {
        "median": statistics.median(ordered),
        "p95": ordered[index],
        "max": ordered[-1],
    }


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
        camera = next(item for item in client.cameras.list()
                      if item.configured and item.name == "Drive Down")
        end = client.current_time
        with client.media.recordings(
            channel=camera.channel, start=end - timedelta(days=1), end=end
        ) as recordings:
            recording = next(item for item in recordings
                             if item.video_stream == "Main")
        video = validate_bridge(client.media.playback(recording), audio=False)
        dual = validate_bridge(
            client.media.playback(recording, audio=True), audio=True
        )
    print(json.dumps({
        "camera": {"name": camera.name, "channel": camera.channel},
        "recording": {
            "profile": recording.video_stream,
            "start": recording.start_time.isoformat(),
            "end": recording.end_time.isoformat(),
        },
        "video_only": video,
        "video_audio": dual,
    }, indent=2))


if __name__ == "__main__":
    main()
