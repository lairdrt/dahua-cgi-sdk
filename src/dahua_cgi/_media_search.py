"""
Internal media search implementation.
"""

from __future__ import annotations

from datetime import datetime
from types import TracebackType
from typing import Iterator

from ._rpc_connection import _RpcConnection
from .exceptions import InvalidResponseError
from .models import Recording
from .parsers.camera import _public_to_response_channel
from .parsers.recording import parse_rpc_recordings


class _MediaSearch(Iterator[Recording]):
    """
    Implements a recorder-managed media search.

    This class encapsulates the recorder's server-side search object and
    exposes a simple Python iterator.
    """

    def __init__(
        self,
        connection: _RpcConnection,
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
        self._closed = False

    def __iter__(self) -> "_MediaSearch":
        return self

    def __next__(self) -> Recording:
        """
        Return the next recording from the search.
        """

        if self._closed:
            raise StopIteration

        try:
            while True:

                #
                # Return any buffered recordings first.
                #
                if self._buffer:
                    return self._buffer.pop(0)

                #
                # No more data available.
                #
                if self._finished:
                    self.close()
                    raise StopIteration

                #
                # Lazily initialize the recorder search.
                #
                if self._object is None:
                    self._create()
                    self._find()

                #
                # Refill the buffer.
                #
                self._fetch_page()

        except StopIteration:
            raise

        except Exception:
            try:
                self.close()
            except Exception:
                pass
            raise

    def __enter__(self) -> "_MediaSearch":
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        self.close()

    def close(self) -> None:
        """
        Close the recorder search object, if it has been created.
        """

        if self._closed:
            return

        self._closed = True
        self._buffer.clear()

        object_id = self._object
        self._object = None

        if object_id is None:
            return

        for method in ("mediaFileFind.close", "mediaFileFind.destroy"):
            try:
                self._connection.call(method, object_id=object_id)
            except Exception:
                pass

    def _create(self) -> None:
        """
        Create a recorder search object.
        """

        response = self._connection.call(
            "mediaFileFind.factory.create",
        )
        try:
            object_id = response["result"]
            if isinstance(object_id, bool):
                raise TypeError
            self._object = int(object_id)
        except (KeyError, TypeError, ValueError) as exc:
            raise InvalidResponseError(
                "Recorder returned an invalid search object."
            ) from exc

    def _find(self) -> None:
        """
        Initialize the recorder search.
        """

        if self._object is None:
            raise RuntimeError("Search object has not been created.")

        self._connection.call(
            "mediaFileFind.findFile",
            {
                "condition": {
                    "Channel": _public_to_response_channel(self._channel),
                    "Dirs": None,
                    "Types": ["dav"],
                    "Order": "Ascent",
                    "Redundant": "Exclusion",
                    "Events": None,
                    "StartTime": self._start.strftime("%Y-%m-%d %H:%M:%S"),
                    "EndTime": self._end.strftime("%Y-%m-%d %H:%M:%S"),
                    "Flags": ["Timing", "Event", "Manual"],
                }
            },
            object_id=self._object,
        )

    def _fetch_page(self) -> None:
        """
        Fetch the next page of recordings from the recorder.
        """

        if self._object is None:
            raise RuntimeError("Search object has not been created.")

        response = self._connection.call(
            "mediaFileFind.findNextFile",
            {"count": 100},
            object_id=self._object,
        )
        recordings = parse_rpc_recordings(response)

        self._buffer.extend(recordings)

        #
        # If the recorder returned no recordings,
        # the search is complete.
        #
        if not recordings:
            self._finished = True
