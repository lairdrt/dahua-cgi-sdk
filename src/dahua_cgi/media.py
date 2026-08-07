"""
Media service.
"""

from __future__ import annotations

from datetime import datetime
from typing import Iterator

from ._connection import _Connection
from ._media_search import _MediaSearch
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
