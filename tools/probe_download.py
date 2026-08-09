"""
Probe a Dahua CGI endpoint for recording downloads.

This is an experimental utility used to reverse-engineer the
download protocol. It is intentionally verbose.
"""

import os
from pathlib import Path

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

        path = (
            "mnt/dvr/2026-08-06/000/dav/07/0/0/"
            "167008/07.14.13-07.15.45[M][0@0][0].dav"
        )

        response = client._connection.get(
            f"/cgi-bin/RPC_Loadfile/{path}"
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
            print(response.text[:2000])

        else:

            filename = Path("probe_response.bin")

            filename.write_bytes(response.content)

            print(f"Binary response written to {filename}")
            print(f"Content-Length: {len(response.content):,} bytes")

            print("\nFirst 64 bytes:")
            print(response.content[:64].hex(" "))

        print("=" * 80)

        print(response.request.method)
        print(response.request.url)
        print(response.request.headers)


if __name__ == "__main__":
    main()
