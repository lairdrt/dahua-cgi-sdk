"""
Recording parser.
"""

from __future__ import annotations

from datetime import datetime
from typing import Mapping

from ..exceptions import InvalidResponseError
from ..models import Recording


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
) -> list[str]:
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

    return result