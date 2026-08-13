"""Parsers for Dahua camera configuration responses."""

from __future__ import annotations

import re
from typing import Literal

from ..exceptions import InvalidResponseError
from ..models import Camera, StreamProfile
from .cgi import parse_cgi_properties

_CHANNEL_TITLE_PATTERN = re.compile(r"^table\.ChannelTitle\[(\d+)]\.Name$")
_CAMERA_PROPERTY_PATTERN = re.compile(r"^camera\[(\d+)]\.(.+)$")


def parse_cameras(inventory_text: str, channel_titles_text: str) -> tuple[Camera, ...]:
    """Parse a key=value camera inventory into public camera models."""

    titles = {
        _response_to_public_channel(int(match.group(1))): value
        for key, value in parse_cgi_properties(channel_titles_text).items()
        if (match := _CHANNEL_TITLE_PATTERN.fullmatch(key)) is not None
    }
    entries: dict[int, dict[str, str]] = {}
    for key, value in parse_cgi_properties(inventory_text).items():
        match = _CAMERA_PROPERTY_PATTERN.fullmatch(key)
        if match is not None:
            entries.setdefault(int(match.group(1)), {})[match.group(2)] = value

    cameras = []
    for entry in entries.values():
        if entry.get("Type", "").casefold() == "compose":
            continue

        try:
            response_channel = int(entry["UniqueChannel"])
        except (KeyError, ValueError) as exc:
            raise InvalidResponseError(
                "Camera inventory entry has an invalid UniqueChannel."
            ) from exc

        configured = _parse_boolean(entry.get("Enable"), property_name="Enable")
        channel = _response_to_public_channel(response_channel)
        cameras.append(
            Camera(
                channel=channel,
                name=titles.get(channel, ""),
                configured=configured,
                address=_metadata(entry, "Address", configured=configured),
                device_type=_metadata(
                    entry, "DeviceInfo.DeviceType", configured=configured
                ),
                serial_number=_metadata(
                    entry, "DeviceInfo.SerialNo", configured=configured
                ),
                mac_address=_metadata(entry, "DeviceInfo.Mac", configured=configured),
                protocol=_metadata(entry, "Protocol", configured=configured),
            )
        )

    return tuple(sorted(cameras, key=lambda camera: camera.channel))


def parse_stream_profiles(text: str, *, channel: int) -> tuple[StreamProfile, ...]:
    """Parse the normal main stream and first substream for ``channel``."""

    values = parse_cgi_properties(text)
    config_index = _public_to_response_channel(channel)
    profiles = [
        _parse_stream_profile(
            values,
            prefix=f"table.Encode[{config_index}].MainFormat[0]",
            kind="main",
        )
    ]

    sub_prefix = f"table.Encode[{config_index}].ExtraFormat[0]"
    if any(key.startswith(f"{sub_prefix}.") for key in values):
        profiles.append(_parse_stream_profile(values, prefix=sub_prefix, kind="sub"))

    return tuple(profiles)


def _parse_stream_profile(
    values: dict[str, str],
    *,
    prefix: str,
    kind: Literal["main", "sub"],
) -> StreamProfile:
    def required(name: str) -> str:
        key = f"{prefix}.{name}"
        try:
            return values[key]
        except KeyError as exc:
            raise InvalidResponseError(
                f"Missing required stream property: {key}."
            ) from exc

    try:
        width = int(required("Video.Width"))
        height = int(required("Video.Height"))
        fps = float(required("Video.FPS"))
        bitrate = int(required("Video.BitRate"))
    except ValueError as exc:
        raise InvalidResponseError(
            f"Invalid numeric stream property for {prefix}."
        ) from exc

    audio_enabled_value = required("AudioEnable").lower()
    if audio_enabled_value not in {"true", "false"}:
        raise InvalidResponseError(f"Invalid audio enable value for {prefix}.")

    audio_enabled = audio_enabled_value == "true"

    return StreamProfile(
        kind=kind,
        codec=required("Video.Compression"),
        width=width,
        height=height,
        fps=fps,
        bitrate=bitrate,
        bitrate_control=required("Video.BitRateControl"),
        audio_enabled=audio_enabled,
        audio_codec=values.get(f"{prefix}.Audio.Compression"),
    )


def _response_to_public_channel(channel: int) -> int:
    """Convert a zero-based response channel to a public/request channel."""

    return channel + 1


def _public_to_response_channel(channel: int) -> int:
    """Convert a public/request channel to a zero-based response channel."""

    return channel - 1


def _parse_boolean(value: str | None, *, property_name: str) -> bool:
    if value is None or value.casefold() not in {"true", "false"}:
        raise InvalidResponseError(f"Invalid camera inventory {property_name} value.")
    return value.casefold() == "true"


def _metadata(
    entry: dict[str, str], property_name: str, *, configured: bool
) -> str | None:
    if not configured:
        return None
    value = entry.get(property_name, "").strip()
    return value or None
