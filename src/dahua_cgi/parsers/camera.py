"""Parsers for Dahua RPC camera responses."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any, Literal

from ..exceptions import InvalidResponseError
from ..models import Camera, StreamProfile


def parse_cameras(
    inventory: Any, channel_titles: Any, states: Any
) -> tuple[Camera, ...]:
    """Combine RPC inventory, titles, and state into public camera models."""

    titles = _parse_titles(_sequence(channel_titles, "channel titles"))
    connected = _parse_states(_sequence(states, "camera states"))
    cameras = []
    for value in _sequence(inventory, "camera inventory"):
        entry = _mapping(value, "camera inventory entry")
        entry_type = entry.get("Type")
        if not isinstance(entry_type, str):
            raise InvalidResponseError("Camera inventory entry has an invalid Type.")
        if entry_type.casefold() == "compose":
            continue
        response_channel = entry.get("UniqueChannel")
        if not _is_int(response_channel):
            raise InvalidResponseError(
                "Camera inventory entry has an invalid UniqueChannel."
            )
        configured = entry.get("Enable")
        if not isinstance(configured, bool):
            raise InvalidResponseError("Camera inventory entry has an invalid Enable.")
        device = _mapping(entry.get("DeviceInfo"), "camera DeviceInfo")
        channel = _response_to_public_channel(response_channel)
        cameras.append(
            Camera(
                channel=channel,
                name=titles.get(channel, ""),
                configured=configured,
                connected=connected.get(channel, False),
                address=_metadata(device, "Address", configured),
                device_type=_metadata(device, "DeviceType", configured),
                serial_number=_metadata(device, "SerialNo", configured),
                mac_address=_metadata(device, "Mac", configured),
                protocol=_metadata(device, "ProtocolType", configured),
            )
        )
    return tuple(sorted(cameras, key=lambda camera: camera.channel))


def parse_stream_profiles(table: Any, *, channel: int) -> tuple[StreamProfile, ...]:
    entries = _sequence(table, "Encode configuration")
    try:
        encode = _mapping(
            entries[_public_to_response_channel(channel)], "Encode channel"
        )
    except IndexError as exc:
        raise InvalidResponseError(
            f"Encode configuration did not include camera channel {channel}."
        ) from exc
    main = _sequence(encode.get("MainFormat"), "MainFormat")
    if not main:
        raise InvalidResponseError("MainFormat did not include a stream profile.")
    profiles = [_parse_stream(main[0], "main")]
    if encode.get("ExtraFormat") is not None:
        extra = _sequence(encode["ExtraFormat"], "ExtraFormat")
        if extra:
            profiles.append(_parse_stream(extra[0], "sub"))
    return tuple(profiles)


def _parse_titles(entries: Sequence[Any]) -> dict[int, str]:
    titles = {}
    for index, value in enumerate(entries):
        name = _mapping(value, "channel title").get("Name")
        if not isinstance(name, str):
            raise InvalidResponseError("Channel title entry has an invalid Name.")
        titles[_response_to_public_channel(index)] = name
    return titles


def _parse_states(entries: Sequence[Any]) -> dict[int, bool]:
    states = {}
    for value in entries:
        entry = _mapping(value, "camera state")
        channel = entry.get("channel")
        if not _is_int(channel):
            raise InvalidResponseError("Camera state has an invalid channel.")
        state = entry.get("connectionState")
        if state is not None and not isinstance(state, str):
            raise InvalidResponseError("Camera state has an invalid connectionState.")
        states[_response_to_public_channel(channel)] = state == "Connected"
    return states


def _parse_stream(value: Any, kind: Literal["main", "sub"]) -> StreamProfile:
    profile = _mapping(value, f"{kind} stream profile")
    video = _mapping(profile.get("Video"), f"{kind} stream Video")
    audio = profile.get("Audio")
    audio_values = _mapping(audio, f"{kind} stream Audio") if audio is not None else {}
    return StreamProfile(
        kind=kind,
        codec=_string(video, "Compression"),
        width=_integer(video, "Width"),
        height=_integer(video, "Height"),
        fps=_number(video, "FPS"),
        bitrate=_integer(video, "BitRate"),
        bitrate_control=_string(video, "BitRateControl"),
        audio_enabled=_boolean(profile, "AudioEnable"),
        audio_codec=_optional_string(audio_values, "Compression"),
    )


def _response_to_public_channel(channel: int) -> int:
    return channel + 1


def _public_to_response_channel(channel: int) -> int:
    return channel - 1


def _mapping(value: Any, name: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise InvalidResponseError(f"RPC response has an invalid {name}.")
    return value


def _sequence(value: Any, name: str) -> Sequence[Any]:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes)):
        raise InvalidResponseError(f"RPC response has an invalid {name}.")
    return value


def _is_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _string(values: Mapping[str, Any], name: str) -> str:
    value = values.get(name)
    if not isinstance(value, str):
        raise InvalidResponseError(f"Stream profile has an invalid {name}.")
    return value


def _optional_string(values: Mapping[str, Any], name: str) -> str | None:
    value = values.get(name)
    if value is not None and not isinstance(value, str):
        raise InvalidResponseError(f"Stream profile has an invalid {name}.")
    return value


def _integer(values: Mapping[str, Any], name: str) -> int:
    value = values.get(name)
    if not _is_int(value):
        raise InvalidResponseError(f"Stream profile has an invalid {name}.")
    return value


def _number(values: Mapping[str, Any], name: str) -> float:
    value = values.get(name)
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        raise InvalidResponseError(f"Stream profile has an invalid {name}.")
    return float(value)


def _boolean(values: Mapping[str, Any], name: str) -> bool:
    value = values.get(name)
    if not isinstance(value, bool):
        raise InvalidResponseError(f"Stream profile has an invalid {name}.")
    return value


def _metadata(values: Mapping[str, Any], name: str, configured: bool) -> str | None:
    if not configured:
        return None
    value = values.get(name)
    if value is None:
        return None
    if not isinstance(value, str):
        raise InvalidResponseError(f"Camera metadata has an invalid {name}.")
    return value.strip() or None
