from dataclasses import dataclass
from datetime import datetime


@dataclass(frozen=True, slots=True)
class Recording:
    """
    Represents a single recording stored on the recorder.
    """

    channel: int
    cluster: int
    disk: int
    partition: int
    start_time: datetime
    end_time: datetime
    file_path: str
    type: str
    video_stream: str
    events: tuple[str, ...]
    flags: tuple[str, ...]
    length: int
    cut_length: int
