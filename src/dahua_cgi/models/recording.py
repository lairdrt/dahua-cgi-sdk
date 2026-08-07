"""
Recording domain object.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime


@dataclass(frozen=True, slots=True)
class Recording:
    """
    Represents a single recording stored on the recorder.
    """

    channel: int
    start_time: datetime
    end_time: datetime

    file_path: str

    event: str | None

    stream: str | None

    length: int | None

    size: int | None

    cluster: int | None

    disk: int | None

    partition: int | None
