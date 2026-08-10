"""
Internal recorder connection.
"""

from __future__ import annotations

from typing import Any, Mapping

import requests
from requests import Response, Session
from requests.auth import HTTPDigestAuth

from .exceptions import (
    AuthenticationError,
    RecorderConnectionError,
    TransportError,
)


class _Connection:
    """
    Owns all communication with the recorder.
    """

    def __init__(
        self,
        *,
        host: str,
        port: int,
        username: str,
        password: str,
        timeout: float,
        use_ssl: bool,
    ) -> None:

        self._host = host
        self._port = port
        self._username = username
        self._password = password
        self._timeout = timeout

        self._base_url = f"{'https' if use_ssl else 'http'}://{host}:{port}"

        self._session = self._create_session()

    def get(
        self,
        path: str,
        *,
        params: Mapping[str, Any] | None = None,
    ) -> Response:

        return self.request(
            "GET",
            path,
            params=params,
        )

    def request(
        self,
        method: str,
        path: str,
        *,
        params: Mapping[str, Any] | None = None,
        data: Mapping[str, Any] | None = None,
    ) -> Response:
        """
        Send an authenticated request to the recorder.
        """

        url = f"{self._base_url}{path}"

        try:
            response = self._session.request(
                method=method,
                url=url,
                params=params,
                data=data,
                timeout=self._timeout,
            )

        except requests.exceptions.ConnectionError as exc:
            raise RecorderConnectionError(
                f"Unable to connect to recorder at {self._host}:{self._port}."
            ) from exc

        except requests.exceptions.Timeout as exc:
            raise TransportError(
                f"Timed out connecting to recorder at {self._host}:{self._port}."
            ) from exc

        except requests.exceptions.RequestException as exc:
            raise TransportError(str(exc)) from exc

        if response.status_code == 401:
            raise AuthenticationError(
                f"Authentication failed for user '{self._username}'."
            )

        return response

    def close(self) -> None:
        self._session.close()

    def _create_session(self) -> Session:
        """
        Create the HTTP session.
        """

        session = Session()

        session.auth = HTTPDigestAuth(
            self._username,
            self._password,
        )

        return session
