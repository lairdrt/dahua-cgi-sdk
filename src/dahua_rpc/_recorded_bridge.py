"""Private logical recorded output and loopback RTSP presentation."""

from __future__ import annotations

import secrets
import socket
import threading
import time
from collections import deque
from dataclasses import dataclass
from datetime import datetime
from typing import Protocol
from urllib.parse import urlsplit

from ._media_fanout import DEFAULT_QUEUE_CAPACITY, EncodedPacketFanout
from ._rtp_continuity import (
    CodecConfigurationCache,
    RtpContinuityMapper,
    TrackConfiguration,
    require_compatible,
    rewrite_rtcp,
    track_configurations,
)
from .models import EncodedMediaPacket, MediaTrack

LOOPBACK_HOST = "127.0.0.1"
LOGICAL_PATH = "/recorded"
HANDOFF_TIMEOUT = 1.5
HANDOFF_PACKET_LIMIT = 256
STARTUP_TIMEOUT = 30.0
STARTUP_PACKET_LIMIT = 2048
STARTUP_AUDIO_PACKET_LIMIT = 256
H265_IRAP_TYPES = frozenset(range(16, 22))


class StartupTimeoutError(RuntimeError):
    """A downstream client did not receive a decoder-startable access unit."""


@dataclass(frozen=True, slots=True)
class _VideoPacketInfo:
    timestamp: int
    sequence: int
    marker: bool
    keyframe: bool
    keyframe_fragment: tuple[bool, bool] | None = None


def _video_packet_info(codec: str, data: bytes) -> _VideoPacketInfo | None:
    """Return the minimum RTP/NAL information needed by the startup gate."""
    if len(data) < 13 or data[0] >> 6 != 2:
        return None
    offset = 12 + 4 * (data[0] & 0x0F)
    if data[0] & 0x10:
        if len(data) < offset + 4:
            return None
        offset += 4 + 4 * int.from_bytes(data[offset + 2 : offset + 4], "big")
    if data[0] & 0x20:
        padding = data[-1]
        if padding == 0 or padding > len(data) - offset:
            return None
        payload = data[offset:-padding]
    else:
        payload = data[offset:]
    if not payload:
        return None
    timestamp = int.from_bytes(data[4:8], "big")
    sequence = int.from_bytes(data[2:4], "big")
    marker = bool(data[1] & 0x80)
    codec = codec.upper()
    if codec == "H265":
        if len(payload) < 2:
            return None
        nal_type = (payload[0] >> 1) & 0x3F
        if nal_type == 49:
            if len(payload) < 3:
                return None
            fu_type = payload[2] & 0x3F
            return _VideoPacketInfo(
                timestamp,
                sequence,
                marker,
                fu_type in H265_IRAP_TYPES,
                (bool(payload[2] & 0x80), bool(payload[2] & 0x40)),
            )
        if nal_type == 48:
            offset = 2
            keyframe = False
            while offset < len(payload):
                if offset + 2 > len(payload):
                    return None
                length = int.from_bytes(payload[offset : offset + 2], "big")
                offset += 2
                if length < 2 or offset + length > len(payload):
                    return None
                keyframe |= ((payload[offset] >> 1) & 0x3F) in H265_IRAP_TYPES
                offset += length
            return _VideoPacketInfo(timestamp, sequence, marker, keyframe)
        return _VideoPacketInfo(
            timestamp, sequence, marker, nal_type in H265_IRAP_TYPES
        )
    if codec == "H264":
        nal_type = payload[0] & 0x1F
        if nal_type == 28:
            if len(payload) < 2:
                return None
            fu_type = payload[1] & 0x1F
            return _VideoPacketInfo(
                timestamp,
                sequence,
                marker,
                fu_type == 5,
                (bool(payload[1] & 0x80), bool(payload[1] & 0x40)),
            )
        if nal_type == 24:
            offset = 1
            keyframe = False
            while offset < len(payload):
                if offset + 2 > len(payload):
                    return None
                length = int.from_bytes(payload[offset : offset + 2], "big")
                offset += 2
                if length < 1 or offset + length > len(payload):
                    return None
                keyframe |= (payload[offset] & 0x1F) == 5
                offset += length
            return _VideoPacketInfo(timestamp, sequence, marker, keyframe)
        return _VideoPacketInfo(timestamp, sequence, marker, nal_type == 5)
    return None


class _StartupGate:
    """Bounded, downstream-local decoder startup state."""

    def __init__(self, codec: str, packet_limit: int, audio_limit: int) -> None:
        self.codec = codec
        self.packet_limit = packet_limit
        self.audio_limit = audio_limit
        self.deadline = 0.0
        self.active = False
        self.candidate: list[EncodedMediaPacket] = []
        self.candidate_timestamp: int | None = None
        self.candidate_keyframe = False
        self.fu_started = False
        self.fu_complete = False
        self.invalid = False
        self.audio: deque[EncodedMediaPacket] = deque(maxlen=audio_limit)
        self.packet_high_water = 0
        self.audio_high_water = 0

    def arm(self, deadline: float) -> None:
        self.deadline = deadline
        self.active = True
        self.candidate.clear()
        self.candidate_timestamp = None
        self.candidate_keyframe = False
        self.fu_started = False
        self.fu_complete = False
        self.invalid = False
        self.audio.clear()

    def stop(self) -> None:
        self.active = False
        self.candidate.clear()
        self.audio.clear()

    def add_audio(self, packet: EncodedMediaPacket) -> None:
        self.audio.append(packet)
        self.audio_high_water = max(self.audio_high_water, len(self.audio))

    def add_video(
        self, packet: EncodedMediaPacket
    ) -> tuple[tuple[EncodedMediaPacket, ...], tuple[EncodedMediaPacket, ...]] | None:
        info = _video_packet_info(self.codec, packet.data)
        if info is None:
            self.candidate.clear()
            self.candidate_timestamp = None
            self.invalid = True
            return None
        if info.timestamp != self.candidate_timestamp:
            self.candidate = []
            self.candidate_timestamp = info.timestamp
            self.candidate_keyframe = False
            self.fu_started = False
            self.fu_complete = False
            self.invalid = False
        elif self.candidate:
            previous = int.from_bytes(self.candidate[-1].data[2:4], "big")
            if info.sequence != (previous + 1) & 0xFFFF:
                self.invalid = True
        if len(self.candidate) >= self.packet_limit:
            self.candidate.clear()
            self.invalid = True
            return None
        self.candidate.append(packet)
        self.packet_high_water = max(self.packet_high_water, len(self.candidate))
        if info.keyframe:
            self.candidate_keyframe = True
            if info.keyframe_fragment is not None:
                start, end = info.keyframe_fragment
                if start:
                    self.fu_started = True
                elif not self.fu_started:
                    self.invalid = True
                if end and self.fu_started:
                    self.fu_complete = True
        if not info.marker:
            return None
        fragmented = any(
            (_video_packet_info(self.codec, item.data) or info).keyframe_fragment
            is not None
            and (_video_packet_info(self.codec, item.data) or info).keyframe
            for item in self.candidate
        )
        usable = (
            self.candidate_keyframe
            and not self.invalid
            and (not fragmented or (self.fu_started and self.fu_complete))
        )
        if not usable:
            self.candidate.clear()
            self.candidate_timestamp = None
            return None
        video = tuple(self.candidate)
        first_arrival = video[0].arrival_time
        audio = tuple(item for item in self.audio if item.arrival_time >= first_arrival)
        self.stop()
        return video, audio


class _Playback(Protocol):
    duration: float | None
    media_tracks: tuple[MediaTrack, ...]

    def start(self) -> None: ...
    def pause(self) -> None: ...
    def resume(self) -> None: ...
    def seek(self, seconds: float) -> None: ...
    def seek_relative(self, delta_seconds: float) -> None: ...
    def receive_packets(
        self, duration: float = 1.0
    ) -> tuple[EncodedMediaPacket, ...]: ...
    def close(self) -> None: ...


@dataclass(frozen=True, slots=True)
class OutboundPacket:
    media_type: str
    packet_type: str
    arrival_time: float
    data: bytes
    initialization: bool = False


@dataclass(frozen=True, slots=True)
class HandoffResult:
    readiness_seconds: dict[str, float]
    buffered_packets: int
    activation_seconds: float


class _OutboundQueue:
    """Bounded output queue protecting initialization and RTCP when possible."""

    def __init__(self, capacity: int) -> None:
        if capacity < 1:
            raise ValueError("capacity must be positive")
        self.capacity = capacity
        self._items: deque[OutboundPacket] = deque()
        self._condition = threading.Condition()
        self.closed = False
        self.drops = 0
        self.high_water = 0

    def put(self, item: OutboundPacket) -> None:
        with self._condition:
            if self.closed:
                return
            if len(self._items) == self.capacity:
                index = next(
                    (
                        i
                        for i, queued in enumerate(self._items)
                        if queued.packet_type == "rtp" and not queued.initialization
                    ),
                    0,
                )
                del self._items[index]
                self.drops += 1
            self._items.append(item)
            self.high_water = max(self.high_water, len(self._items))
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


class _CurrentPlaybackSource:
    def __init__(self, output: RecordedOutput) -> None:
        self.output = output

    def receive_packets(
        self, duration: float = 1.0
    ) -> tuple[EncodedMediaPacket, ...]:
        if not self.output._upstream_enabled.wait(duration):
            return ()
        with self.output._control_lock:
            return self.output._playback.receive_packets(duration)


class RecordedOutput:
    """One stable local output backed by one active file-specific playback."""

    def __init__(
        self,
        playback: _Playback,
        *,
        include_audio: bool = False,
        host: str = LOOPBACK_HOST,
        port: int = 0,
        queue_capacity: int = DEFAULT_QUEUE_CAPACITY,
        startup_timeout: float = STARTUP_TIMEOUT,
        startup_packet_limit: int = STARTUP_PACKET_LIMIT,
        startup_audio_packet_limit: int = STARTUP_AUDIO_PACKET_LIMIT,
    ) -> None:
        if not _is_loopback(host):
            raise ValueError("recorded output must bind to a loopback address")
        self._playback = playback
        self._include_audio = include_audio
        self._host = host
        self._requested_port = port
        if startup_timeout <= 0:
            raise ValueError("startup_timeout must be positive")
        if startup_packet_limit < 1 or startup_audio_packet_limit < 1:
            raise ValueError("startup packet limits must be positive")
        self._startup_timeout = startup_timeout
        self._startup_packet_limit = startup_packet_limit
        self._startup_audio_packet_limit = startup_audio_packet_limit
        self._queue = _OutboundQueue(queue_capacity)
        self._control_lock = threading.Lock()
        self._pipeline_lock = threading.RLock()
        self._send_lock = threading.Lock()
        self._stop = threading.Event()
        self._upstream_enabled = threading.Event()
        self._downstream_playing = threading.Event()
        self._server: socket.socket | None = None
        self._client: socket.socket | None = None
        self._threads: list[threading.Thread] = []
        self._session = secrets.token_hex(8)
        self._channels: dict[tuple[str, str], int] = {}
        self._tracks: tuple[MediaTrack, ...] = ()
        self._configurations: dict[str, TrackConfiguration] = {}
        self._cache: CodecConfigurationCache | None = None
        self._mappers: dict[str, RtpContinuityMapper] = {}
        self._pending_rtcp: dict[str, EncodedMediaPacket] = {}
        self._reinject_pending: set[str] = set()
        self._prepared: _Playback | None = None
        self._fanout: EncodedPacketFanout | None = None
        self._subscription = None
        self._error: Exception | None = None
        self._paused = False
        self._startup_gate: _StartupGate | None = None
        self._startup_error: StartupTimeoutError | None = None

    @property
    def url(self) -> str:
        if self._server is None:
            raise RuntimeError("recorded output is not started")
        return f"rtsp://{self._host}:{self._server.getsockname()[1]}{LOGICAL_PATH}"

    @property
    def host(self) -> str:
        return self._host

    @property
    def port(self) -> int:
        if self._server is None:
            raise RuntimeError("recorded output is not started")
        return self._server.getsockname()[1]

    @property
    def session(self) -> str:
        return self._session

    @property
    def tracks(self) -> tuple[MediaTrack, ...]:
        return self._tracks or self._selected_tracks(self._playback)

    @property
    def queue_drops(self) -> int:
        return self._queue.drops

    @property
    def queue_high_water(self) -> int:
        return self._queue.high_water

    @property
    def error(self) -> Exception | None:
        if self._error is not None:
            return self._error
        return self._fanout.error if self._fanout is not None else None

    @property
    def startup_error(self) -> StartupTimeoutError | None:
        return self._startup_error

    @property
    def startup_packet_high_water(self) -> int:
        return self._startup_gate.packet_high_water if self._startup_gate else 0

    @property
    def startup_audio_high_water(self) -> int:
        return self._startup_gate.audio_high_water if self._startup_gate else 0

    def start(self) -> None:
        if self._server is not None:
            raise RuntimeError("recorded output is already started")
        self._playback.start()
        self._tracks = self._selected_tracks(self._playback)
        self._configurations = track_configurations(self._tracks)
        self._cache = CodecConfigurationCache(self._tracks)
        video = self._configurations.get("video")
        if video is not None and video.codec.upper() in ("H264", "H265"):
            self._startup_gate = _StartupGate(
                video.codec,
                self._startup_packet_limit,
                self._startup_audio_packet_limit,
            )
        self._mappers = {
            media_type: RtpContinuityMapper(
                config.clock_rate, payload_type=config.payload_type
            )
            for media_type, config in self._configurations.items()
        }
        server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        server.bind((self._host, self._requested_port))
        server.listen(1)
        server.settimeout(0.2)
        self._server = server
        self._upstream_enabled.set()
        self._fanout = EncodedPacketFanout(_CurrentPlaybackSource(self))
        self._subscription = self._fanout.subscribe()
        self._fanout.start()
        self._thread("dahua-recorded-process", self._process_loop)
        self._thread("dahua-recorded-accept", self._accept_loop)
        self._thread("dahua-recorded-send", self._sender_loop)

    def seek(self, seconds: float) -> None:
        with self._control_lock:
            self._playback.seek(seconds)
            self._begin_discontinuity(
                time.monotonic(), gate=self._downstream_playing.is_set()
            )

    def seek_relative(self, delta_seconds: float) -> None:
        with self._control_lock:
            self._playback.seek_relative(delta_seconds)
            self._begin_discontinuity(
                time.monotonic(), gate=self._downstream_playing.is_set()
            )

    def pause(self) -> None:
        with self._control_lock:
            self._playback.pause()
            self._upstream_enabled.clear()
            self._paused = True

    def resume(self) -> None:
        with self._control_lock:
            self._playback.resume()
            self._begin_discontinuity(time.monotonic())
            self._upstream_enabled.set()
            self._paused = False

    def prepare(
        self,
        playback: _Playback,
        *,
        target_time: datetime,
        recording_start: datetime,
    ) -> float:
        if target_time.tzinfo is None or recording_start.tzinfo is None:
            raise ValueError("transition timestamps must be timezone-aware")
        seconds = (target_time - recording_start).total_seconds()
        if seconds < 0:
            raise ValueError("target time precedes incoming recording")
        playback.start()
        try:
            require_compatible(
                self._configurations, self._selected_tracks(playback)
            )
            playback.seek(seconds)
            playback.pause()
        except Exception:
            playback.close()
            raise
        previous = self._prepared
        self._prepared = playback
        if previous is not None:
            previous.close()
        return seconds

    def activate_prepared(
        self, *, timeout: float = HANDOFF_TIMEOUT
    ) -> HandoffResult:
        incoming = self._prepared
        if incoming is None:
            raise RuntimeError("no prepared playback is available")
        started = time.monotonic()
        staged: deque[EncodedMediaPacket] = deque(maxlen=HANDOFF_PACKET_LIMIT)
        ready: dict[str, float] = {}
        required = set(self._configurations)
        with self._control_lock:
            incoming.resume()
            deadline = started + timeout
            while time.monotonic() < deadline and set(ready) != required:
                window = min(0.05, max(0.001, deadline - time.monotonic()))
                for packet in incoming.receive_packets(window):
                    staged.append(packet)
                    if packet.packet_type == "rtp" and packet.media_type in required:
                        ready.setdefault(packet.media_type, time.monotonic() - started)
            if set(ready) != required:
                incoming.close()
                self._prepared = None
                missing = ", ".join(sorted(required - set(ready)))
                raise RuntimeError(
                    f"prepared handoff timed out waiting for RTP: {missing}"
                )
            outgoing = self._playback
            self._playback = incoming
            self._prepared = None
            self._begin_discontinuity(
                started, gate=self._downstream_playing.is_set()
            )
            for packet in staged:
                self._process_packet(packet)
        outgoing.close()
        return HandoffResult(
            ready, len(staged), time.monotonic() - started
        )

    def abort_prepared(self) -> None:
        prepared = self._prepared
        self._prepared = None
        if prepared is not None:
            prepared.close()

    def close(self) -> None:
        if self._stop.is_set():
            return
        self._stop.set()
        self._upstream_enabled.set()
        self._downstream_playing.clear()
        self._queue.close()
        if self._fanout is not None:
            self._fanout.close()
        for connection in (self._client, self._server):
            if connection is not None:
                try:
                    connection.shutdown(socket.SHUT_RDWR)
                except OSError:
                    pass
                connection.close()
        self._client = None
        self._server = None
        for thread in self._threads:
            if thread is not threading.current_thread():
                thread.join(timeout=2.0)
        try:
            self.abort_prepared()
        except Exception as exc:
            if self._error is None:
                self._error = exc
        try:
            self._playback.close()
        except Exception as exc:
            if self._error is None:
                self._error = exc

    def _selected_tracks(self, playback: _Playback) -> tuple[MediaTrack, ...]:
        return tuple(
            track
            for track in playback.media_tracks
            if track.media_type == "video"
            or (self._include_audio and track.media_type == "audio")
        )

    def _thread(self, name: str, target: object) -> None:
        assert callable(target)
        thread = threading.Thread(name=name, target=target)
        thread.start()
        self._threads.append(thread)

    def _begin_discontinuity(self, at: float, *, gate: bool = False) -> None:
        with self._pipeline_lock:
            for mapper in self._mappers.values():
                mapper.discontinuity(at)
            assert self._cache is not None
            self._reinject_pending = {
                media_type
                for media_type in self._configurations
                if self._cache.get(media_type) is not None
            }
            self._pending_rtcp.clear()
            self._queue.clear()
            if gate and self._startup_gate is not None:
                self._startup_error = None
                self._startup_gate.arm(at + self._startup_timeout)

    def _process_loop(self) -> None:
        assert self._subscription is not None
        while not self._stop.is_set():
            packet = self._subscription.get(0.1)
            if packet is not None:
                self._process_packet(packet)

    def _process_packet(self, packet: EncodedMediaPacket) -> None:
        with self._pipeline_lock:
            self._process_packet_locked(packet)

    def _process_packet_locked(
        self, packet: EncodedMediaPacket, *, startup_release: bool = False
    ) -> None:
        gate = self._startup_gate
        if gate is not None and gate.active:
            if packet.packet_type != "rtp":
                return
            if packet.media_type == "audio":
                gate.add_audio(packet)
                return
            if packet.media_type != "video":
                return
            ready = gate.add_video(packet)
            if ready is None:
                return
            video, audio = ready
            for item in video:
                self._process_packet_locked(item, startup_release=True)
            for item in audio:
                self._process_packet_locked(item, startup_release=True)
            return
        if (
            not startup_release
            and self._server is not None
            and not self._downstream_playing.is_set()
        ):
            return
        mapper = self._mappers.get(packet.media_type)
        if mapper is None:
            return
        if packet.packet_type == "rtcp":
            translated = rewrite_rtcp(packet.data, mapper)
            if translated is None:
                self._pending_rtcp[packet.media_type] = packet
            else:
                self._queue_packet(packet, translated)
            return
        data = mapper.rewrite(packet)
        if packet.media_type in self._reinject_pending:
            assert self._cache is not None
            initialization = self._cache.get(packet.media_type)
            if initialization is not None:
                timestamp = int.from_bytes(data[4:8], "big")
                media = bytearray(data)
                mapper.next_sequence = int.from_bytes(media[2:4], "big")
                for injected in mapper.initialization_packets(
                    timestamp, initialization.parameter_sets
                ):
                    self._queue_packet(packet, injected, initialization=True)
                media[2:4] = mapper.next_sequence.to_bytes(2, "big")
                mapper.next_sequence = (mapper.next_sequence + 1) & 0xFFFF
                data = bytes(media)
            self._reinject_pending.discard(packet.media_type)
        generated = mapper.generated_sender_report()
        if generated is not None:
            self._queue.put(
                OutboundPacket(
                    packet.media_type, "rtcp", packet.arrival_time, generated
                )
            )
        self._queue_packet(packet, data)
        pending = self._pending_rtcp.pop(packet.media_type, None)
        if pending is not None:
            translated = rewrite_rtcp(pending.data, mapper)
            if translated is not None:
                self._queue_packet(pending, translated)

    def _queue_packet(
        self,
        packet: EncodedMediaPacket,
        data: bytes,
        *,
        initialization: bool = False,
    ) -> None:
        self._queue.put(
            OutboundPacket(
                packet.media_type,
                packet.packet_type,
                packet.arrival_time,
                data,
                initialization,
            )
        )

    def _accept_loop(self) -> None:
        assert self._server is not None
        while not self._stop.is_set():
            try:
                connection, _ = self._server.accept()
            except socket.timeout:
                continue
            except OSError:
                return
            if self._client is not None:
                connection.sendall(_response(453, "Not Enough Bandwidth", "1"))
                connection.close()
                continue
            self._client = connection
            connection.settimeout(0.2)
            try:
                self._client_loop(connection)
            except OSError:
                pass
            except RuntimeError as exc:
                if not self._stop.is_set():
                    self._error = exc
            finally:
                self._downstream_playing.clear()
                connection.close()
                self._client = None

    def _client_loop(self, connection: socket.socket) -> None:
        buffer = bytearray()
        while not self._stop.is_set():
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
                body = build_sdp(self.tracks, self._playback.duration).encode()
                self._send_response(
                    connection,
                    200,
                    cseq,
                    (
                        ("Content-Type", "application/sdp"),
                        ("Content-Base", self.url + "/"),
                    ),
                    body,
                )
            elif method == "SETUP":
                media_type = _media_from_target(target)
                if media_type not in self._configurations:
                    self._send_response(connection, 404, cseq)
                    continue
                rtp, rtcp = _interleaved(headers.get("transport", ""))
                self._channels[(media_type, "rtp")] = rtp
                self._channels[(media_type, "rtcp")] = rtcp
                self._send_response(
                    connection,
                    200,
                    cseq,
                    (
                        ("Session", self._session),
                        ("Transport", f"RTP/AVP/TCP;unicast;interleaved={rtp}-{rtcp}"),
                    ),
                )
            elif method == "PLAY":
                if self._paused:
                    self.resume()
                self._begin_discontinuity(time.monotonic(), gate=True)
                self._send_response(
                    connection, 200, cseq, (("Session", self._session),)
                )
                self._downstream_playing.set()
            elif method == "PAUSE":
                self._downstream_playing.clear()
                if self._startup_gate is not None:
                    with self._pipeline_lock:
                        self._startup_gate.stop()
                self.pause()
                self._send_response(
                    connection, 200, cseq, (("Session", self._session),)
                )
            elif method == "TEARDOWN":
                self._downstream_playing.clear()
                self._send_response(
                    connection, 200, cseq, (("Session", self._session),)
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
        with self._send_lock:
            connection.sendall(_response(status, "OK", cseq, headers, body))

    def _sender_loop(self) -> None:
        while not self._stop.is_set():
            self._expire_startup()
            if not self._downstream_playing.wait(0.1):
                continue
            packet = self._queue.get(0.1)
            if packet is None or not self._downstream_playing.is_set():
                continue
            connection = self._client
            channel = self._channels.get((packet.media_type, packet.packet_type))
            if connection is None or channel is None:
                continue
            frame = b"$" + bytes((channel,)) + len(packet.data).to_bytes(2, "big")
            try:
                with self._send_lock:
                    connection.sendall(frame + packet.data)
            except OSError:
                self._downstream_playing.clear()

    def _expire_startup(self) -> None:
        gate = self._startup_gate
        if gate is None or not gate.active or time.monotonic() < gate.deadline:
            return
        with self._pipeline_lock:
            if not gate.active or time.monotonic() < gate.deadline:
                return
            gate.stop()
            self._startup_error = StartupTimeoutError(
                "downstream startup timed out waiting for a complete keyframe "
                "access unit"
            )
            self._downstream_playing.clear()
            connection = self._client
            if connection is not None:
                try:
                    connection.shutdown(socket.SHUT_RDWR)
                except OSError:
                    pass


def build_sdp(tracks: tuple[MediaTrack, ...], duration: float | None) -> str:
    lines = [
        "v=0",
        "o=- 0 0 IN IP4 127.0.0.1",
        "s=Dahua recorded output",
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


def _is_loopback(host: str) -> bool:
    return host.casefold() == "localhost" or host.startswith("127.")


def _read_request(
    connection: socket.socket, buffer: bytearray
) -> tuple[str, str, dict[str, str]] | None:
    while b"\r\n\r\n" not in buffer:
        try:
            chunk = connection.recv(4096)
        except socket.timeout:
            return None
        if not chunk:
            raise OSError("downstream RTSP client disconnected")
        buffer.extend(chunk)
    head, _, remainder = buffer.partition(b"\r\n\r\n")
    buffer[:] = remainder
    lines = head.decode("iso-8859-1").split("\r\n")
    method, target, _ = lines[0].split(" ", 2)
    headers = {}
    for line in lines[1:]:
        name, separator, value = line.partition(":")
        if separator:
            headers[name.casefold()] = value.strip()
    return method.upper(), target, headers


def _response(
    status: int,
    reason: str,
    cseq: str,
    headers: tuple[tuple[str, str], ...] = (),
    body: bytes = b"",
) -> bytes:
    lines = [f"RTSP/1.0 {status} {reason}", f"CSeq: {cseq}"]
    lines.extend(f"{name}: {value}" for name, value in headers)
    if body:
        lines.append(f"Content-Length: {len(body)}")
    return ("\r\n".join(lines) + "\r\n\r\n").encode() + body


def _media_from_target(target: str) -> str:
    path = urlsplit(target).path.rstrip("/")
    value = path.rsplit("/", 1)[-1]
    if value.startswith("trackID="):
        return value.removeprefix("trackID=")
    return value


def _interleaved(transport: str) -> tuple[int, int]:
    for item in transport.split(";"):
        name, separator, value = item.strip().partition("=")
        if separator and name.casefold() == "interleaved":
            first, dash, second = value.partition("-")
            if dash and first.isdigit() and second.isdigit():
                return int(first), int(second)
    raise RuntimeError("SETUP requires TCP interleaved channels")
