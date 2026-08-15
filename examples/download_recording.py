"""
Example of connecting to a Dahua device and downloading recordings.
"""
import os
from datetime import datetime
from pathlib import Path

from dahua_rpc import DahuaClient

host = os.environ["DAHUA_HOST"]
username = os.environ["DAHUA_USERNAME"]
password = os.environ["DAHUA_PASSWORD"]

def main() -> None:

    with DahuaClient(
        host=host,
        username=username,
        password=password,
    ) as client:

        print(f"Host:              {client.host}")
        print(f"Manufacturer:      {client.manufacturer}")
        print(f"Model:             {client.model}")
        print(f"Serial Number:     {client.serial_number}")
        print(f"Hardware Revision: {client.hardware_revision}")
        print(f"Firmware Version:  {client.firmware_version}")
        print(f"API Version:       {client.api_version}")
        print(f"Processor:         {client.processor}")

        download_this = None
        print("\nSearching for recordings...\n")
        for recording in client.media.recordings(
            channel=1,
            start=datetime(2026, 8, 6, 0, 0, 0),
            end=datetime(2026, 8, 7, 23, 59, 59),
        ):
            print(
                f"Recording: "
                f"{recording.start_time} - "
                f"{recording.end_time}  "
                f"{recording.video_stream:<10} "
                f"{recording.length} "
                f"{recording.file_path}"
            )
            download_this = recording

        if download_this is not None:
            start_stamp = download_this.start_time.strftime("%Y%m%d_%H%M%S")
            end_stamp = download_this.end_time.strftime("%Y%m%d_%H%M%S")
            download_path = Path.cwd() / f"{start_stamp}_{end_stamp}.dav"

            print(
                f"Downloading recording: "
                f"{download_this.start_time} - "
                f"{download_this.end_time}  "
                f"{download_this.video_stream:<10} "
                f"{download_this.length} "
                f"{download_this.file_path}"
            )
            download_path.write_bytes(client.media.recording_bytes(download_this))
            print(f"Downloaded to: {download_path}")


if __name__ == "__main__":
    main()
