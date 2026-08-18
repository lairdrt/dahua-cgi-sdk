"""Internal RPC2 recorder connection."""

from __future__ import annotations

import hashlib
import threading
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
_KEEPALIVE_REQUEST_TIMEOUT = 300
_DEFAULT_KEEPALIVE_INTERVAL = 30.0
_KEEPALIVE_TIMEOUT_FRACTION = 0.5


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
        keepalive_interval: float | None = None,
    ) -> None:
        self._host = host
        self._port = port
        self._username = username
        self._password = password
        self._timeout = timeout
        scheme = "https" if use_ssl else "http"
        self._base_url = f"{scheme}://{host}:{port}"
        self._http = Session()
        if keepalive_interval is not None and keepalive_interval <= 0:
            raise ValueError("keepalive_interval must be greater than zero")
        self._lock = threading.RLock()
        self._request_id = 0
        self._session: str | None = None
        self._session_error: Exception | None = None
        self._keepalive_interval_override = keepalive_interval
        self._keepalive_interval = keepalive_interval or _DEFAULT_KEEPALIVE_INTERVAL
        self._keepalive_stop = threading.Event()
        self._keepalive_thread: threading.Thread | None = None
        self.keepalive_count = 0
        self.keepalive_statuses: list[int] = []
        self.keepalive_transient_failures = 0

    def call(
        self,
        method: str,
        params: Any = None,
        *,
        object_id: int | None = None,
    ) -> dict[str, Any]:
        """Call an RPC method, logging in on first use."""

        with self._lock:
            if self._session_error is not None:
                raise self._session_error
            if self._session is None:
                self._login_locked()
            return self._post(
                "/RPC2",
                method,
                params,
                object_id=object_id,
                include_session=True,
            )

    def login(self) -> None:
        """Establish the recorder's two-stage Default RPC session."""

        with self._lock:
            self._login_locked()

    def _login_locked(self) -> None:
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
        self._session_error = None
        self._start_keepalive()

    def get(self, path: str, *, stream: bool = False) -> Response:
        """Perform an HTTP Digest GET using the connection credentials."""

        kwargs: dict[str, Any] = {
            "auth": HTTPDigestAuth(self._username, self._password)
        }
        if stream:
            kwargs["stream"] = True
        with self._lock:
            return self._request(
                "GET",
                path,
                **kwargs,
            )

    def logout(self) -> None:
        """Log out the current RPC session, if one exists."""

        self.stop_keepalive()
        with self._lock:
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
                self._session_error = None

    def close(self) -> None:
        """Log out and release HTTP resources."""

        self.stop_keepalive()
        try:
            self.logout()
        finally:
            with self._lock:
                self._http.close()

    def stop_keepalive(self) -> None:
        """Stop RPC session maintenance before client resource cleanup."""

        thread = self._keepalive_thread
        self._keepalive_thread = None
        self._keepalive_stop.set()
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=self._timeout + 1.0)

    def _start_keepalive(self) -> None:
        if self._keepalive_thread is not None:
            return
        self._keepalive_stop.clear()
        self._keepalive_thread = threading.Thread(
            name="dahua-rpc-keepalive", target=self._keepalive_loop
        )
        self._keepalive_thread.start()

    def _keepalive_loop(self) -> None:
        while not self._keepalive_stop.wait(self._keepalive_interval):
            try:
                with self._lock:
                    if self._session is None:
                        return
                    response = self._post(
                        "/RPC2",
                        "global.keepAlive",
                        {
                            "timeout": _KEEPALIVE_REQUEST_TIMEOUT,
                            "active": True,
                        },
                        include_session=True,
                    )
                    self._update_keepalive_interval(response)
                    self.keepalive_count += 1
                    self.keepalive_statuses.append(200)
            except (RecorderConnectionError, TransportError):
                self.keepalive_transient_failures += 1
                continue
            except (AuthenticationError, InvalidResponseError):
                with self._lock:
                    self._session_error = InvalidResponseError(
                        "RPC session keepalive was rejected by the recorder."
                    )
                return

    def _update_keepalive_interval(self, response: Mapping[str, Any]) -> None:
        if self._keepalive_interval_override is not None:
            return
        params = response.get("params")
        if not isinstance(params, Mapping):
            return
        timeout = params.get("timeout")
        if isinstance(timeout, (int, float)) and not isinstance(timeout, bool):
            if timeout > 0:
                self._keepalive_interval = timeout * _KEEPALIVE_TIMEOUT_FRACTION

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
