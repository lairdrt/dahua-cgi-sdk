"""Small live smoke test for the public encoded-packet playback API."""

from __future__ import annotations

import json
import os
from datetime import timedelta

from dahua_rpc import DahuaClient


def _summary(packets) -> dict:
    categories = sorted({(item.media_type, item.packet_type) for item in packets})
    sender_reports = [
        info
        for packet in packets
        for info in packet.rtcp_packets
        if info.packet_type == 200
    ]
    return {
        "packet_count": len(packets),
        "categories": [list(value) for value in categories],
        "sender_report_count": len(sender_reports),
        "sender_reports_complete": all(
            item.ssrc is not None
            and item.ntp_seconds is not None
            and item.ntp_fraction is not None
            and item.rtp_timestamp is not None
            for item in sender_reports
        ),
    }


def main() -> None:
    credentials = {name: os.environ.get(name) for name in (
        "DAHUA_HOST", "DAHUA_USERNAME", "DAHUA_PASSWORD"
    )}
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
            channel=camera.channel, start=end - timedelta(days=1), end=end
        ) as results:
            recording = next(
                item for item in results if item.video_stream == "Main"
            )
        with client.media.playback(recording) as playback:
            playback.start()
            video_only = _summary(playback.receive_packets(1.0))
        with client.media.playback(recording, audio=True) as playback:
            playback.start()
            dual = _summary(playback.receive_packets(1.5))
            duration = playback.duration or 0.0
            playback.seek(min(max(duration * 0.5, 1.0), duration))
            after_seek = _summary(playback.receive_packets(1.0))
    print(json.dumps({
        "camera": {"name": camera.name, "channel": camera.channel},
        "video_only": video_only,
        "video_audio": dual,
        "after_seek": after_seek,
    }, indent=2))


if __name__ == "__main__":
    main()
