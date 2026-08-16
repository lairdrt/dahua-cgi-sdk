"""Recorder-local timezone handling."""

from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from .exceptions import InvalidResponseError

_WIRE_FORMAT = "%Y-%m-%d %H:%M:%S"


def parse_recorder_timezone(response: Any) -> ZoneInfo:
    """Parse the recorder's configured IANA timezone."""
    table = _response_mapping(response, "params", "table")
    description = table.get("TimeZoneDesc")
    if not isinstance(description, str) or not description:
        raise InvalidResponseError(
            "Recorder NTP configuration omitted a valid TimeZoneDesc."
        )
    try:
        return ZoneInfo(description)
    except ZoneInfoNotFoundError as exc:
        raise InvalidResponseError(
            f"Recorder returned an unknown timezone: {description!r}."
        ) from exc


def parse_recorder_current_time(response: Any, timezone: ZoneInfo) -> datetime:
    """Parse current recorder wall time and attach its configured timezone."""
    params = _response_mapping(response, "params")
    value = params.get("time")
    if not isinstance(value, str):
        raise InvalidResponseError("global.getCurrentTime omitted a valid time.")
    try:
        local = datetime.strptime(value, _WIRE_FORMAT)
    except ValueError as exc:
        raise InvalidResponseError(
            "global.getCurrentTime returned an invalid time."
        ) from exc
    return localize_recorder_time(local, timezone)


def localize_recorder_time(value: datetime, timezone: ZoneInfo) -> datetime:
    """Attach recorder timezone rules to an unambiguous naive wall time."""
    if value.tzinfo is not None:
        raise ValueError("recorder wall time must be timezone-naive")
    first = value.replace(tzinfo=timezone, fold=0)
    second = value.replace(tzinfo=timezone, fold=1)
    if first.utcoffset() != second.utcoffset():
        raise InvalidResponseError(
            "Recorder returned a wall time that is ambiguous at a DST transition."
        )
    round_trip = first.astimezone(ZoneInfo("UTC")).astimezone(timezone)
    if round_trip.replace(tzinfo=None) != value:
        raise InvalidResponseError(
            "Recorder returned a wall time that does not exist at a DST transition."
        )
    return first


def recorder_search_time(value: datetime, timezone: ZoneInfo) -> str:
    """Convert an aware instant to an unambiguous recorder wall-clock string."""
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("media search times must be timezone-aware")
    local = value.astimezone(timezone)
    naive = local.replace(tzinfo=None)
    if naive.replace(tzinfo=timezone, fold=0).utcoffset() != naive.replace(
        tzinfo=timezone, fold=1
    ).utcoffset():
        raise ValueError(
            "media search time is ambiguous in the recorder timezone"
        )
    return local.strftime(_WIRE_FORMAT)


def _response_mapping(value: Any, *path: str) -> Mapping[str, Any]:
    current = value
    for name in path:
        if not isinstance(current, Mapping):
            raise InvalidResponseError("Recorder time response was malformed.")
        current = current.get(name)
    if not isinstance(current, Mapping):
        raise InvalidResponseError("Recorder time response was malformed.")
    return current
