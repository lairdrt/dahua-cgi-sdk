"""
Get system information from a Dahua device.

Calls are based upon documentation found in the DAHUA HTTP API FOR IPC Version 3.26
"""

import os

from dahua_cgi import DahuaClient


def print_response(response):
    print("=" * 80)
    print("RESPONSE")
    print("=" * 80)

    print(f"Status: {response.status_code}")

    print("\nHeaders:")
    for key, value in response.headers.items():
        print(f"  {key}: {value}")

    print()

    content_type = response.headers.get("Content-Type", "")

    if content_type.startswith("text"):

        print("Body:")
        print("-" * 80)
        print(response.text[:2000])

    else:

        print("\nFirst 64 bytes:")
        print(response.content[:64].hex(" "))

    print("=" * 80)

    print(response.request.method)
    print(response.request.url)
    print(response.request.headers)

    print("=" * 80)
    print()


def main() -> None:
    host = os.environ["DAHUA_HOST"]
    username = os.environ["DAHUA_USERNAME"]
    password = os.environ["DAHUA_PASSWORD"]

    with DahuaClient(
        host=host,
        username=username,
        password=password,
    ) as client:
        
        print("=" * 80)
        print("/cgi-bin/magicBox.cgi?action=getDeviceType")
        response = client._connection.request(
            "GET",
            "/cgi-bin/magicBox.cgi",
            params={
                "action": "getDeviceType",
            },
        )
        print_response(response)

        print("=" * 80)
        print("/cgi-bin/magicBox.cgi?action=getHardwareVersion")
        response = client._connection.request(
            "GET",
            "/cgi-bin/magicBox.cgi",
            params={
                "action": "getHardwareVersion",
            },
        )
        print_response(response)

        print("=" * 80)
        print("/cgi-bin/magicBox.cgi?action=getSerialNo")
        response = client._connection.request(
            "GET",
            "/cgi-bin/magicBox.cgi",
            params={
                "action": "getSerialNo",
            },
        )
        print_response(response)

        print("=" * 80)
        print("/cgi-bin/magicBox.cgi?action=getMachineName")
        response = client._connection.request(
            "GET",
            "/cgi-bin/magicBox.cgi",
            params={
                "action": "getMachineName",
            },
        )
        print_response(response)

        print("=" * 80)
        print("/cgi-bin/magicBox.cgi?action=getSystemInfo")
        response = client._connection.request(
            "GET",
            "/cgi-bin/magicBox.cgi",
            params={
                "action": "getSystemInfo",
            },
        )
        print_response(response)

        print("=" * 80)
        print("/cgi-bin/magicBox.cgi?action=getVendor")
        response = client._connection.request(
            "GET",
            "/cgi-bin/magicBox.cgi",
            params={
                "action": "getVendor",
            },
        )
        print_response(response)

        print("=" * 80)
        print("/cgi-bin/magicBox.cgi?action=getSoftwareVersion")
        response = client._connection.request(
            "GET",
            "/cgi-bin/magicBox.cgi",
            params={
                "action": "getSoftwareVersion",
            },
        )
        print_response(response)

        print("=" * 80)
        print("/cgi-bin/configManager.cgi?action=getConfig&name=General")
        response = client._connection.request(
            "GET",
            "/cgi-bin/configManager.cgi",
            params={
                "action": "getConfig",
                "name": "General"
            },
        )
        print_response(response)

if __name__ == "__main__":
    main()
