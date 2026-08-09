"""
Media service.
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Iterator

from ._connection import _Connection
from ._media_search import _MediaSearch
from .exceptions import InvalidResponseError
from .models import Recording


class MediaService:
    """
    Provides access to recorder media.
    """

    def __init__(self, connection: _Connection) -> None:
        self._connection = connection

    def search(
        self,
        *,
        channel: int,
        start: datetime,
        end: datetime,
    ) -> Iterator[Recording]:
        """
        Search for recordings.
        """

        return _MediaSearch(
            connection=self._connection,
            channel=channel,
            start=start,
            end=end,
        )

    def download(
        self,
        recording: Recording,
        destination: str | Path,
    ) -> Path:
        """
        Download a recording to ``destination``.

        Returns the path written after the recorder successfully supplies the
        recording data.
        """

        response = self._connection.get(
            f"/cgi-bin/RPC_Loadfile/{recording.file_path.lstrip('/')}"
        )

        if response.status_code != 200:
            raise InvalidResponseError(
                f"Unexpected HTTP status code: {response.status_code}"
            )

        target = Path(destination)
        target.write_bytes(response.content)

        return target
