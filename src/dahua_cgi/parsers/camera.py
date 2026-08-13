"""Parsers for Dahua camera configuration responses."""

from __future__ import annotations

import re
from typing import Literal

from ..exceptions import InvalidResponseError
from ..models import Camera, StreamProfile
from .cgi import parse_cgi_properties

_CHANNEL_TITLE_PATTERN = re.compile(r"^table\.ChannelTitle\[(\d+)]\.Name$")


def parse_cameras(text: str) -> tuple[Camera, ...]:
    """Parse channel-title configuration into public camera models."""

    values = parse_cgi_properties(text)
    cameras = [
        Camera(channel=int(match.group(1)) + 1, name=value)
        for key, value in values.items()
        if (match := _CHANNEL_TITLE_PATTERN.fullmatch(key)) is not None
    ]

    return tuple(sorted(cameras, key=lambda camera: camera.channel))


def parse_stream_profiles(text: str, *, channel: int) -> tuple[StreamProfile, ...]:
    """Parse the normal main stream and first substream for ``channel``."""

    values = parse_cgi_properties(text)
    config_index = channel - 1
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
