"""
Media service.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime
from functools import partial
from typing import Iterator
from zoneinfo import ZoneInfo

from ._media_search import _MediaSearch
from ._rpc_connection import _RpcConnection
from .exceptions import InvalidResponseError
from .models import Recording, Snapshot
from .parsers.recording import parse_rpc_recordings
from .parsers.snapshot import parse_rpc_snapshots
from .playback import RecordingPlayback


class MediaService:
    """
    Provides access to recorder media.
    """

    def __init__(
        self,
        connection: _RpcConnection,
        *,
        timezone: ZoneInfo,
        playback_factory: (
            Callable[[Recording, bool], RecordingPlayback] | None
        ) = None,
    ) -> None:
        self._connection = connection
        self._timezone = timezone
        self._playback_factory = playback_factory

    def recordings(
        self,
        *,
        channel: int,
        start: datetime,
        end: datetime,
    ) -> Iterator[Recording]:
        """
        Search for recordings.
        """

        if channel < 1:
            raise ValueError("channel must be at least 1")
        self._validate_search_times(start, end)

        return _MediaSearch(
            connection=self._connection,
            channel=channel,
            start=start,
            end=end,
            timezone=self._timezone,
            parse_page=partial(parse_rpc_recordings, timezone=self._timezone),
        )

    def snapshots(
        self,
        *,
        channel: int,
        start: datetime,
        end: datetime,
    ) -> Iterator[Snapshot]:
        """Search for JPEG snapshots stored on the recorder."""

        if channel < 1:
            raise ValueError("channel must be at least 1")
        self._validate_search_times(start, end)

        return _MediaSearch(
            connection=self._connection,
            channel=channel,
            start=start,
            end=end,
            timezone=self._timezone,
            media_type="jpg",
            parse_page=partial(parse_rpc_snapshots, timezone=self._timezone),
        )

    @staticmethod
    def _validate_search_times(start: datetime, end: datetime) -> None:
        if (
            start.tzinfo is None
            or start.utcoffset() is None
            or end.tzinfo is None
            or end.utcoffset() is None
        ):
            raise ValueError("media search times must be timezone-aware")

    def recording_bytes(self, recording: Recording) -> bytes:
        """Retrieve the stored DAV bytes for a recording."""

        response = self._connection.get(
            f"/cgi-bin/RPC_Loadfile/{recording.file_path.lstrip('/')}"
        )

        if response.status_code != 200:
            raise InvalidResponseError(
                f"Unexpected HTTP status code: {response.status_code}"
            )

        return response.content

    def playback(
        self, recording: Recording, *, audio: bool = False
    ) -> RecordingPlayback:
        """Create playback, optionally requesting its discovered audio track."""

        if self._playback_factory is None:
            raise RuntimeError("Recorded playback is not configured.")
        return self._playback_factory(recording, audio)

    def snapshot_bytes(self, snapshot: Snapshot) -> bytes:
        """Retrieve and validate the JPEG bytes for a stored snapshot."""

        response = self._connection.get(
            f"/cgi-bin/RPC_Loadfile/{snapshot.file_path.lstrip('/')}",
            stream=True,
        )
        try:
            if response.status_code != 200:
                raise InvalidResponseError(
                    f"Unexpected HTTP status code: {response.status_code}"
                )
            response.raw.enforce_content_length = False
            payload = bytearray()
            for chunk in response.raw.stream(8192, decode_content=False):
                payload.extend(chunk)
                if payload.endswith(b"\xff\xd9"):
                    break
        finally:
            response.close()

        content = bytes(payload)
        if not content.startswith(b"\xff\xd8") or not content.endswith(b"\xff\xd9"):
            raise InvalidResponseError(
                "Recorder returned malformed stored snapshot JPEG data."
            )
        return content
