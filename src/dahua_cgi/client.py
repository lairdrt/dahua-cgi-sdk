"""
Core Dahua client.

This module implements the foundation of the SDK. It is intentionally
independent of any specific CGI endpoint beyond the minimal connection
verification performed during construction.

A DahuaClient instance represents a verified connection to a
single Dahua-compatible recorder.

Successful construction guarantees that:

- the recorder is reachable
- authentication has succeeded
- immutable recorder identity has been established
"""

from __future__ import annotations

import logging

from requests import Response

from ._connection import _Connection
from .exceptions import (
    InvalidResponseError,
)
from .media import MediaService
from .parsers import parse_cgi_properties


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

        self._logger = logging.getLogger(__name__)

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

        self._media = MediaService(
            connection=self._connection,
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

    #
    # ------------------------------------------------------------------
    # Public methods
    # ------------------------------------------------------------------
    #

    def close(self) -> None:
        """Release underlying HTTP resources."""
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

        response = self._connection.get(
            "/cgi-bin/magicBox.cgi",
            params={
                "action": "getSystemInfo",
            },
        )
        if response.status_code != 200:
            raise InvalidResponseError(
                f"Unexpected HTTP status code: {response.status_code}"
            )

        #
        # Identity parsing will be implemented next.
        #
        self._load_identity(response)

    def _load_identity(self, response: Response) -> None:
        """
        Populate immutable recorder identity from the recorder response.
        """

        values = parse_cgi_properties(response.text)

        #
        # These keys are intentionally tolerant.
        # Different firmware revisions expose slightly different names.
        #
        self._manufacturer = values.get("manufacturer") or values.get("Manufacturer")

        self._model = (
            values.get("model")
            or values.get("Model")
            or values.get("updateSerial")
            or values.get("deviceType")
            or values.get("DeviceType")
        )

        self._serial_number = values.get("serialNumber") or values.get("SerialNumber")

        self._hardware_revision = values.get("hardwareVersion") or values.get(
            "HardwareVersion"
        )

        self._firmware_version = values.get("version") or values.get("Version")

        self._api_version = values.get("webVersion") or values.get("WebVersion")

        self._processor = values.get("processor") or values.get("Processor")

        #
        # Verify we learned enough to identify the recorder.
        #
        if self._model is None:
            raise InvalidResponseError(
                "Unable to determine recorder model from system information."
            )
