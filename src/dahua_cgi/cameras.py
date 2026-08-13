"""Read-only camera service."""

from __future__ import annotations

from requests import Response

from ._connection import _Connection
from .exceptions import InvalidResponseError
from .models import Camera, StreamProfile
from .parsers.camera import parse_cameras, parse_stream_profiles


class CameraService:
    """Provides camera-channel discovery, stream configuration, and snapshots."""

    def __init__(self, connection: _Connection) -> None:
        self._connection = connection

    def list(self) -> tuple[Camera, ...]:
        """Return all 1-based camera channels exposed by the recorder.

        Channels are enumerated from ``ChannelTitle``. Their presence does not
        indicate that a physical camera is configured or currently connected.
        """

        response = self._get_config("ChannelTitle")
        return parse_cameras(response.text)

    def get(self, channel: int) -> Camera:
        """Return the recorder-exposed slot for a 1-based camera channel."""

        self._validate_channel(channel)

        for camera in self.list():
            if camera.channel == channel:
                return camera

        raise InvalidResponseError(
            f"Recorder response did not include camera channel {channel}."
        )

    def streams(self, channel: int) -> tuple[StreamProfile, ...]:
        """Return the normal main stream and available first substream."""

        self._validate_channel(channel)
        response = self._get_config("Encode")
        return parse_stream_profiles(response.text, channel=channel)

    def snapshot(self, channel: int) -> bytes:
        """Return a JPEG snapshot for a 1-based camera channel."""

        self._validate_channel(channel)
        response = self._connection.get(
            "/cgi-bin/snapshot.cgi",
            params={"channel": channel},
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

    def _get_config(self, name: str) -> Response:
        response = self._connection.get(
            "/cgi-bin/configManager.cgi",
            params={"action": "getConfig", "name": name},
        )

        if response.status_code != 200:
            raise InvalidResponseError(
                f"Unexpected HTTP status code: {response.status_code}"
            )

        return response

    @staticmethod
    def _validate_channel(channel: int) -> None:
        if channel < 1:
            raise ValueError("channel must be at least 1")
