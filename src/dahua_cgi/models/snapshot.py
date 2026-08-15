from dataclasses import dataclass
from datetime import datetime


@dataclass(frozen=True, slots=True)
class Snapshot:
    """Represents a JPEG snapshot stored on the recorder."""

    channel: int
    start: datetime
    end: datetime
    file_path: str
    length: int
    disk: int
    cluster: int
    partition: int
    video_stream: str | None
