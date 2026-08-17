"""Probe recorded RTSP session scaling and prepared-session pressure."""

from __future__ import annotations

import json
import os
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from dahua_rpc import DahuaClient
from dahua_rpc.models import Camera, Recording

SEARCH_DAYS = 4
ACTIVE_TARGET = 8
ACTIVE_SECONDS = 15.0
PRESSURE_SECONDS = 15.0
HIGH_PRESSURE_SECONDS = 5.0
PAUSED_HOLD_SECONDS = 60.0
RECEIVE_SLICE = 1.0
FIRST_MEDIA_TIMEOUT = 2.0
RTP_VIDEO_CLOCK = 90_000.0


@dataclass(frozen=True)
class Selection:
    camera: Camera
    recording: Recording


@dataclass(frozen=True)
class CommonWindow:
    selections: tuple[Selection, ...]
    start: datetime
    end: datetime

    @property
    def seconds(self) -> float:
        return (self.end - self.start).total_seconds()


def _search(
    client: DahuaClient, camera: Camera, start: datetime, end: datetime
) -> list[Recording]:
    with client.media.recordings(
        channel=camera.channel, start=start, end=end
    ) as results:
        return list(results)


def _best_common_window(
    cameras: tuple[Camera, ...], recordings: dict[int, list[Recording]]
) -> CommonWindow | None:
    events: list[tuple[datetime, int, int, Recording]] = []
    camera_by_channel = {camera.channel: camera for camera in cameras}
    for channel, items in recordings.items():
        for item in items:
            if item.video_stream == "Main":
                events.append((item.start_time, 1, channel, item))
                events.append((item.end_time, -1, channel, item))
    events.sort(key=lambda item: (item[0], item[1]))
    active: dict[int, Recording] = {}
    best: CommonWindow | None = None
    for index, (when, kind, channel, recording) in enumerate(events):
        if kind < 0:
            if active.get(channel) == recording:
                active.pop(channel)
        else:
            active[channel] = recording
        if index + 1 >= len(events):
            continue
        next_when = events[index + 1][0]
        if next_when <= when or not active:
            continue
        selections = tuple(
            Selection(camera_by_channel[item_channel], item)
            for item_channel, item in sorted(active.items())
        )
        candidate = CommonWindow(selections, when, next_when)
        if best is None or (
            len(candidate.selections), candidate.seconds
        ) > (len(best.selections), best.seconds):
            best = candidate
    return best


def _next_recording(
    current: Recording, recordings: list[Recording]
) -> Recording | None:
    candidates = sorted(
        (
            item
            for item in recordings
            if item.video_stream == "Main"
            and item.file_path != current.file_path
            and item.start_time >= current.start_time
        ),
        key=lambda item: (item.start_time, item.end_time),
    )
    return next(
        (item for item in candidates if item.start_time >= current.end_time),
        candidates[0] if candidates else None,
    )


def _first_media(playback) -> dict[str, Any]:
    started = time.monotonic()
    packets = 0
    byte_count = 0
    first_timestamp = None
    while time.monotonic() - started < FIRST_MEDIA_TIMEOUT:
        receipt = playback.receive(0.1)
        packets += receipt.packets
        byte_count += receipt.bytes
        if first_timestamp is None and receipt.first_timestamp is not None:
            first_timestamp = receipt.first_timestamp
        if packets:
            break
    return {
        "latency_seconds": time.monotonic() - started,
        "packets": packets,
        "bytes": byte_count,
        "first_rtp_timestamp": first_timestamp,
    }


def _receive_for(playback, duration: float) -> dict[str, Any]:
    started = time.monotonic()
    deadline = started + duration
    packets = 0
    byte_count = 0
    first_timestamp = None
    last_timestamp = None
    while time.monotonic() < deadline:
        receipt = playback.receive(
            min(RECEIVE_SLICE, max(0.01, deadline - time.monotonic()))
        )
        packets += receipt.packets
        byte_count += receipt.bytes
        if first_timestamp is None and receipt.first_timestamp is not None:
            first_timestamp = receipt.first_timestamp
        if receipt.last_timestamp is not None:
            last_timestamp = receipt.last_timestamp
    wall_elapsed = time.monotonic() - started
    rtp_elapsed = None
    if first_timestamp is not None and last_timestamp is not None:
        rtp_elapsed = (
            (last_timestamp - first_timestamp) & 0xFFFFFFFF
        ) / RTP_VIDEO_CLOCK
    return {
        "packets": packets,
        "bytes": byte_count,
        "wall_elapsed_seconds": wall_elapsed,
        "rtp_elapsed_seconds_assuming_90khz": rtp_elapsed,
        "payload_mbps": byte_count * 8 / wall_elapsed / 1_000_000,
    }


def _start_selection(client: DahuaClient, selection: Selection, master: datetime):
    playback = client.media.playback(selection.recording)
    offset = (master - selection.recording.start_time).total_seconds()
    started = time.monotonic()
    playback.start()
    if offset:
        playback.seek(offset)
    startup = time.monotonic() - started
    first = _first_media(playback)
    return playback, {
        "camera": selection.camera.name,
        "channel": selection.camera.channel,
        "recording_start": selection.recording.start_time.isoformat(),
        "recording_end": selection.recording.end_time.isoformat(),
        "npt_target": offset,
        "returned_range": playback.returned_range,
        "startup_seconds": startup,
        "first_media": first,
        "codec": playback.video_codec,
    }


def _run_concurrently(function, values):
    results = [None] * len(values)
    errors: list[dict[str, str]] = []
    with ThreadPoolExecutor(max_workers=len(values)) as executor:
        futures = {
            executor.submit(function, value): index
            for index, value in enumerate(values)
        }
        for future in as_completed(futures):
            index = futures[future]
            try:
                results[index] = future.result()
            except Exception as exc:
                errors.append(
                    {"index": str(index), "error": f"{type(exc).__name__}: {exc}"}
                )
    return results, errors


def _close_all(playbacks) -> list[str]:
    errors = []
    for playback in playbacks:
        if playback is None:
            continue
        try:
            playback.close()
        except Exception as exc:
            errors.append(f"{type(exc).__name__}: {exc}")
    return errors


def _fresh_check(
    client: DahuaClient, selection: Selection, master: datetime
) -> dict[str, Any]:
    playback = None
    try:
        playback, details = _start_selection(client, selection, master)
        details["success"] = details["first_media"]["packets"] > 0
        return details
    finally:
        if playback is not None:
            playback.close()


def _active_stage(
    client: DahuaClient,
    selections: tuple[Selection, ...],
    master: datetime,
    duration: float,
) -> dict[str, Any]:
    playbacks = []
    report: dict[str, Any] = {}
    try:
        starts, errors = _run_concurrently(
            lambda item: _start_selection(client, item, master), selections
        )
        report["start_errors"] = errors
        for result in starts:
            if result is not None:
                playbacks.append(result[0])
        report["sessions"] = [result[1] for result in starts if result is not None]
        if errors:
            report["existing_after_failure"], _ = _run_concurrently(
                lambda item: _receive_for(item, 1.0), playbacks
            )
            return report
        media, media_errors = _run_concurrently(
            lambda item: _receive_for(item, duration), playbacks
        )
        report["media"] = media
        report["media_errors"] = media_errors
        report["aggregate_payload_mbps"] = sum(
            item["payload_mbps"] for item in media if item is not None
        )
        playbacks[0].close()
        playbacks[0] = None
        remaining, remaining_errors = _run_concurrently(
            lambda item: _receive_for(item, 1.0), playbacks[1:]
        )
        report["after_close_one"] = remaining
        report["after_close_one_errors"] = remaining_errors
        return report
    finally:
        report["cleanup_errors"] = _close_all(playbacks)


def _prepare_one(client: DahuaClient, selection: Selection):
    playback = client.media.playback(selection.recording)
    started = time.monotonic()
    playback.start()
    offset = min(2.0, playback.duration or 2.0)
    if offset:
        playback.seek(offset)
    playback.pause()
    return playback, {
        "camera": selection.camera.name,
        "channel": selection.camera.channel,
        "npt_target": offset,
        "returned_range": playback.returned_range,
        "prepare_seconds": time.monotonic() - started,
        "codec": playback.video_codec,
    }


def _pressure_stage(
    client: DahuaClient,
    active_selections: tuple[Selection, ...],
    prepared_selections: tuple[Selection, ...],
    master: datetime,
    duration: float,
    *,
    resume_prepared: bool,
) -> dict[str, Any]:
    active = []
    prepared = []
    report: dict[str, Any] = {}
    try:
        active_results, active_errors = _run_concurrently(
            lambda item: _start_selection(client, item, master), active_selections
        )
        report["active_start_errors"] = active_errors
        active = [item[0] for item in active_results if item is not None]
        report["active_sessions"] = [
            item[1] for item in active_results if item is not None
        ]
        if active_errors:
            return report
        prepared_results, prepared_errors = _run_concurrently(
            lambda item: _prepare_one(client, item), prepared_selections
        )
        report["prepared_errors"] = prepared_errors
        prepared = [item[0] for item in prepared_results if item is not None]
        report["prepared_sessions"] = [
            item[1] for item in prepared_results if item is not None
        ]
        report["simultaneous_sessions"] = len(active) + len(prepared)
        active_media, active_media_errors = _run_concurrently(
            lambda item: _receive_for(item, duration), active
        )
        report["active_media"] = active_media
        report["active_media_errors"] = active_media_errors
        report["active_aggregate_payload_mbps"] = sum(
            item["payload_mbps"] for item in active_media if item is not None
        )
        if prepared_errors:
            return report
        if resume_prepared:
            def resume(item):
                started = time.monotonic()
                item.resume()
                command_seconds = time.monotonic() - started
                first = _first_media(item)
                return {
                    "resume_command_seconds": command_seconds,
                    "first_media": first,
                    "ready_seconds": command_seconds + first["latency_seconds"],
                }

            resumed, resume_errors = _run_concurrently(resume, prepared)
            report["resume"] = resumed
            report["resume_errors"] = resume_errors
            ready = [item["ready_seconds"] for item in resumed if item is not None]
            report["readiness_spread_seconds"] = (
                max(ready) - min(ready) if ready else None
            )
            report["active_close_errors"] = _close_all(active)
            active = []
            post_close, post_close_errors = _run_concurrently(
                lambda item: _receive_for(item, 1.0), prepared
            )
            report["prepared_after_active_close"] = post_close
            report["prepared_after_active_close_errors"] = post_close_errors
        return report
    finally:
        report["cleanup_errors"] = _close_all((*active, *prepared))


def _hold_prepared(client: DahuaClient, selection: Selection) -> dict[str, Any]:
    playback = None
    try:
        playback, details = _prepare_one(client, selection)
        hold_started = time.monotonic()
        while time.monotonic() - hold_started < PAUSED_HOLD_SECONDS:
            remaining = PAUSED_HOLD_SECONDS - (time.monotonic() - hold_started)
            time.sleep(min(5.0, remaining))
        resume_started = time.monotonic()
        playback.resume()
        command_seconds = time.monotonic() - resume_started
        first = _first_media(playback)
        return {
            "prepared": details,
            "hold_seconds": time.monotonic() - hold_started,
            "resume_command_seconds": command_seconds,
            "first_media": first,
            "valid_after_hold": first["packets"] > 0,
        }
    finally:
        if playback is not None:
            playback.close()


def main() -> None:
    host = os.environ.get("DAHUA_HOST")
    username = os.environ.get("DAHUA_USERNAME")
    password = os.environ.get("DAHUA_PASSWORD")
    if not host or not username or not password:
        raise SystemExit("Set DAHUA_HOST, DAHUA_USERNAME, and DAHUA_PASSWORD.")
    stage = os.environ.get("DAHUA_SCALING_STAGE", "all")
    allowed_stages = {"all", "fresh", "baseline", "pressure", "high", "hold"}
    if stage not in allowed_stages:
        raise SystemExit(f"DAHUA_SCALING_STAGE must be one of {sorted(allowed_stages)}")

    output: dict[str, Any] = {}
    print(f"stage={stage} discovery starting", flush=True)
    with DahuaClient(
        host=host, username=username, password=password, timeout=5.0
    ) as client:
        cameras = tuple(camera for camera in client.cameras.list() if camera.configured)
        end = client.current_time
        start = end - timedelta(days=SEARCH_DAYS)
        recordings = {
            camera.channel: _search(client, camera, start, end) for camera in cameras
        }
        window = _best_common_window(cameras, recordings)
        if window is None:
            raise RuntimeError("No common Main recording window found.")
        output["candidates"] = [
            {"name": item.camera.name, "channel": item.camera.channel}
            for item in window.selections
        ]
        output["common_window"] = {
            "start": window.start.isoformat(),
            "end": window.end.isoformat(),
            "seconds": window.seconds,
            "camera_count": len(window.selections),
        }
        if len(window.selections) < ACTIVE_TARGET:
            output["limitation"] = "Fewer than eight cameras overlap."
            print(json.dumps(output, indent=2, default=str))
            return
        selected = window.selections[:ACTIVE_TARGET]
        master = window.start
        pairs = []
        for selection in selected:
            next_item = _next_recording(
                selection.recording, recordings[selection.camera.channel]
            )
            if next_item is not None:
                pairs.append(
                    (selection, Selection(selection.camera, next_item))
                )
        adjacent = [pair[1] for pair in pairs]
        output["adjacent_preparable_count"] = len(pairs)
        if stage == "fresh":
            output["fresh"] = _fresh_check(client, selected[0], master)
        if stage in {"all", "baseline"}:
            print("stage=baseline starting", flush=True)
            output["baseline_8_active"] = _active_stage(
                client, selected, master, ACTIVE_SECONDS
            )
            output["fresh_after_baseline"] = _fresh_check(
                client, selected[0], master
            )
        baseline = output.get("baseline_8_active", {})
        baseline_stable = (
            stage != "all"
            or (
                not baseline.get("start_errors")
                and not baseline.get("media_errors")
                and all(item["packets"] > 0 for item in baseline.get("media", []))
            )
        )

        if stage in {"all", "pressure"} and baseline_stable and len(pairs) >= 4:
            print("stage=pressure starting", flush=True)
            output["four_active_four_prepared"] = _pressure_stage(
                client,
                tuple(pair[0] for pair in pairs[:4]),
                tuple(pair[1] for pair in pairs[:4]),
                master,
                PRESSURE_SECONDS,
                resume_prepared=True,
            )
            output["fresh_after_4_plus_4"] = _fresh_check(
                client, selected[0], master
            )
        elif stage in {"all", "pressure"}:
            output["four_active_four_prepared"] = {
                "not_run": "baseline unstable or fewer than four adjacent files"
            }

        pressure = output.get("four_active_four_prepared", {})
        pressure_stable = (
            stage != "all"
            or (
                "not_run" not in pressure
                and not pressure.get("prepared_errors")
                and not pressure.get("active_media_errors")
                and not pressure.get("resume_errors")
            )
        )
        if (
            stage in {"all", "high"}
            and baseline_stable
            and pressure_stable
            and adjacent
        ):
            print("stage=high starting", flush=True)
            high_prepared = tuple(adjacent[: len(selected)])
            output["eight_active_plus_prepared"] = _pressure_stage(
                client, selected, high_prepared, master,
                HIGH_PRESSURE_SECONDS, resume_prepared=False,
            )
            output["fresh_after_high_pressure"] = _fresh_check(
                client, selected[0], master
            )
        elif stage in {"all", "high"}:
            output["eight_active_plus_prepared"] = {
                "not_run": "earlier stress stage was not stable"
            }

        if stage in {"all", "hold"}:
            print("stage=hold starting", flush=True)
            hold_selection = adjacent[0] if adjacent else selected[0]
            output["prepared_hold_60"] = _hold_prepared(client, hold_selection)
            output["fresh_after_hold"] = _fresh_check(client, selected[0], master)

    print(json.dumps(output, indent=2, default=str))


if __name__ == "__main__":
    main()
