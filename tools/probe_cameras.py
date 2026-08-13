"""
Probe a Dahua CGI endpoint for recording downloads.

This is an experimental utility used to reverse-engineer the
download protocol. It is intentionally verbose.
"""

import os

from dahua_cgi import DahuaClient


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
        print("REQUEST")
        print("=" * 80)

# Works:
#       request_text = f"/cgi-bin/configManager.cgi"
#        print(request_text)
#
#        response = client._connection.get(
#            request_text,
#            params={
#                "action": "getConfig",
#                "name": "ChannelTitle",
#            },
#        )
# 

# Works (sensitive data):
#       request_text = f"/cgi-bin/configManager.cgi"
#        response = client._connection.get(
#            request_text,
#            params={
#                "action": "getConfig",
#                "name": "RemoteDevice",
#            },
#        )

# Does NOT work:
#        request_text = f"/cgi-bin/remoteDeviceManager.cgi"
#        response = client._connection.get(
#            request_text,
#            params={
#                "action": "getDeviceList",
#            },
#        )

# Works:
#        request_text = f"/cgi-bin/configManager.cgi"
#        response = client._connection.get(
#            request_text,
#            params={
#                "action": "getConfig",
#                "name": "Encode",
#            },
#        )

# Works:
#        request_text = f"/cgi-bin/snapshot.cgi"
#        response = client._connection.get(
#            request_text,
#            params={
#                "channel": "1",
#            },
#        )
 
# GET /cgi-bin/configManager.cgi?action=getConfig&name=RemoteDeviceStatus : FAILS 400
# GET /cgi-bin/configManager.cgi?action=getConfig&name=RemoteDeviceInfo : FAILS 400
# GET /cgi-bin/configManager.cgi?action=getConfig&name=CameraInfo : FAILS 400

        request_text = "/cgi-bin/LogicDeviceManager.cgi"
        print(request_text)

        response = client._connection.request(
            "GET",
            "/cgi-bin/LogicDeviceManager.cgi",
            params={
                "action": "getCameraAll",
            },
        )

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
            print(response.text)

        else:

            print("\nFirst 64 bytes:")
            print(response.content[:64].hex(" "))

        print("=" * 80)

        print(response.request.method)
        print(response.request.url)
        print(response.request.headers)


if __name__ == "__main__":
    main()
