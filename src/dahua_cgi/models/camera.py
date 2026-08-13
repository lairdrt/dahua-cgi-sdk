from dataclasses import dataclass
from typing import Literal


@dataclass(frozen=True, slots=True)
class Camera:
    """A recorder-exposed camera channel slot.

    An instance does not imply that a physical camera is configured or
    currently connected. Public channel numbers are 1-based.
    """

    channel: int
    name: str


@dataclass(frozen=True, slots=True)
class StreamProfile:
    """Read-only encoding settings for a camera stream."""

    kind: Literal["main", "sub"]
    codec: str
    width: int
    height: int
    fps: float
    bitrate: int
    bitrate_control: str
    audio_enabled: bool
    audio_codec: str | None
