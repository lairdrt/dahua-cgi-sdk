"""Internal RPC2 recorder connection."""

from __future__ import annotations

import hashlib
from collections.abc import Mapping
from typing import Any

import requests
from requests import Response, Session
from requests.auth import HTTPDigestAuth

from .exceptions import (
    AuthenticationError,
    InvalidResponseError,
    RecorderConnectionError,
    TransportError,
)

_LOGIN_CHALLENGE_ERROR = 268632079


class _RpcConnection:
    """Own a lazy, reusable RPC2 session with the recorder."""

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
        scheme = "https" if use_ssl else "http"
        self._base_url = f"{scheme}://{host}:{port}"
        self._http = Session()
        self._request_id = 0
        self._session: str | None = None

    def call(
        self,
        method: str,
        params: Any = None,
        *,
        object_id: int | None = None,
    ) -> dict[str, Any]:
        """Call an RPC method, logging in on first use."""

        if self._session is None:
            self.login()
        return self._post(
            "/RPC2",
            method,
            params,
            object_id=object_id,
            include_session=True,
        )

    def login(self) -> None:
        """Establish the recorder's two-stage Default RPC session."""

        if self._session is not None:
            return
        challenge = self._post(
            "/RPC2_Login",
            "global.login",
            {
                "userName": self._username,
                "password": "",
                "clientType": "Dahua3.0-Web3.0",
            },
            include_session=False,
            allow_login_challenge=True,
        )
        try:
            session = str(challenge["session"])
            challenge_params = challenge["params"]
            realm = str(challenge_params["realm"])
            random_value = str(challenge_params["random"])
            encryption = challenge_params["encryption"]
        except (KeyError, TypeError) as exc:
            raise InvalidResponseError(
                "RPC login challenge was incomplete."
            ) from exc
        if not session:
            raise InvalidResponseError("RPC login challenge returned an empty session.")
        if encryption != "Default":
            raise AuthenticationError(
                f"Unsupported RPC authentication mode: {encryption!r}."
            )

        self._session = session
        password_response = self._challenge_response(realm, random_value)
        try:
            response = self._post(
                "/RPC2_Login",
                "global.login",
                {
                    "userName": self._username,
                    "password": password_response,
                    "clientType": "Dahua3.0-Web3.0",
                    "authorityType": "Default",
                    "passwordType": "Default",
                },
                include_session=True,
            )
        except Exception:
            self._session = None
            raise
        if returned_session := response.get("session"):
            self._session = str(returned_session)

    def get(self, path: str) -> Response:
        """Perform an HTTP Digest GET using the connection credentials."""

        return self._request(
            "GET",
            path,
            auth=HTTPDigestAuth(self._username, self._password),
        )

    def logout(self) -> None:
        """Log out the current RPC session, if one exists."""

        if self._session is None:
            return
        try:
            self._post(
                "/RPC2",
                "global.logout",
                None,
                include_session=True,
            )
        finally:
            self._session = None

    def close(self) -> None:
        """Log out and release HTTP resources."""

        try:
            self.logout()
        finally:
            self._http.close()

    def _challenge_response(self, realm: str, random_value: str) -> str:
        ha1 = hashlib.md5(
            f"{self._username}:{realm}:{self._password}".encode()
        ).hexdigest().upper()
        return hashlib.md5(
            f"{self._username}:{random_value}:{ha1}".encode()
        ).hexdigest().upper()

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
        include_session: bool,
        allow_login_challenge: bool = False,
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "method": method,
            "params": params,
            "id": self._next_id(),
        }
        if include_session:
            if self._session is None:
                raise InvalidResponseError("RPC session is not established.")
            payload["session"] = self._session
        if object_id is not None:
            payload["object"] = object_id

        response = self._request("POST", path, json=payload)
        if response.status_code != 200:
            raise InvalidResponseError(
                f"{method} returned unexpected HTTP status {response.status_code}."
            )
        try:
            data = response.json()
        except ValueError as exc:
            raise InvalidResponseError(
                f"{method} response was not valid JSON."
            ) from exc
        if not isinstance(data, dict):
            raise InvalidResponseError(f"{method} returned a malformed RPC response.")

        if allow_login_challenge and self._is_login_challenge(data):
            return data
        if data.get("result") is False:
            if method == "global.login":
                raise AuthenticationError("RPC authentication failed.")
            raise InvalidResponseError(f"{method} was rejected by the recorder.")
        if data.get("error") not in (None, {}):
            raise InvalidResponseError(f"{method} returned an RPC error.")
        if "result" not in data:
            raise InvalidResponseError(f"{method} response omitted result.")
        return data

    @staticmethod
    def _is_login_challenge(data: Mapping[str, Any]) -> bool:
        error = data.get("error")
        return (
            data.get("result") is False
            and isinstance(error, Mapping)
            and error.get("code") == _LOGIN_CHALLENGE_ERROR
        )

    def _request(self, method: str, path: str, **kwargs: Any) -> Response:
        try:
            response = self._http.request(
                method=method,
                url=f"{self._base_url}{path}",
                timeout=self._timeout,
                **kwargs,
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
