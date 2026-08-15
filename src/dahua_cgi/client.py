"""
Core Dahua client.

This module implements the foundation of the SDK.

A DahuaClient instance represents a verified connection to a
single Dahua-compatible recorder.

Successful construction guarantees that:

- the recorder is reachable
- authentication has succeeded
- immutable recorder identity has been established
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from ._connection import _Connection
from ._rpc_connection import _RpcConnection
from .cameras import CameraService
from .exceptions import (
    InvalidResponseError,
)
from .media import MediaService


class DahuaClient:
    """
    Represents a verified connection to a single Dahua-compatible recorder.

    Successful construction guarantees:

    - constructor arguments are valid
    - the recorder is reachable
    - authentication succeeded
    - the recorder responded to a verification request

    The client is immutable after construction.
    """

    def __init__(
        self,
        *,
        host: str,
        username: str,
        password: str,
        port: int | None = None,
        use_ssl: bool = False,
        timeout: float = 10.0,
    ) -> None:

        self._validate_arguments(
            host=host,
            username=username,
            password=password,
            port=port,
            timeout=timeout,
        )

        self._host = host
        self._username = username
        self._password = password
        self._use_ssl = use_ssl
        self._port = port if port is not None else (443 if use_ssl else 80)
        self._timeout = timeout

        self._base_url = (
            f"{'https' if self._use_ssl else 'http'}://" f"{self._host}:{self._port}"
        )

        self._connection = _Connection(
            host=self._host,
            port=self._port,
            username=self._username,
            password=self._password,
            timeout=self._timeout,
            use_ssl=self._use_ssl,
        )
        self._rpc_connection = _RpcConnection(
            host=self._host,
            port=self._port,
            username=self._username,
            password=self._password,
            timeout=self._timeout,
            use_ssl=self._use_ssl,
        )

        self._media = MediaService(
            connection=self._rpc_connection,
        )
        self._cameras = CameraService(
            connection=self._rpc_connection,
            snapshot_connection=self._connection,
        )

        #
        # Immutable recorder identity.
        # These will be populated during verification.
        #
        self._manufacturer: str | None = None
        self._model: str | None = None
        self._serial_number: str | None = None
        self._hardware_revision: str | None = None
        self._firmware_version: str | None = None
        self._api_version: str | None = None
        self._processor: str | None = None

        self._verify_connection()

    #
    # ------------------------------------------------------------------
    # Public properties
    # ------------------------------------------------------------------
    #

    @property
    def host(self) -> str:
        return self._host

    @property
    def port(self) -> int:
        return self._port

    @property
    def timeout(self) -> float:
        return self._timeout

    @property
    def base_url(self) -> str:
        return self._base_url

    @property
    def manufacturer(self) -> str | None:
        """Recorder manufacturer."""
        return self._manufacturer

    @property
    def model(self) -> str | None:
        """Recorder model."""
        return self._model

    @property
    def serial_number(self) -> str | None:
        """Recorder serial number."""
        return self._serial_number

    @property
    def hardware_revision(self) -> str | None:
        """Recorder hardware revision."""
        return self._hardware_revision

    @property
    def firmware_version(self) -> str | None:
        """Recorder firmware version."""
        return self._firmware_version

    @property
    def api_version(self) -> str | None:
        """Recorder web/API version."""
        return self._api_version

    @property
    def processor(self) -> str | None:
        """Recorder processor type."""
        return self._processor

    @property
    def media(self) -> MediaService:
        """
        Recorder media.
        """
        return self._media

    @property
    def cameras(self) -> CameraService:
        """Recorder cameras."""

        return self._cameras

    #
    # ------------------------------------------------------------------
    # Public methods
    # ------------------------------------------------------------------
    #

    def close(self) -> None:
        """Release underlying HTTP resources."""
        try:
            self._rpc_connection.close()
        finally:
            self._connection.close()

    def __enter__(self) -> "DahuaClient":
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.close()

    #
    # ------------------------------------------------------------------
    # Private methods
    # ------------------------------------------------------------------
    #

    @staticmethod
    def _validate_arguments(
        *,
        host: str,
        username: str,
        password: str,
        port: int | None,
        timeout: float,
    ) -> None:
        """Validate constructor arguments."""

        if not host.strip():
            raise ValueError("host must not be empty")

        if not username.strip():
            raise ValueError("username must not be empty")

        if not password:
            raise ValueError("password must not be empty")

        if port is not None and not (1 <= port <= 65535):
            raise ValueError("port must be between 1 and 65535")

        if timeout <= 0:
            raise ValueError("timeout must be greater than zero")

    def _verify_connection(self) -> None:
        """
        Verify connectivity and authentication.

        The constructor guarantees that a DahuaClient instance represents a
        reachable, authenticated recorder.
        """

        system_info = self._rpc_params(
            self._rpc_connection.call("magicBox.getSystemInfo"),
            method="magicBox.getSystemInfo",
        )
        software = self._rpc_params(
            self._rpc_connection.call("magicBox.getSoftwareVersion"),
            method="magicBox.getSoftwareVersion",
        )
        hardware = self._rpc_params(
            self._rpc_connection.call("magicBox.getHardwareVersion"),
            method="magicBox.getHardwareVersion",
        )
        software_version = software.get("version")
        if not isinstance(software_version, Mapping):
            raise InvalidResponseError(
                "magicBox.getSoftwareVersion returned an invalid version."
            )

        self._manufacturer = self._optional_string(
            system_info, "manufacturer", "Manufacturer"
        )
        self._model = (
            self._optional_string(system_info, "model", "Model")
            or self._optional_string(system_info, "updateSerial")
            or self._optional_string(system_info, "deviceType", "DeviceType")
        )
        self._serial_number = self._optional_string(
            system_info, "serialNumber", "SerialNumber"
        )
        self._hardware_revision = self._optional_string(hardware, "version")
        self._firmware_version = self._optional_string(
            software_version, "Version", "version"
        )
        self._api_version = self._optional_string(
            software_version, "WebVersion", "webVersion"
        )
        self._processor = self._optional_string(
            system_info, "processor", "Processor"
        )
        if self._model is None:
            raise InvalidResponseError(
                "Unable to determine recorder model from system information."
            )

    @staticmethod
    def _rpc_params(response: Any, *, method: str) -> Mapping[str, Any]:
        params = response.get("params") if isinstance(response, Mapping) else None
        if not isinstance(params, Mapping):
            raise InvalidResponseError(f"{method} response omitted valid params.")
        return params

    @staticmethod
    def _optional_string(
        values: Mapping[str, Any], *names: str
    ) -> str | None:
        for name in names:
            value = values.get(name)
            if value is None:
                continue
            if not isinstance(value, str):
                raise InvalidResponseError(
                    f"Recorder identity field {name} was not a string."
                )
            return value or None
        return None
