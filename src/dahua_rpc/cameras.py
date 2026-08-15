"""Read-only camera service."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from ._rpc_connection import _RpcConnection
from .exceptions import InvalidResponseError
from .live import LiveStream, LiveStreamName
from .models import Camera, StreamProfile
from .parsers.camera import parse_cameras, parse_stream_profiles


class CameraService:
    """Provides RPC camera discovery and configuration."""

    def __init__(
        self,
        connection: _RpcConnection,
        *,
        live_stream_factory: (
            Callable[[int, LiveStreamName, int, StreamProfile], LiveStream] | None
        ) = None,
    ) -> None:
        self._connection = connection
        self._live_stream_factory = live_stream_factory

    def list(self) -> tuple[Camera, ...]:
        inventory = self._params(
            self._connection.call("LogicDeviceManager.getCameraAll")
        ).get("camera")
        titles = self._params(
            self._connection.call("configManager.getConfig", {"name": "ChannelTitle"})
        ).get("table")
        states = self._params(
            self._connection.call(
                "LogicDeviceManager.getCameraState", {"uniqueChannels": [-1]}
            )
        ).get("states")
        return parse_cameras(inventory, titles, states)

    def get(self, channel: int) -> Camera:
        self._validate_channel(channel)
        for camera in self.list():
            if camera.channel == channel:
                return camera
        raise InvalidResponseError(
            f"Recorder response did not include camera channel {channel}."
        )

    def streams(self, channel: int) -> tuple[StreamProfile, ...]:
        self._validate_channel(channel)
        table = self._params(
            self._connection.call("configManager.getConfig", {"name": "Encode"})
        ).get("table")
        return parse_stream_profiles(table, channel=channel)

    def live_stream(
        self, *, channel: int, stream: LiveStreamName = "Main"
    ) -> LiveStream:
        """Create an RTSP live-video session for a configured camera stream."""

        self._validate_channel(channel)
        if stream not in ("Main", "Extra1"):
            raise ValueError("stream must be 'Main' or 'Extra1'")
        camera = self.get(channel)
        if not camera.configured:
            raise InvalidResponseError(
                f"Camera channel {channel} is not configured."
            )
        kind = "main" if stream == "Main" else "sub"
        profiles = self.streams(channel)
        profile = next((item for item in profiles if item.kind == kind), None)
        if profile is None:
            raise InvalidResponseError(
                f"Camera channel {channel} does not expose stream {stream}."
            )
        if self._live_stream_factory is None:
            raise RuntimeError("Live streaming is not configured.")
        subtype = 0 if stream == "Main" else 1
        return self._live_stream_factory(channel, stream, subtype, profile)

    @staticmethod
    def _params(response: Any) -> dict[str, Any]:
        params = response.get("params") if isinstance(response, dict) else None
        if not isinstance(params, dict):
            raise InvalidResponseError("RPC response omitted valid params.")
        return params

    @staticmethod
    def _validate_channel(channel: int) -> None:
        if channel < 1:
            raise ValueError("channel must be at least 1")
