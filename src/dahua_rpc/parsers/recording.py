"""
Recording parser.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Mapping
from zoneinfo import ZoneInfo

from ..exceptions import InvalidResponseError
from ..models import Recording
from ..recorder_time import localize_recorder_time
from .camera import _response_to_public_channel


def parse_rpc_recordings(
    response: Mapping[str, Any], *, timezone: ZoneInfo
) -> list[Recording]:
    """Parse a successful RPC ``findNextFile`` response."""

    params = response.get("params")
    if not isinstance(params, Mapping):
        raise InvalidResponseError("RPC recording response omitted params.")
    found = params.get("found")
    infos = params.get("infos")
    if (
        not isinstance(found, int)
        or isinstance(found, bool)
        or found < 0
    ):
        raise InvalidResponseError("RPC recording page was malformed.")
    if found == 0 and infos is None:
        return []
    if not isinstance(infos, list):
        raise InvalidResponseError("RPC recording page was malformed.")
    if found != len(infos):
        raise InvalidResponseError("RPC recording count did not match infos.")
    return [
        _parse_rpc_recording(info, index, timezone)
        for index, info in enumerate(infos)
    ]


def _parse_rpc_recording(value: Any, index: int, timezone: ZoneInfo) -> Recording:
    if not isinstance(value, Mapping):
        raise InvalidResponseError(f"Unable to parse RPC recording {index}.")
    try:
        events = value.get("Events", [])
        flags = value.get("Flags", [])
        if not isinstance(events, list) or not all(
            isinstance(item, str) for item in events
        ):
            raise TypeError
        if not isinstance(flags, list) or not all(
            isinstance(item, str) for item in flags
        ):
            raise TypeError
        channel = _required_int(value, "Channel")
        length = _required_int(value, "Length")
        return Recording(
            channel=_response_to_public_channel(channel),
            cluster=_required_int(value, "Cluster"),
            cut_length=_optional_int(value, "CutLength", 0),
            disk=_required_int(value, "Disk"),
            end_time=localize_recorder_time(
                _parse_datetime(_required_string(value, "EndTime")), timezone
            ),
            events=tuple(events),
            file_path=_required_string(value, "FilePath"),
            flags=tuple(flags),
            length=length,
            partition=_required_int(value, "Partition"),
            start_time=localize_recorder_time(
                _parse_datetime(_required_string(value, "StartTime")), timezone
            ),
            type=_required_string(value, "Type"),
            video_stream=_required_string(value, "VideoStream"),
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise InvalidResponseError(
            f"Unable to parse RPC recording {index}."
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


def _optional_int(value: Mapping[str, Any], key: str, default: int) -> int:
    if key not in value:
        return default
    return _required_int(value, key)


def parse_recordings(
    values: Mapping[str, str],
) -> list[Recording]:
    """
    Parse recording search results.
    """

    return [
        _parse_recording(values, index)
        for index in sorted(_collect_indices(values))
    ]


def _collect_indices(
    values: Mapping[str, str],
) -> set[int]:
    """
    Collect all recording indices contained in the response.
    """

    indices: set[int] = set()

    for key in values:

        if not key.startswith("items["):
            continue

        end = key.find("]")

        if end == -1:
            continue

        try:
            indices.add(int(key[6:end]))
        except ValueError:
            continue

    return indices


def _parse_recording(
    values: Mapping[str, str],
    index: int,
) -> Recording:
    """
    Parse a single recording.
    """

    prefix = f"items[{index}]."

    try:
        return Recording(
            channel=int(values[prefix + "Channel"]),
            cluster=int(values[prefix + "Cluster"]),
            cut_length=int(values[prefix + "CutLength"]),
            disk=int(values[prefix + "Disk"]),
            end_time=_parse_datetime(values[prefix + "EndTime"]),
            events=_parse_string_list(values, prefix + "Events"),
            file_path=values[prefix + "FilePath"],
            flags=_parse_string_list(values, prefix + "Flags"),
            length=int(values[prefix + "Length"]),
            partition=int(values[prefix + "Partition"]),
            start_time=_parse_datetime(values[prefix + "StartTime"]),
            type=values[prefix + "Type"],
            video_stream=values[prefix + "VideoStream"],
        )

    except (KeyError, ValueError) as exc:
        raise InvalidResponseError(
            f"Unable to parse recording {index}."
        ) from exc


def _parse_datetime(
    value: str,
) -> datetime:
    """
    Parse a Dahua date/time value.
    """

    return datetime.strptime(
        value,
        "%Y-%m-%d %H:%M:%S",
    )


def _parse_string_list(
    values: Mapping[str, str],
    prefix: str,
) -> tuple[str, ...]:
    """
    Parse a Dahua indexed string list.

    Example:

        Events[0]
        Events[1]

    or

        Flags[0]
        Flags[1]
    """

    result: list[str] = []

    index = 0

    while True:

        value = values.get(f"{prefix}[{index}]")

        if value is None:
            break

        result.append(value)

        index += 1

    return tuple(result)
