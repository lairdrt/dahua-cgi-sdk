"""Disposable RPC2 media-search and recording-retrieval feasibility probe."""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
from typing import Any

import requests
from requests.auth import HTTPDigestAuth

HOST = os.environ.get("DAHUA_HOST", "192.168.1.34")
CHANNEL = 0
START_TIME = "2026-08-14 05:27:10" # "2026-08-13 00:00:00"
END_TIME = "2026-08-14 05:28:25" # 2026-08-13 23:59:59"
RESULT_COUNT = 10
PRINT_COUNT = 5
TIMEOUT = 30.0
OUTPUT_DIR = Path(__file__).with_name("output")

METADATA_FIELDS = (
    "Channel",
    "StartTime",
    "EndTime",
    "Duration",
    "Length",
    "FilePath",
    "VideoStream",
    "Events",
    "Flags",
)
SENSITIVE_KEYS = {"password", "random", "realm", "session"}


class ProbeFailure(RuntimeError):
    """A fail-fast probe-stage failure."""


class RpcProbe:
    def __init__(self, host: str, username: str, password: str) -> None:
        self._base_url = f"http://{host}"
        self._username = username
        self._password = password
        self._http = requests.Session()
        self._request_id = 0
        self._session: str | None = None

    def _next_id(self) -> int:
        self._request_id += 1
        return self._request_id

    def _post(
        self,
        path: str,
        method: str,
        params: Any,
        *,
        object_id: int | None = None,
        include_session: bool = True,
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "method": method,
            "params": params,
            "id": self._next_id(),
        }
        if include_session and self._session is not None:
            payload["session"] = self._session
        if object_id is not None:
            payload["object"] = object_id

        print(f"\nRPC method: {method}")
        try:
            response = self._http.post(
                f"{self._base_url}{path}", json=payload, timeout=TIMEOUT
            )
        except requests.RequestException as exc:
            raise ProbeFailure(f"{method}: HTTP request failed: {exc}") from exc

        if response.status_code != 200:
            raise ProbeFailure(
                f"{method}: HTTP {response.status_code}; safe body: "
                f"{response.text[:2000]}"
            )

        try:
            data = response.json()
        except requests.exceptions.JSONDecodeError as exc:
            raise ProbeFailure(
                f"{method}: response was not JSON; safe body: {response.text[:2000]}"
            ) from exc

        # https://github.com/H3xano/dahua-rpc-api
        # Response: Camera returns error Code 268632079 but includes critical
        # tokens in params:
        #    realm — e.g. "Login to 4X02345PAJ9A82F"
        #    random — random string for this session
        #    session — temporary session ID
        expected_login_challenge = (
            method == "global.login"
            and not include_session
            and data.get("result") is False
            and (data.get("error") or {}).get("code") == 268632079
        )

        print(json.dumps(_safe(data), indent=2, sort_keys=True))
        if not expected_login_challenge and data.get("result") is False:
            raise ProbeFailure(f"{method}: RPC rejected request: {_safe_json(data)}")
        if not expected_login_challenge and data.get("error") not in (None, {}):
            raise ProbeFailure(f"{method}: RPC error: {_safe_json(data)}")
        return data

    def login(self) -> None:
        challenge = self._post(
            "/RPC2_Login",
            "global.login",
            {
                "userName": self._username,
                "password": "",
                "clientType": "Dahua3.0-Web3.0",
            },
            include_session=False,
        )
        try:
            self._session = str(challenge["session"])
            challenge_params = challenge["params"]
            realm = str(challenge_params["realm"])
            random_value = str(challenge_params["random"])
        except (KeyError, TypeError) as exc:
            raise ProbeFailure(
                f"global.login challenge was incomplete: {_safe_json(challenge)}"
            ) from exc

        encryption = challenge_params.get("encryption")
        if encryption != "Default":
            raise ProbeFailure(
                f"Unexpected RPC authentication mode: {encryption!r}; stopping."
            )

        ha1_source = f"{self._username}:{realm}:{self._password}".encode()
        ha1 = hashlib.md5(ha1_source).hexdigest().upper()
        response_source = f"{self._username}:{random_value}:{ha1}".encode()
        challenge_response = hashlib.md5(response_source).hexdigest().upper()

        self._post(
            "/RPC2_Login",
            "global.login",
            {
                "userName": self._username,
                "password": challenge_response,
                "clientType": "Dahua3.0-Web3.0",
                "authorityType": "Default",
                "passwordType": "Default",
            },
        )

    def rpc(
        self, method: str, params: Any = None, *, object_id: int | None = None
    ) -> dict[str, Any]:
        return self._post("/RPC2", method, params, object_id=object_id)

    def search_media(self, condition: dict[str, Any]) -> list[dict[str, Any]]:
        """Run one disposable media search with guaranteed finder cleanup."""

        created = self.rpc("mediaFileFind.factory.create")
        finder = created.get("result")
        if not isinstance(finder, int) or isinstance(finder, bool):
            raise ProbeFailure("mediaFileFind.factory.create returned no object.")
        try:
            self.rpc(
                "mediaFileFind.findFile",
                {"condition": condition},
                object_id=finder,
            )
            page = self.rpc(
                "mediaFileFind.findNextFile",
                {"count": 100},
                object_id=finder,
            )
            params = page.get("params")
            if not isinstance(params, dict):
                raise ProbeFailure("findNextFile omitted params.")
            found = params.get("found")
            infos = params.get("infos")
            if found == 0 and infos is None:
                return []
            if (
                not isinstance(found, int)
                or isinstance(found, bool)
                or not isinstance(infos, list)
                or len(infos) != found
                or not all(isinstance(item, dict) for item in infos)
            ):
                raise ProbeFailure("findNextFile returned a malformed result page.")
            return infos
        finally:
            for method in ("mediaFileFind.close", "mediaFileFind.destroy"):
                try:
                    self.rpc(method, object_id=finder)
                except ProbeFailure as exc:
                    print(f"Cleanup warning: {exc}")

    def retrieve(self, file_path: str) -> tuple[Path, int, str]:
        url = f"{self._base_url}/cgi-bin/RPC_Loadfile/{file_path.lstrip('/')}"
        try:
            response = self._http.get(
                url,
                auth=HTTPDigestAuth(self._username, self._password),
                timeout=TIMEOUT,
            )
        except requests.RequestException as exc:
            raise ProbeFailure(f"RPC_Loadfile request failed: {exc}") from exc

        content_type = response.headers.get("Content-Type", "")
        print(f"RPC_Loadfile HTTP status: {response.status_code}")
        print(f"RPC_Loadfile content type: {content_type or 'missing'}")
        if response.status_code != 200:
            raise ProbeFailure(
                f"RPC_Loadfile returned HTTP {response.status_code}; "
                f"FilePath={file_path!r}; body={response.text[:2000]!r}"
            )
        if not response.content:
            raise ProbeFailure(f"RPC_Loadfile returned an empty file: {file_path!r}")

        OUTPUT_DIR.mkdir(exist_ok=True)
        filename = Path(file_path).name or "rpc_recording.dav"
        destination = OUTPUT_DIR / filename
        destination.write_bytes(response.content)
        print(f"Leading bytes: {response.content[:32].hex(' ')}")
        return destination, len(response.content), content_type

    def inspect_jpg_transfer(self, file_path: str, media_length: int) -> Path | None:
        """Read a JPG RPC_Loadfile response despite a bad outer length."""

        url = f"{self._base_url}/cgi-bin/RPC_Loadfile/{file_path.lstrip('/')}"
        try:
            response = self._http.get(
                url,
                auth=HTTPDigestAuth(self._username, self._password),
                timeout=TIMEOUT,
                stream=True,
            )
            response.raw.enforce_content_length = False
            delivered = b"".join(
                response.raw.stream(8192, decode_content=False)
            )
        except requests.RequestException as exc:
            raise ProbeFailure(f"RPC_Loadfile request failed: {exc}") from exc

        content_type = response.headers.get("Content-Type", "")
        declared_text = response.headers.get("Content-Length")
        try:
            declared_length = int(declared_text) if declared_text is not None else None
        except ValueError:
            declared_length = None

        payload = delivered
        framing = "none"
        jpeg_start = delivered.find(b"\xff\xd8")
        if jpeg_start > 0:
            prefix = delivered[:jpeg_start]
            if prefix.startswith(b"HTTP/") and prefix.endswith(b"\r\n\r\n"):
                framing = prefix.decode("iso-8859-1").replace("\r\n", " | ")
                payload = delivered[jpeg_start:]

        soi = payload.startswith(b"\xff\xd8")
        eoi = payload.endswith(b"\xff\xd9")
        print(f"HTTP status: {response.status_code}")
        print(f"Content-Type: {content_type or 'missing'}")
        print(f"Declared Content-Length: {declared_text or 'missing'}")
        print(f"mediaFileFind Length: {media_length}")
        print(f"Actual delivered bytes: {len(delivered)}")
        print(f"First 16 bytes: {payload[:16].hex(' ')}")
        print(f"Last 16 bytes: {payload[-16:].hex(' ')}")
        print(f"Inner framing: {framing}")
        print(f"JPEG SOI present: {soi}")
        print(f"JPEG EOI present: {eoi}")
        print(
            "Declared length equals media length x 96: "
            f"{declared_length == media_length * 96}"
        )

        if response.status_code != 200:
            raise ProbeFailure(f"RPC_Loadfile returned HTTP {response.status_code}.")
        if not (soi and eoi):
            return None

        OUTPUT_DIR.mkdir(exist_ok=True)
        destination = OUTPUT_DIR / Path(file_path).name
        destination.write_bytes(payload)
        print("JPEG validation: PASS")
        print(f"Saved JPG: {destination}")
        return destination

    def load_jpg_sample(self, recording: dict[str, Any]) -> dict[str, Any]:
        """Return metadata for one strictly validated raw JPG transfer."""

        file_path = recording.get("FilePath")
        media_length = recording.get("Length")
        if not isinstance(file_path, str) or not isinstance(media_length, int):
            raise ProbeFailure("JPG record omitted FilePath or Length.")
        url = f"{self._base_url}/cgi-bin/RPC_Loadfile/{file_path.lstrip('/')}"
        try:
            with self._http.get(
                url,
                auth=HTTPDigestAuth(self._username, self._password),
                timeout=TIMEOUT,
                stream=True,
            ) as response:
                response.raw.enforce_content_length = False
                payload = b"".join(
                    response.raw.stream(8192, decode_content=False)
                )
                status = response.status_code
                declared_text = response.headers.get("Content-Length")
        except requests.RequestException as exc:
            raise ProbeFailure(f"RPC_Loadfile request failed: {exc}") from exc

        if status != 200:
            raise ProbeFailure(f"RPC_Loadfile returned HTTP {status}.")
        if not payload.startswith(b"\xff\xd8"):
            raise ProbeFailure("RPC_Loadfile JPG omitted JPEG SOI.")
        if not payload.endswith(b"\xff\xd9"):
            raise ProbeFailure("RPC_Loadfile JPG omitted JPEG EOI.")
        try:
            declared_length = int(declared_text) if declared_text is not None else None
        except ValueError:
            declared_length = None
        width, height = _jpeg_dimensions(payload)
        return {
            "timestamp": recording.get("StartTime"),
            "media_length": media_length,
            "declared_length": declared_length,
            "actual_length": len(payload),
            "width": width,
            "height": height,
            "video_stream": recording.get("VideoStream"),
            "events": recording.get("Events"),
            "file_path": file_path,
        }

    def close(self) -> None:
        self._http.close()


def _safe(value: Any) -> Any:
    if isinstance(value, dict):
        return {
            key: "<redacted>" if key.casefold() in SENSITIVE_KEYS else _safe(item)
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [_safe(item) for item in value]
    return value


def _safe_json(value: Any) -> str:
    return json.dumps(_safe(value), sort_keys=True)


def _jpeg_dimensions(payload: bytes) -> tuple[int, int]:
    """Read JPEG dimensions from a Start Of Frame segment."""

    sof_markers = {
        0xC0,
        0xC1,
        0xC2,
        0xC3,
        0xC5,
        0xC6,
        0xC7,
        0xC9,
        0xCA,
        0xCB,
        0xCD,
        0xCE,
        0xCF,
    }
    offset = 2
    while offset + 4 <= len(payload):
        if payload[offset] != 0xFF:
            raise ProbeFailure("JPEG contained invalid marker framing.")
        while offset < len(payload) and payload[offset] == 0xFF:
            offset += 1
        if offset >= len(payload):
            break
        marker = payload[offset]
        offset += 1
        if marker in {0x01, 0xD8, 0xD9} or 0xD0 <= marker <= 0xD7:
            continue
        if offset + 2 > len(payload):
            break
        segment_length = int.from_bytes(payload[offset : offset + 2], "big")
        if segment_length < 2 or offset + segment_length > len(payload):
            raise ProbeFailure("JPEG contained an invalid segment length.")
        if marker in sof_markers:
            if segment_length < 7:
                raise ProbeFailure("JPEG SOF segment was too short.")
            height = int.from_bytes(payload[offset + 3 : offset + 5], "big")
            width = int.from_bytes(payload[offset + 5 : offset + 7], "big")
            if width < 1 or height < 1:
                raise ProbeFailure("JPEG SOF returned invalid dimensions.")
            return width, height
        if marker == 0xDA:
            break
        offset += segment_length
    raise ProbeFailure("JPEG did not contain a supported SOF segment.")


def _print_recording(index: int, recording: dict[str, Any]) -> None:
    print(f"\nRecording {index}:")
    for field in METADATA_FIELDS:
        if field in recording:
            print(f"  {field}: {recording[field]}")


def main() -> None:
    username = os.environ.get("DAHUA_USERNAME")
    password = os.environ.get("DAHUA_PASSWORD")
    if not username or not password:
        raise SystemExit("Set DAHUA_USERNAME and DAHUA_PASSWORD before running.")

    status = {
        "RPC login": "FAIL",
        "mediaFileFind create": "FAIL",
        "findFile": "FAIL",
        "findNextFile": "FAIL",
        "Recording metadata usable for timeline": "NO",
        "RPC_Loadfile recording retrieval": "NOT ATTEMPTED",
    }
    finder: int | None = None
    downloaded: tuple[Path, int, str] | None = None
    unresolved: str | None = None
    probe = RpcProbe(HOST, username, password)

    try:
        probe.login()
        status["RPC login"] = "PASS"

        created = probe.rpc("mediaFileFind.factory.create")
        finder = int(created["result"])
        status["mediaFileFind create"] = "PASS"

        condition = {
            "Channel": CHANNEL,
            "Dirs": None,
            "Types": ["dav"],
            "Order": "Ascent",
            "Redundant": "Exclusion",
            "Events": None,
            "StartTime": START_TIME,
            "EndTime": END_TIME,
            "Flags": ["Timing", "Event", "Manual"],
        }
        probe.rpc(
            "mediaFileFind.findFile",
            {"condition": condition},
            object_id=finder,
        )
        status["findFile"] = "PASS"

        page = probe.rpc(
            "mediaFileFind.findNextFile",
            {"count": RESULT_COUNT},
            object_id=finder,
        )
        status["findNextFile"] = "PASS"
        params = page.get("params") or {}
        recordings = params.get("infos") or []
        print(f"\nReturned recording count: {params.get('found', len(recordings))}")
        for index, recording in enumerate(recordings[:PRINT_COUNT], start=1):
            _print_recording(index, recording)

        timeline_fields = {"Channel", "StartTime", "EndTime", "FilePath"}
        if recordings and all(timeline_fields <= item.keys() for item in recordings):
            status["Recording metadata usable for timeline"] = "YES"

        local = [
            item
            for item in recordings
            if str(item.get("FilePath", "")).startswith("/")
        ]
        if not local:
            unresolved = (
                "No returned recording had a local FilePath beginning with '/'."
            )
        else:
            selected = min(local, key=lambda item: int(item.get("Length") or 0))
            file_path = str(selected["FilePath"])
            print(f"\nSelected RPC_Loadfile FilePath: {file_path}")
            status["RPC_Loadfile recording retrieval"] = "FAIL"
            downloaded = probe.retrieve(file_path)
            status["RPC_Loadfile recording retrieval"] = "PASS"
    except (ProbeFailure, KeyError, TypeError, ValueError) as exc:
        unresolved = str(exc)
        print(f"\nPROBE STOPPED: {unresolved}")
    finally:
        if finder is not None:
            for method in ("mediaFileFind.close", "mediaFileFind.destroy"):
                try:
                    probe.rpc(method, object_id=finder)
                except ProbeFailure as exc:
                    print(f"Cleanup warning: {exc}")
        if status["RPC login"] == "PASS":
            try:
                probe.rpc("global.logout")
            except ProbeFailure as exc:
                print(f"Logout warning: {exc}")
        probe.close()

    print("\n=== FEASIBILITY REPORT ===")
    for label, result in status.items():
        print(f"{label}: {result}")
    if downloaded is not None:
        path, size, content_type = downloaded
        print(f"Downloaded file: {path} ({size:,} bytes, {content_type or 'unknown'})")
    print(f"Exact unresolved issue: {unresolved or 'None'}")


if __name__ == "__main__":
    main()
