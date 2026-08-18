"""Public recorded-media RTSP playback abstraction."""

from __future__ import annotations

import math
import re
import time
from collections.abc import Callable
from dataclasses import dataclass
from enum import Enum, auto
from types import TracebackType

from ._rtsp_connection import _RtspConnection
from .exceptions import PlaybackStateError
from .models import EncodedMediaPacket, MediaTrack, Recording


@dataclass(frozen=True, slots=True)
class RtpReceipt:
    """Observed interleaved RTP media without decoding it."""

    packets: int
    bytes: int
    first_timestamp: int | None
    last_timestamp: int | None


class _PlaybackState(Enum):
    NEW = auto()
    PLAYING = auto()
    PAUSED = auto()
    CLOSED = auto()


class RecordingPlayback:
    """One synchronous RTSP playback session for one indexed recording."""

    def __init__(
        self,
        connection: _RtspConnection,
        recording: Recording,
        *,
        on_close: Callable[[RecordingPlayback], None] | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._connection = connection
        self.recording = recording
        self._on_close = on_close
        self._clock = clock
        self._state = _PlaybackState.NEW
        self._anchor_npt = 0.0
        self._anchor_time: float | None = None
        self.packets_received = 0
        self.bytes_received = 0
        self.last_rtp_timestamp: int | None = None

    @property
    def duration(self) -> float | None:
        return self._connection.duration

    @property
    def video_codec(self) -> str | None:
        return self._connection.video_codec

    @property
    def video_control(self) -> str | None:
        return self._connection.video_control

    @property
    def returned_range(self) -> str | None:
        return self._connection.returned_range

    @property
    def media_tracks(self) -> tuple[MediaTrack, ...]:
        """Media tracks discovered at start; empty before DESCRIBE completes."""

        return self._connection.media_tracks

    @property
    def position(self) -> float:
        """Current NPT position in seconds from the recording beginning."""

        if self._state is _PlaybackState.NEW:
            raise PlaybackStateError(
                "position is not available before playback starts."
            )
        position = self._anchor_npt
        if self._state is _PlaybackState.PLAYING and self._anchor_time is not None:
            position += self._clock() - self._anchor_time
        return self._clamp(position)

    def start(self) -> None:
        self._require(_PlaybackState.NEW, operation="start")
        self._connection.start()
        self._set_playing_anchor(
            _range_start(self._connection.returned_range, fallback=0.0)
        )
        self._state = _PlaybackState.PLAYING

    def pause(self) -> None:
        self._require(_PlaybackState.PLAYING, operation="pause")
        position = self.position
        returned_range = self._connection.pause()
        self._anchor_npt = self._clamp(
            _range_start(returned_range, fallback=position)
        )
        self._anchor_time = None
        self._state = _PlaybackState.PAUSED

    def resume(self) -> None:
        self._require(_PlaybackState.PAUSED, operation="resume")
        position = self._anchor_npt
        returned_range = self._connection.play()
        self._set_playing_anchor(_range_start(returned_range, fallback=position))
        self._state = _PlaybackState.PLAYING

    def seek(self, seconds: float) -> None:
        if not math.isfinite(seconds):
            raise ValueError("seconds must be finite")
        if seconds < 0:
            raise ValueError("seconds must be non-negative")
        if self.duration is not None and seconds > self.duration:
            raise ValueError("seconds exceeds the recording duration")
        self._seek_absolute(seconds)

    def seek_relative(self, delta_seconds: float) -> None:
        """Seek forward or backward relative to the current NPT position."""

        if not math.isfinite(delta_seconds):
            raise ValueError("delta_seconds must be finite")
        self._require_active("seek_relative")
        target = self._clamp(self.position + delta_seconds)
        self._seek_absolute(target)

    def receive(self, duration: float = 1.0) -> RtpReceipt:
        self._require(_PlaybackState.PLAYING, operation="receive")
        if duration <= 0:
            raise ValueError("duration must be greater than zero")
        result = self._connection.receive(duration)
        self.packets_received += result.packets
        self.bytes_received += result.bytes
        self.last_rtp_timestamp = result.last_timestamp
        return RtpReceipt(
            result.packets,
            result.bytes,
            result.first_timestamp,
            result.last_timestamp,
        )

    def receive_packets(
        self, duration: float = 1.0
    ) -> tuple[EncodedMediaPacket, ...]:
        """Receive ordered encoded packets from this playback's sole reader.

        Calls to ``receive`` and ``receive_packets`` must not overlap. A future
        fan-out layer may distribute one reader's results to local consumers.
        """

        self._require(_PlaybackState.PLAYING, operation="receive_packets")
        if duration <= 0:
            raise ValueError("duration must be greater than zero")
        return self._connection.receive_packets(duration)

    def close(self) -> None:
        if self._state is _PlaybackState.CLOSED:
            return
        if self._state is _PlaybackState.PLAYING:
            self._anchor_npt = self.position
            self._anchor_time = None
        try:
            if self._state in (_PlaybackState.PLAYING, _PlaybackState.PAUSED):
                self._connection.teardown()
        finally:
            self._connection.close_socket()
            self._state = _PlaybackState.CLOSED
            if self._on_close is not None:
                self._on_close(self)

    def __enter__(self) -> RecordingPlayback:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        if exc_type is None:
            self.close()
            return
        try:
            self.close()
        except Exception:
            pass

    def _require(self, state: _PlaybackState, *, operation: str) -> None:
        if self._state is not state:
            raise PlaybackStateError(
                f"{operation} is not valid while playback is "
                f"{self._state.name.lower()}."
            )

    def _require_active(self, operation: str) -> None:
        if self._state not in (_PlaybackState.PLAYING, _PlaybackState.PAUSED):
            raise PlaybackStateError(
                f"{operation} is not valid while playback is "
                f"{self._state.name.lower()}."
            )

    def _seek_absolute(self, seconds: float) -> None:
        self._require_active("seek")
        returned_range = self._connection.play(f"npt={seconds:g}-")
        confirmed = _range_start(returned_range, fallback=seconds)
        self._set_playing_anchor(confirmed)
        self._state = _PlaybackState.PLAYING

    def _set_playing_anchor(self, position: float) -> None:
        self._anchor_npt = self._clamp(position)
        self._anchor_time = self._clock()

    def _clamp(self, position: float) -> float:
        position = max(0.0, position)
        if self.duration is not None:
            position = min(position, self.duration)
        return position


def _range_start(value: str | None, *, fallback: float | None = None) -> float:
    if isinstance(value, str):
        match = re.search(r"(?:^|\s)npt=(\d+(?:\.\d+)?)-", value)
        if match is not None:
            return float(match.group(1))
    if fallback is None:
        raise ValueError("RTSP Range omitted a numeric NPT start")
    return fallback
