"""Measure decoder startup material received by late H.265 RTSP clients."""

from __future__ import annotations

import json
import os
import socket
import subprocess
import time
from datetime import timedelta
from typing import Any

from dahua_rpc import DahuaClient
from dahua_rpc._recorded_bridge import RecordedOutput

DELAYS = (0.0, 5.0, 20.0)
CAPTURE_SECONDS = 5.0
IRAP_TYPES = set(range(16, 22))


def _recv_exact(connection: socket.socket, length: int) -> bytes:
    data = bytearray()
    while len(data) < length:
        chunk = connection.recv(length - len(data))
        if not chunk:
            raise RuntimeError("downstream RTSP client disconnected")
        data.extend(chunk)
    return bytes(data)


def _response(connection: socket.socket) -> tuple[int, dict[str, str], bytes]:
    data = bytearray()
    while b"\r\n\r\n" not in data:
        data.extend(connection.recv(4096))
    header, body = bytes(data).split(b"\r\n\r\n", 1)
    lines = header.decode("ascii").split("\r\n")
    status = int(lines[0].split()[1])
    headers = {}
    for line in lines[1:]:
        name, _, value = line.partition(":")
        headers[name.casefold()] = value.strip()
    length = int(headers.get("content-length", "0"))
    if len(body) < length:
        body += _recv_exact(connection, length - len(body))
    return status, headers, body[:length]


def _request(
    connection: socket.socket,
    method: str,
    target: str,
    cseq: int,
    headers: tuple[tuple[str, str], ...] = (),
) -> tuple[int, dict[str, str], bytes]:
    lines = [f"{method} {target} RTSP/1.0", f"CSeq: {cseq}"]
    lines.extend(f"{name}: {value}" for name, value in headers)
    connection.sendall(("\r\n".join(lines) + "\r\n\r\n").encode("ascii"))
    return _response(connection)


def _h265_units(payload: bytes) -> list[dict[str, Any]]:
    if len(payload) < 2:
        return [{"kind": "malformed", "reason": "short NAL header"}]
    nal_type = (payload[0] >> 1) & 0x3F
    if nal_type == 48:
        units = []
        offset = 2
        while offset < len(payload):
            if offset + 2 > len(payload):
                return units + [{"kind": "malformed", "reason": "short AP size"}]
            length = int.from_bytes(payload[offset : offset + 2], "big")
            offset += 2
            if length < 2 or offset + length > len(payload):
                return units + [{"kind": "malformed", "reason": "invalid AP unit"}]
            units.append({"kind": "ap", "nal_type": (payload[offset] >> 1) & 0x3F})
            offset += length
        return units
    if nal_type == 49:
        if len(payload) < 3:
            return [{"kind": "malformed", "reason": "short FU header"}]
        return [{
            "kind": "fu",
            "nal_type": payload[2] & 0x3F,
            "start": bool(payload[2] & 0x80),
            "end": bool(payload[2] & 0x40),
        }]
    return [{"kind": "single", "nal_type": nal_type}]


def _capture(playback: Any, delay: float) -> dict[str, Any]:
    output = RecordedOutput(playback)
    output.start()
    started = time.monotonic()
    try:
        remaining = delay - (time.monotonic() - started)
        if remaining > 0:
            time.sleep(remaining)
        with socket.create_connection(
            (output.host, output.port), timeout=2.0
        ) as client:
            client.settimeout(1.0)
            status, _, sdp = _request(client, "DESCRIBE", output.url, 1)
            if status != 200:
                raise RuntimeError(f"DESCRIBE failed: {status}")
            status, _, _ = _request(
                client,
                "SETUP",
                f"{output.url}/trackID=video",
                2,
                (("Transport", "RTP/AVP/TCP;unicast;interleaved=0-1"),),
            )
            if status != 200:
                raise RuntimeError(f"SETUP failed: {status}")
            play_sent = time.monotonic()
            status, _, _ = _request(
                client,
                "PLAY",
                output.url,
                3,
                (("Session", output.session),),
            )
            if status != 200:
                raise RuntimeError(f"PLAY failed: {status}")
            packets = []
            packet_count = 0
            first_by_type: dict[int, float] = {}
            fu_starts: set[tuple[bytes, int]] = set()
            first_complete_irap = None
            inter_before_irap = 0
            malformed = []
            deadline = play_sent + CAPTURE_SECONDS
            while time.monotonic() < deadline:
                try:
                    header = _recv_exact(client, 4)
                    if header[0] != 0x24:
                        raise RuntimeError("non-interleaved downstream data")
                    channel = header[1]
                    data = _recv_exact(client, int.from_bytes(header[2:4], "big"))
                except socket.timeout:
                    continue
                except RuntimeError as exc:
                    malformed.append(str(exc))
                    break
                received = time.monotonic()
                if channel != 0:
                    continue
                if len(data) < 12 or data[0] >> 6 != 2:
                    malformed.append("invalid RTP header")
                    continue
                csrc_count = data[0] & 0x0F
                header_length = 12 + 4 * csrc_count
                if len(data) < header_length:
                    malformed.append("truncated RTP CSRC list")
                    continue
                if data[0] & 0x10:
                    if len(data) < header_length + 4:
                        malformed.append("truncated RTP extension header")
                        continue
                    extension_words = int.from_bytes(
                        data[header_length + 2 : header_length + 4], "big"
                    )
                    header_length += 4 + extension_words * 4
                    if len(data) < header_length:
                        malformed.append("truncated RTP extension")
                        continue
                units = _h265_units(data[header_length:])
                elapsed = received - play_sent
                packet_count += 1
                for unit in units:
                    if "nal_type" in unit:
                        first_by_type.setdefault(unit["nal_type"], elapsed)
                    if unit["kind"] == "fu":
                        key = (data[4:8], unit["nal_type"])
                        if unit["start"]:
                            fu_starts.add(key)
                        if (
                            unit["end"]
                            and key in fu_starts
                            and unit["nal_type"] in IRAP_TYPES
                            and first_complete_irap is None
                        ):
                            first_complete_irap = elapsed
                    elif (
                        unit.get("nal_type") in IRAP_TYPES
                        and first_complete_irap is None
                    ):
                        first_complete_irap = elapsed
                    if unit["kind"] == "malformed":
                        malformed.append(unit["reason"])
                if first_complete_irap is None and any(
                    unit.get("nal_type", 64) < 16 for unit in units
                ):
                    inter_before_irap += 1
                if len(packets) < 12:
                    packets.append({
                        "after_play_seconds": elapsed,
                        "payload_type": data[1] & 0x7F,
                        "marker": bool(data[1] & 0x80),
                        "sequence": int.from_bytes(data[2:4], "big"),
                        "timestamp": int.from_bytes(data[4:8], "big"),
                        "ssrc": int.from_bytes(data[8:12], "big"),
                        "bytes": len(data),
                        "units": units,
                    })
        def first(types: set[int]) -> float | None:
            values = [value for key, value in first_by_type.items() if key in types]
            return min(values) if values else None

        return {
            "requested_delay_seconds": delay,
            "actual_connect_delay_seconds": play_sent - started,
            "sdp": sdp.decode("ascii"),
            "rtp_packet_count": packet_count,
            "first_rtp_seconds": packets[0]["after_play_seconds"] if packets else None,
            "first_vps_seconds": first({32}),
            "first_sps_seconds": first({33}),
            "first_pps_seconds": first({34}),
            "first_irap_seconds": first(IRAP_TYPES),
            "first_complete_irap_seconds": first_complete_irap,
            "inter_packets_before_complete_irap": inter_before_irap,
            "nal_types": sorted(first_by_type),
            "malformed": malformed,
            "first_packets": packets,
            "bridge_error": str(output.error) if output.error else None,
            "startup_error": (
                str(output.startup_error) if output.startup_error else None
            ),
            "startup_packet_high_water": output.startup_packet_high_water,
            "startup_audio_high_water": output.startup_audio_high_water,
            "queue_drops": output.queue_drops,
            "queue_high_water": output.queue_high_water,
        }
    finally:
        output.close()


def _ffprobe(playback: Any, executable: str, delay: float) -> dict[str, Any]:
    output = RecordedOutput(playback)
    output.start()
    try:
        time.sleep(delay)
        command = [
            executable,
            "-v",
            "warning",
            "-rtsp_transport",
            "tcp",
            "-rw_timeout",
            "7000000",
            "-select_streams",
            "v:0",
            "-read_intervals",
            "%+5",
            "-show_packets",
            "-show_entries",
            "packet=pts_time,flags,size",
            "-of",
            "json",
            output.url,
        ]
        try:
            completed = subprocess.run(
                command,
                capture_output=True,
                check=False,
                text=True,
                timeout=40,
            )
            parsed = json.loads(completed.stdout) if completed.stdout else {}
            packets = parsed.get("packets", [])
            return {
                "delay_seconds": delay,
                "returncode": completed.returncode,
                "packet_count": len(packets),
                "first_packet_flags": (
                    packets[0].get("flags") if packets else None
                ),
                "stderr": completed.stderr,
                "timed_out": False,
            }
        except subprocess.TimeoutExpired as exc:
            return {
                "delay_seconds": delay,
                "returncode": None,
                "stdout": exc.stdout,
                "stderr": exc.stderr,
                "timed_out": True,
            }
    finally:
        output.close()


def main() -> None:
    credentials = {
        name: os.environ.get(name)
        for name in ("DAHUA_HOST", "DAHUA_USERNAME", "DAHUA_PASSWORD")
    }
    if not all(credentials.values()):
        raise SystemExit("Set DAHUA_HOST, DAHUA_USERNAME, and DAHUA_PASSWORD.")
    with DahuaClient(
        host=credentials["DAHUA_HOST"],
        username=credentials["DAHUA_USERNAME"],
        password=credentials["DAHUA_PASSWORD"],
    ) as client:
        camera = next(
            item
            for item in client.cameras.list()
            if item.configured and item.name == "Drive Down"
        )
        end = client.current_time
        with client.media.recordings(
            channel=camera.channel,
            start=end - timedelta(days=1),
            end=end,
        ) as recordings:
            candidates = [
                item
                for item in recordings
                if item.video_stream == "Main"
                and (item.end_time - item.start_time).total_seconds() >= 35
            ]
        if not candidates:
            raise RuntimeError("No Main recording of at least 35 seconds was found.")
        recording = max(candidates, key=lambda item: item.start_time)
        cases = []
        if not os.environ.get("LATE_CLIENT_FFPROBE_ONLY"):
            for delay in DELAYS:
                print(f"late-client case: {delay:g}s", flush=True)
                cases.append(_capture(client.media.playback(recording), delay))
        ffprobe_path = os.environ.get("FFPROBE_PATH")
        ffprobe = []
        if ffprobe_path:
            for delay in (5.0, 20.0):
                print(f"late-client ffprobe case: {delay:g}s", flush=True)
                ffprobe.append(_ffprobe(
                    client.media.playback(recording), ffprobe_path, delay
                ))
        print(json.dumps({
            "camera": {"name": camera.name, "channel": camera.channel},
            "recording": {
                "profile": recording.video_stream,
                "start": recording.start_time.isoformat(),
                "end": recording.end_time.isoformat(),
                "duration_seconds": (
                    recording.end_time - recording.start_time
                ).total_seconds(),
            },
            "capture_seconds": CAPTURE_SECONDS,
            "cases": cases,
            "ffprobe": ffprobe,
        }, indent=2))


if __name__ == "__main__":
    main()
