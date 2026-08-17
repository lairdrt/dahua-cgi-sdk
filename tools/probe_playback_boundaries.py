"""Probe recorded RTSP handoffs between adjacent indexed DAV files."""

from __future__ import annotations

import hashlib
import json
import os
import statistics
import time
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from dahua_rpc import DahuaClient
from dahua_rpc.models import Camera, Recording

PREFERRED_CAMERAS = ("Drive Down", "Drive Up")
SEARCH_DAYS = 4
REPETITIONS = 3
MEDIA_WINDOW = 0.1
TARGET_B_OFFSET = 2.0


@dataclass(frozen=True)
class Boundary:
    camera: Camera
    outgoing: Recording
    incoming: Recording

    @property
    def gap_seconds(self) -> float:
        return (self.incoming.start_time - self.outgoing.end_time).total_seconds()


def _identity(recording: Recording) -> str:
    return hashlib.sha256(recording.file_path.encode()).hexdigest()[:12]


def _public_recording(recording: Recording) -> dict[str, Any]:
    return {
        "identity_sha256": _identity(recording),
        "channel": recording.channel,
        "start": recording.start_time.isoformat(),
        "end": recording.end_time.isoformat(),
        "events": recording.events,
        "flags": recording.flags,
        "video_stream": recording.video_stream,
        "cluster": recording.cluster,
        "disk": recording.disk,
        "partition": recording.partition,
    }


def _search(
    client: DahuaClient, camera: Camera, start: datetime, end: datetime
) -> list[Recording]:
    with client.media.recordings(
        channel=camera.channel, start=start, end=end
    ) as results:
        return list(results)


def _boundaries(camera: Camera, recordings: list[Recording]) -> list[Boundary]:
    main = sorted(
        (item for item in recordings if item.video_stream == "Main"),
        key=lambda item: (item.start_time, item.end_time),
    )
    return [
        Boundary(camera, outgoing, incoming)
        for outgoing, incoming in zip(main, main[1:], strict=False)
        if outgoing.file_path != incoming.file_path
    ]


def _choose_cases(boundaries: list[Boundary]) -> dict[str, Boundary]:
    if not boundaries:
        raise RuntimeError("No consecutive Main recordings found.")
    nonnegative = [item for item in boundaries if item.gap_seconds >= 0]
    small = min(
        nonnegative or boundaries,
        key=lambda item: (abs(item.gap_seconds), -item.outgoing.end_time.timestamp()),
    )
    noticeable = [item for item in boundaries if item.gap_seconds >= 10]
    overlap = [item for item in boundaries if item.gap_seconds < 0]
    cases = {"small_or_no_gap": small}
    if noticeable:
        cases["noticeable_gap"] = min(
            noticeable,
            key=lambda item: (
                item.gap_seconds,
                -item.outgoing.end_time.timestamp(),
            ),
        )
    if overlap:
        cases["overlap"] = max(
            overlap, key=lambda item: (item.gap_seconds, item.outgoing.end_time)
        )
    return cases


def _first_media(playback, timeout: float = 1.5) -> dict[str, Any]:
    started = time.monotonic()
    attempts = 0
    total_bytes = 0
    while time.monotonic() - started < timeout:
        receipt = playback.receive(MEDIA_WINDOW)
        attempts += 1
        total_bytes += receipt.bytes
        if receipt.packets:
            return {
                "latency_seconds": time.monotonic() - started,
                "polls": attempts,
                "packets": receipt.packets,
                "bytes": total_bytes,
                "first_rtp_timestamp": receipt.first_timestamp,
            }
    return {
        "latency_seconds": time.monotonic() - started,
        "polls": attempts,
        "packets": 0,
        "bytes": total_bytes,
        "first_rtp_timestamp": None,
    }


def _create_and_start(client: DahuaClient, recording: Recording, offset: float):
    created = time.monotonic()
    playback = client.media.playback(recording)
    creation_seconds = time.monotonic() - created
    started = time.monotonic()
    playback.start()
    if offset:
        playback.seek(offset)
    setup_seconds = time.monotonic() - started
    return playback, creation_seconds, setup_seconds


def _prime_outgoing(client: DahuaClient, boundary: Boundary):
    playback, _, setup_seconds = _create_and_start(client, boundary.outgoing, 0.0)
    duration = playback.duration
    if duration is None:
        playback.close()
        raise RuntimeError("Outgoing SDP omitted duration.")
    for lead_seconds in (1.0, 2.0, 5.0, 10.0, 20.0):
        playback.seek(max(0.0, duration - lead_seconds))
        receipt = _first_media(playback)
        if receipt["packets"]:
            return playback, duration, setup_seconds, lead_seconds, receipt
    playback.close()
    raise RuntimeError("Outgoing recording delivered no near-end media.")


def _serial_handoff(client: DahuaClient, boundary: Boundary) -> dict[str, Any]:
    outgoing, duration, outgoing_setup, lead_seconds, final_a = _prime_outgoing(
        client, boundary
    )
    boundary_decision = time.monotonic()
    close_started = time.monotonic()
    outgoing.close()
    close_seconds = time.monotonic() - close_started
    incoming, creation, setup = _create_and_start(
        client, boundary.incoming, TARGET_B_OFFSET
    )
    try:
        first_b = _first_media(incoming)
        return {
            "outgoing_duration": duration,
            "outgoing_setup_seconds": outgoing_setup,
            "verified_media_lead_seconds": lead_seconds,
            "final_a": final_a,
            "incoming_creation_seconds": creation,
            "incoming_setup_seek_seconds": setup,
            "incoming_returned_range": incoming.returned_range,
            "first_b": first_b,
            "effective_interruption_seconds": time.monotonic() - boundary_decision,
            "outgoing_close_seconds": close_seconds,
        }
    finally:
        incoming.close()


def _preopen_handoff(client: DahuaClient, boundary: Boundary) -> dict[str, Any]:
    outgoing, duration, outgoing_setup, lead_seconds, _ = _prime_outgoing(
        client, boundary
    )
    incoming = None
    try:
        incoming, creation, setup = _create_and_start(
            client, boundary.incoming, TARGET_B_OFFSET
        )
        pause_started = time.monotonic()
        incoming.pause()
        pause_seconds = time.monotonic() - pause_started
        final_a = _first_media(outgoing)
        boundary_decision = time.monotonic()
        resume_started = time.monotonic()
        incoming.resume()
        resume_seconds = time.monotonic() - resume_started
        first_b = _first_media(incoming)
        effective = time.monotonic() - boundary_decision
        close_started = time.monotonic()
        outgoing.close()
        return {
            "outgoing_duration": duration,
            "outgoing_setup_seconds": outgoing_setup,
            "verified_media_lead_seconds": lead_seconds,
            "final_a": final_a,
            "incoming_creation_seconds": creation,
            "incoming_setup_seek_seconds": setup,
            "incoming_returned_range": incoming.returned_range,
            "incoming_pause_seconds": pause_seconds,
            "incoming_resume_seconds": resume_seconds,
            "first_b": first_b,
            "effective_interruption_seconds": effective,
            "outgoing_close_after_switch_seconds": time.monotonic() - close_started,
        }
    finally:
        outgoing.close()
        if incoming is not None:
            incoming.close()


def _summarize(runs: list[dict[str, Any]]) -> dict[str, Any]:
    fields = (
        "incoming_creation_seconds",
        "incoming_setup_seek_seconds",
        "effective_interruption_seconds",
    )
    return {
        field: {
            "min": min(run[field] for run in runs),
            "median": statistics.median(run[field] for run in runs),
            "max": max(run[field] for run in runs),
        }
        for field in fields
    }


def _extra1_match(
    recording: Recording, recordings: list[Recording]
) -> Recording | None:
    extra = [item for item in recordings if item.video_stream == "Extra1"]
    return min(
        extra,
        key=lambda item: abs((item.start_time - recording.start_time).total_seconds()),
        default=None,
    )


def _other_camera_during_gap(
    boundary: Boundary,
    all_recordings: dict[str, list[Recording]],
) -> dict[str, Any] | None:
    if boundary.gap_seconds <= 0:
        return None
    midpoint = boundary.outgoing.end_time + timedelta(seconds=boundary.gap_seconds / 2)
    for name, recordings in all_recordings.items():
        if name == boundary.camera.name:
            continue
        match = next(
            (
                item
                for item in recordings
                if item.video_stream == "Main"
                and item.start_time <= midpoint < item.end_time
            ),
            None,
        )
        if match is not None:
            return {
                "camera": name,
                "channel": match.channel,
                "master_time": midpoint.isoformat(),
                "recording": _public_recording(match),
            }
    return None


def main() -> None:
    credentials = {name: os.environ.get(name) for name in (
        "DAHUA_HOST", "DAHUA_USERNAME", "DAHUA_PASSWORD"
    )}
    if not all(credentials.values()):
        raise SystemExit("Set DAHUA_HOST, DAHUA_USERNAME, and DAHUA_PASSWORD.")

    output: dict[str, Any] = {}
    with DahuaClient(
        host=credentials["DAHUA_HOST"],
        username=credentials["DAHUA_USERNAME"],
        password=credentials["DAHUA_PASSWORD"],
    ) as client:
        cameras = tuple(camera for camera in client.cameras.list() if camera.configured)
        preferred = next(
            (
                camera
                for name in PREFERRED_CAMERAS
                for camera in cameras
                if camera.name == name
            ),
            None,
        )
        if preferred is None:
            raise RuntimeError("Preferred configured camera was not found.")
        end = client.current_time
        start = end - timedelta(days=SEARCH_DAYS)
        all_recordings = {
            camera.name: _search(client, camera, start, end) for camera in cameras
        }
        cases = _choose_cases(_boundaries(preferred, all_recordings[preferred.name]))
        output["camera"] = {"name": preferred.name, "channel": preferred.channel}
        output["cases"] = {}
        for name, boundary in cases.items():
            case: dict[str, Any] = {
                "gap_seconds": boundary.gap_seconds,
                "outgoing": _public_recording(boundary.outgoing),
                "incoming": _public_recording(boundary.incoming),
            }
            extra_a = _extra1_match(boundary.outgoing, all_recordings[preferred.name])
            extra_b = _extra1_match(boundary.incoming, all_recordings[preferred.name])
            case["extra1_matches"] = [
                _public_recording(item)
                for item in (extra_a, extra_b)
                if item is not None
            ]
            case["other_camera_during_gap"] = _other_camera_during_gap(
                boundary, all_recordings
            )
            if name != "overlap":
                probe = client.media.playback(boundary.outgoing)
                try:
                    probe.start()
                    duration = probe.duration
                    try:
                        probe.seek((duration or 0.0) + 1.0)
                        beyond = "accepted"
                    except (ValueError, RuntimeError) as exc:
                        beyond = f"rejected: {type(exc).__name__}: {exc}"
                    case["cross_file_seek"] = {
                        "duration": duration,
                        "beyond_duration": beyond,
                    }
                finally:
                    probe.close()
                serial = [_serial_handoff(client, boundary) for _ in range(REPETITIONS)]
                preopen = [
                    _preopen_handoff(client, boundary)
                    for _ in range(REPETITIONS)
                ]
                case["target_b_offset_seconds"] = TARGET_B_OFFSET
                case["serial"] = {"runs": serial, "summary": _summarize(serial)}
                case["preopen"] = {"runs": preopen, "summary": _summarize(preopen)}
            output["cases"][name] = case

    print(json.dumps(output, indent=2, default=str))


if __name__ == "__main__":
    main()
