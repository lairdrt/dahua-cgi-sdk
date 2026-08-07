"""
Internal media search implementation.
"""

from __future__ import annotations

from datetime import datetime
from typing import Iterator

from requests import Response

from ._connection import _Connection
from .exceptions import InvalidResponseError
from .models import Recording
from .parsers import parse_cgi_properties


class _MediaSearch(Iterator[Recording]):
    """
    Implements a recorder-managed media search.

    This class encapsulates the recorder's server-side search object and
    exposes a simple Python iterator.
    """

    def __init__(
        self,
        connection: _Connection,
        *,
        channel: int,
        start: datetime,
        end: datetime,
    ) -> None:

        self._connection = connection

        self._channel = channel
        self._start = start
        self._end = end

        self._object: int | None = None

        self._buffer: list[Recording] = []

        self._finished = False

    def __iter__(self) -> "_MediaSearch":
        return self

    def __next__(self) -> Recording:

        if self._finished:
            raise StopIteration

        if self._object is None:
            self._create()
            self._find()
            self._fetch_page()

            self._finished = True

        raise StopIteration

    def _create(self) -> None:
        """
        Create a recorder search object.
        """

        response = self._connection.get(
            "/cgi-bin/mediaFileFind.cgi",
            params={
                "action": "factory.create",
            },
        )

        self._object = self._parse_object(response)

    @staticmethod
    def _parse_object(response: Response) -> int:
        """
        Parse the recorder search object identifier.
        """

        values = parse_cgi_properties(response.text)

        try:
            return int(values["result"])

        except KeyError as exc:
            raise InvalidResponseError(
                "Recorder did not return a search object."
            ) from exc

        except ValueError as exc:
            raise InvalidResponseError(
                "Recorder returned an invalid search object."
            ) from exc

    def _find(self) -> None:
        """
        Initialize the recorder search.
        """

        if self._object is None:
            raise RuntimeError("Search object has not been created.")

        self._connection.get(
            "/cgi-bin/mediaFileFind.cgi",
            params={
                "action": "findFile",
                "object": self._object,
                "condition.Channel": self._channel,
                "condition.StartTime": self._start.strftime("%Y-%m-%d %H:%M:%S"),
                "condition.EndTime": self._end.strftime("%Y-%m-%d %H:%M:%S"),
            },
        )

    def _fetch_page(self) -> None:
        """
        Fetch the next page of recordings from the recorder.
        """

        if self._object is None:
            raise RuntimeError("Search object has not been created.")


#        response = self._connection.get(
#            "/cgi-bin/mediaFileFind.cgi",
#            params={
#                "action": "findNextFile",
#                "object": self._object,
#                "count": 100,
#            },
#        )
