"""
Example of connecting to a Dahua device.
"""
import os

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


if __name__ == "__main__":
    main()
