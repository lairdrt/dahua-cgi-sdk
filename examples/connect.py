from dahua_cgi import DahuaClient


def main() -> None:

    with DahuaClient(
        host="192.168.1.34",
        username="admin",
        password="Rz!10226$",
    ) as client:

        """
        # Show raw response
        response = client._request(
            "GET",
            "/cgi-bin/magicBox.cgi",
            params={"action": "getSystemInfo"},
        )
        print(response.text)

        Output:
        deviceType=31
        processor=ST7108
        serialNumber=ND012010178076
        updateSerial=DHI-NVR5216-16P-4KS2E

        Host:              192.168.1.34
        Manufacturer:      None
        Model:             DHI-NVR5216-16P-4KS2E
        Serial Number:     ND012010178076
        Hardware Revision: None
        Firmware Version:  None
        API Version:       None
        Processor:         ST7108
        """

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
