"""Read-only camera service."""

from __future__ import annotations

from typing import Any

from ._rpc_connection import _RpcConnection
from .exceptions import InvalidResponseError
from .models import Camera, StreamProfile
from .parsers.camera import parse_cameras, parse_stream_profiles


class CameraService:
    """Provides RPC camera discovery and configuration."""

    def __init__(self, connection: _RpcConnection) -> None:
        self._connection = connection

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
