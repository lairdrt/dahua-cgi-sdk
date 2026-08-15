"""Public live-camera RTSP stream abstraction."""

from __future__ import annotations

from collections.abc import Callable
from enum import Enum, auto
from types import TracebackType
from typing import Literal

from ._rtsp_connection import _RtspConnection
from .exceptions import PlaybackStateError
from .models import StreamProfile
from .playback import RtpReceipt

LiveStreamName = Literal["Main", "Extra1"]


class _LiveState(Enum):
    NEW = auto()
    STREAMING = auto()
    CLOSED = auto()


class LiveStream:
    """One synchronous RTSP live-video session for one camera stream."""

    def __init__(
        self,
        connection: _RtspConnection,
        *,
        channel: int,
        stream: LiveStreamName,
        profile: StreamProfile,
        on_close: Callable[[LiveStream], None] | None = None,
    ) -> None:
        self._connection = connection
        self.channel = channel
        self.stream = stream
        self.profile = profile
        self._on_close = on_close
        self._state = _LiveState.NEW
        self.packets_received = 0
        self.bytes_received = 0
        self.last_rtp_timestamp: int | None = None

    @property
    def target(self) -> str:
        """Credential-free RTSP target URI."""

        return self._connection.target

    @property
    def video_codec(self) -> str | None:
        return self._connection.video_codec

    @property
    def video_control(self) -> str | None:
        return self._connection.video_control

    def start(self) -> None:
        self._require(_LiveState.NEW, operation="start")
        self._connection.start()
        self._state = _LiveState.STREAMING

    def receive(self, duration: float = 1.0) -> RtpReceipt:
        self._require(_LiveState.STREAMING, operation="receive")
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

    def close(self) -> None:
        if self._state is _LiveState.CLOSED:
            return
        try:
            if self._state is _LiveState.STREAMING:
                self._connection.teardown()
        finally:
            self._connection.close_socket()
            self._state = _LiveState.CLOSED
            if self._on_close is not None:
                self._on_close(self)

    def __enter__(self) -> LiveStream:
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

    def _require(self, state: _LiveState, *, operation: str) -> None:
        if self._state is not state:
            raise PlaybackStateError(
                f"{operation} is not valid while live stream is "
                f"{self._state.name.lower()}."
            )
