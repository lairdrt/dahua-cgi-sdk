import os
from datetime import datetime

from dahua_cgi import DahuaClient

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

        print("\nSearching for recordings...\n")
        for recording in client.media.search(
            channel=1,
            start=datetime(2026, 8, 6, 0, 0, 0),
            end=datetime(2026, 8, 7, 23, 59, 59),
        ):
            print(f"Recording: {recording.start_time} - {recording.end_time}  {recording.video_stream:<10} {recording.length} {recording.file_path}")


if __name__ == "__main__":
    main()
