"""Live architecture probe for synchronized recorded playback sessions."""

from __future__ import annotations

import json
import os
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from dahua_rpc import DahuaClient
from dahua_rpc.models import Camera, Recording

TARGET_NAMES = ("Drive Down", "Drive Up")
SEARCH_DAYS = 4
MIN_TWO_SECONDS = 35.0
RUN_SECONDS = 30.0
RECEIVE_SLICE = 1.0
RTP_VIDEO_CLOCK = 90_000.0


@dataclass(frozen=True)
class Window:
    recordings: tuple[Recording, ...]
    start: datetime
    end: datetime

    @property
    def seconds(self) -> float:
        return (self.end - self.start).total_seconds()


def _public_recording(recording: Recording) -> dict[str, Any]:
    return {
        "channel": recording.channel,
        "start": recording.start_time.isoformat(),
        "end": recording.end_time.isoformat(),
        "type": recording.type,
        "events": recording.events,
        "flags": recording.flags,
        "video_stream": recording.video_stream,
        "length": recording.length,
        "cut_length": recording.cut_length,
    }


def _overlap(recordings: tuple[Recording, ...]) -> Window | None:
    start = max(item.start_time for item in recordings)
    end = min(item.end_time for item in recordings)
    return Window(recordings, start, end) if end > start else None


def _best_window(
    groups: tuple[list[Recording], ...], *, stream: str | None = None
) -> Window | None:
    filtered = [
        sorted(
            (
                item
                for item in group
                if stream is None or item.video_stream == stream
            ),
            key=lambda item: item.start_time,
        )
        for group in groups
    ]
    if any(not group for group in filtered):
        return None
    indexes = [0] * len(filtered)
    best: Window | None = None
    while all(
        index < len(group)
        for index, group in zip(indexes, filtered, strict=True)
    ):
        choices = tuple(
            group[index] for group, index in zip(filtered, indexes, strict=True)
        )
        candidate = _overlap(choices)
        if candidate is not None and (best is None or candidate.seconds > best.seconds):
            best = candidate
        earliest_end = min(item.end_time for item in choices)
        for index, item in enumerate(choices):
            if item.end_time == earliest_end:
                indexes[index] += 1
    return best


def _search(
    client: DahuaClient, camera: Camera, start: datetime, end: datetime
) -> list[Recording]:
    with client.media.recordings(
        channel=camera.channel, start=start, end=end
    ) as results:
        return list(results)


def _start_at(client: DahuaClient, recording: Recording, when: datetime):
    playback = client.media.playback(recording)
    started = time.monotonic()
    playback.start()
    offset = max(0.0, (when - recording.start_time).total_seconds())
    if offset:
        playback.seek(offset)
    return playback, time.monotonic() - started


def _receive_for(playback, duration: float) -> dict[str, Any]:
    deadline = time.monotonic() + duration
    packets = 0
    byte_count = 0
    first_timestamp = None
    last_timestamp = None
    first_media_latency = None
    started = time.monotonic()
    while time.monotonic() < deadline:
        receipt = playback.receive(
            min(RECEIVE_SLICE, max(0.01, deadline - time.monotonic()))
        )
        if receipt.packets and first_media_latency is None:
            first_media_latency = time.monotonic() - started
        packets += receipt.packets
        byte_count += receipt.bytes
        if first_timestamp is None and receipt.first_timestamp is not None:
            first_timestamp = receipt.first_timestamp
        if receipt.last_timestamp is not None:
            last_timestamp = receipt.last_timestamp
    rtp_elapsed = None
    if first_timestamp is not None and last_timestamp is not None:
        rtp_elapsed = (
            (last_timestamp - first_timestamp) & 0xFFFFFFFF
        ) / RTP_VIDEO_CLOCK
    return {
        "packets": packets,
        "bytes": byte_count,
        "first_media_latency_seconds": first_media_latency,
        "first_rtp_timestamp": first_timestamp,
        "last_rtp_timestamp": last_timestamp,
        "rtp_elapsed_seconds_assuming_90khz": rtp_elapsed,
        "wall_elapsed_seconds": time.monotonic() - started,
    }


def _exercise(client: DahuaClient, window: Window, duration: float) -> dict[str, Any]:
    with ThreadPoolExecutor(max_workers=len(window.recordings)) as executor:
        starts = list(
            executor.map(
                lambda item: _start_at(client, item, window.start), window.recordings
            )
        )
    playbacks = [item[0] for item in starts]
    report: dict[str, Any] = {
        "start_latency_seconds": [item[1] for item in starts],
        "returned_ranges": [item.returned_range for item in playbacks],
        "codec": [item.video_codec for item in playbacks],
        "duration": [item.duration for item in playbacks],
    }
    try:
        with ThreadPoolExecutor(max_workers=len(playbacks)) as executor:
            report["concurrent_media"] = list(
                executor.map(lambda item: _receive_for(item, duration), playbacks)
            )

        seek_offset = min(
            max(1.0, window.seconds * 0.25), max(1.0, window.seconds - 2.0)
        )
        target = window.start + timedelta(seconds=seek_offset)

        def reposition(pair):
            playback, recording = pair
            requested = max(0.0, (target - recording.start_time).total_seconds())
            started = time.monotonic()
            playback.seek(requested)
            receipt = playback.receive(RECEIVE_SLICE)
            return {
                "requested_npt": requested,
                "returned_range": playback.returned_range,
                "delivery_latency_seconds": time.monotonic() - started,
                "packets": receipt.packets,
                "bytes": receipt.bytes,
                "first_rtp_timestamp": receipt.first_timestamp,
            }

        with ThreadPoolExecutor(max_workers=len(playbacks)) as executor:
            report["reposition_target"] = target.isoformat()
            report["reposition"] = list(
                executor.map(
                    reposition, zip(playbacks, window.recordings, strict=True)
                )
            )

        playbacks[0].close()
        report["after_close_one"] = _receive_for(playbacks[1], 2.0)
    finally:
        for playback in playbacks:
            playback.close()
    return report


def main() -> None:
    host = os.environ.get("DAHUA_HOST")
    username = os.environ.get("DAHUA_USERNAME")
    password = os.environ.get("DAHUA_PASSWORD")
    if not host or not username or not password:
        raise SystemExit("Set DAHUA_HOST, DAHUA_USERNAME, and DAHUA_PASSWORD.")

    output: dict[str, Any] = {}
    with DahuaClient(host=host, username=username, password=password) as client:
        cameras = tuple(camera for camera in client.cameras.list() if camera.configured)
        by_name = {camera.name: camera for camera in cameras}
        missing = [name for name in TARGET_NAMES if name not in by_name]
        if missing:
            raise RuntimeError(f"Configured camera names not found: {missing}")
        output["cameras"] = [
            {"name": camera.name, "channel": camera.channel} for camera in cameras
        ]
        end = client.current_time
        start = end - timedelta(days=SEARCH_DAYS)
        recordings = {
            camera.name: _search(client, camera, start, end) for camera in cameras
        }
        output["search"] = {
            name: {
                "count": len(items),
                "metadata_signatures": sorted(
                    {
                        (
                            item.type,
                            item.events,
                            item.flags,
                            item.video_stream,
                        )
                        for item in items
                    },
                    key=repr,
                ),
            }
            for name, items in recordings.items()
        }

        target_groups = tuple(recordings[name] for name in TARGET_NAMES)
        two = _best_window(target_groups, stream="Main") or _best_window(target_groups)
        if two is None:
            raise RuntimeError("No overlapping Drive Down / Drive Up recordings found.")
        output["two_window"] = {
            "start": two.start.isoformat(),
            "end": two.end.isoformat(),
            "seconds": two.seconds,
            "recordings": [_public_recording(item) for item in two.recordings],
        }
        if two.seconds < MIN_TWO_SECONDS:
            output["two_result"] = {
                "not_run": f"best overlap was shorter than {MIN_TWO_SECONDS} seconds"
            }
        else:
            output["two_result"] = _exercise(
                client, two, min(RUN_SECONDS, two.seconds - 2.0)
            )

        extra = _best_window(target_groups, stream="Extra1")
        output["extra1_window"] = (
            None
            if extra is None
            else {
                "start": extra.start.isoformat(),
                "end": extra.end.isoformat(),
                "seconds": extra.seconds,
                "recordings": [_public_recording(item) for item in extra.recordings],
            }
        )
        if extra is not None and extra.seconds >= 8:
            output["extra1_result"] = _exercise(client, extra, 5.0)
        else:
            output["extra1_result"] = {
                "not_run": "no Extra1 overlap long enough for playback validation"
            }

        four_groups = sorted(
            ((name, items) for name, items in recordings.items() if items),
            key=lambda pair: pair[0],
        )
        best_four = None
        best_names = None
        for index_a in range(len(four_groups)):
            for index_b in range(index_a + 1, len(four_groups)):
                for index_c in range(index_b + 1, len(four_groups)):
                    for index_d in range(index_c + 1, len(four_groups)):
                        selected = (
                            four_groups[index_a], four_groups[index_b],
                            four_groups[index_c], four_groups[index_d],
                        )
                        candidate = _best_window(
                            tuple(pair[1] for pair in selected), stream="Main"
                        )
                        if candidate and (
                            best_four is None or candidate.seconds > best_four.seconds
                        ):
                            best_four = candidate
                            best_names = tuple(pair[0] for pair in selected)
        output["four_window"] = None
        if best_four is not None:
            output["four_window"] = {
                "names": best_names,
                "start": best_four.start.isoformat(),
                "end": best_four.end.isoformat(),
                "seconds": best_four.seconds,
                "recordings": [
                    _public_recording(item) for item in best_four.recordings
                ],
            }
            stable = all(
                item["packets"] > 0
                for item in output.get("two_result", {}).get("concurrent_media", [])
            )
            if stable and best_four.seconds >= 12:
                output["four_result"] = _exercise(
                    client, best_four, min(15.0, best_four.seconds - 2.0)
                )
            else:
                output["four_result"] = {
                    "not_run": "two-camera test not stable or overlap too short"
                }

    print(json.dumps(output, indent=2, default=str))


if __name__ == "__main__":
    main()
