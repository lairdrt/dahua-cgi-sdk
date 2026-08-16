"""Stored snapshot parser."""

from __future__ import annotations

from datetime import datetime
from typing import Any, Mapping
from zoneinfo import ZoneInfo

from ..exceptions import InvalidResponseError
from ..models import Snapshot
from ..recorder_time import localize_recorder_time
from .camera import _response_to_public_channel


def parse_rpc_snapshots(
    response: Mapping[str, Any], *, timezone: ZoneInfo
) -> list[Snapshot]:
    """Parse a successful JPG ``findNextFile`` response."""

    params = response.get("params")
    if not isinstance(params, Mapping):
        raise InvalidResponseError("RPC snapshot response omitted params.")
    found = params.get("found")
    infos = params.get("infos")
    if not isinstance(found, int) or isinstance(found, bool) or found < 0:
        raise InvalidResponseError("RPC snapshot page was malformed.")
    if found == 0 and infos is None:
        return []
    if not isinstance(infos, list):
        raise InvalidResponseError("RPC snapshot page was malformed.")
    if found != len(infos):
        raise InvalidResponseError("RPC snapshot count did not match infos.")
    return [
        _parse_rpc_snapshot(info, index, timezone)
        for index, info in enumerate(infos)
    ]


def _parse_rpc_snapshot(value: Any, index: int, timezone: ZoneInfo) -> Snapshot:
    if not isinstance(value, Mapping):
        raise InvalidResponseError(f"Unable to parse RPC snapshot {index}.")
    try:
        media_type = _required_string(value, "Type")
        if media_type.casefold() != "jpg":
            raise ValueError
        video_stream = value.get("VideoStream")
        if video_stream is not None and (
            not isinstance(video_stream, str) or not video_stream
        ):
            raise TypeError
        return Snapshot(
            channel=_response_to_public_channel(_required_int(value, "Channel")),
            start=localize_recorder_time(
                _parse_datetime(_required_string(value, "StartTime")), timezone
            ),
            end=localize_recorder_time(
                _parse_datetime(_required_string(value, "EndTime")), timezone
            ),
            file_path=_required_string(value, "FilePath"),
            length=_required_int(value, "Length"),
            disk=_required_int(value, "Disk"),
            cluster=_required_int(value, "Cluster"),
            partition=_required_int(value, "Partition"),
            video_stream=video_stream,
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise InvalidResponseError(
            f"Unable to parse RPC snapshot {index}."
        ) from exc


def _required_string(value: Mapping[str, Any], key: str) -> str:
    item = value[key]
    if not isinstance(item, str) or not item:
        raise TypeError
    return item


def _required_int(value: Mapping[str, Any], key: str) -> int:
    item = value[key]
    if not isinstance(item, int) or isinstance(item, bool):
        raise TypeError
    return item


def _parse_datetime(value: str) -> datetime:
    return datetime.strptime(value, "%Y-%m-%d %H:%M:%S")
