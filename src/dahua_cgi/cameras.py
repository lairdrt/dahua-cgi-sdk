"""Read-only camera service."""

from __future__ import annotations

from typing import Any

from ._connection import _Connection
from ._rpc_connection import _RpcConnection
from .exceptions import InvalidResponseError
from .models import Camera, StreamProfile
from .parsers.camera import parse_cameras, parse_stream_profiles


class CameraService:
    """Provides RPC camera discovery/configuration and CGI snapshots."""

    def __init__(
        self, connection: _RpcConnection, *, snapshot_connection: _Connection
    ) -> None:
        self._connection = connection
        self._snapshot_connection = snapshot_connection

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

    def snapshot(self, channel: int) -> bytes:
        """Return a JPEG snapshot using the recorder's binary CGI endpoint."""

        self._validate_channel(channel)
        response = self._snapshot_connection.get(
            "/cgi-bin/snapshot.cgi", params={"channel": channel}
        )
        if response.status_code != 200:
            raise InvalidResponseError(
                f"Unexpected HTTP status code: {response.status_code}"
            )
        content_type = response.headers.get("Content-Type", "")
        media_type = content_type.partition(";")[0].strip().lower()
        if media_type != "image/jpeg":
            raise InvalidResponseError(
                f"Unexpected snapshot content type: {content_type or 'missing'}."
            )
        content = response.content
        if not content.startswith(b"\xff\xd8") or not content.endswith(b"\xff\xd9"):
            raise InvalidResponseError(
                "Recorder returned malformed JPEG snapshot data."
            )
        return content

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
