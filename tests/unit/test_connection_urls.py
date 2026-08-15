from unittest import TestCase
from unittest.mock import Mock

import requests

from dahua_cgi._connection import _Connection
from dahua_cgi.exceptions import AuthenticationError, RecorderConnectionError


class ConnectionUrlTests(TestCase):
    def test_request_uses_http_url_when_ssl_is_disabled(self) -> None:
        connection = _connection(use_ssl=False, port=8080)
        connection._session = session = Mock()
        session.request.return_value = Mock(status_code=200)

        connection.get("/cgi-bin/snapshot.cgi")

        session.request.assert_called_once_with(
            method="GET",
            url="http://recorder.example:8080/cgi-bin/snapshot.cgi",
            params=None,
            data=None,
            json=None,
            timeout=10.0,
        )

    def test_request_uses_https_url_when_ssl_is_enabled(self) -> None:
        connection = _connection(use_ssl=True, port=8443)
        connection._session = session = Mock()
        session.request.return_value = Mock(status_code=200)

        connection.get("/cgi-bin/snapshot.cgi")

        session.request.assert_called_once_with(
            method="GET",
            url="https://recorder.example:8443/cgi-bin/snapshot.cgi",
            params=None,
            data=None,
            json=None,
            timeout=10.0,
        )

    def test_request_forwards_json_payload(self) -> None:
        connection = _connection(use_ssl=False, port=80)
        connection._session = session = Mock()
        session.request.return_value = Mock(status_code=200)
        payload = {"method": "getCameraState", "params": {"channel": 1}}

        connection.request("POST", "/RPC2", json=payload)

        session.request.assert_called_once_with(
            method="POST",
            url="http://recorder.example:80/RPC2",
            params=None,
            data=None,
            json=payload,
            timeout=10.0,
        )

    def test_request_forwards_params_and_json_together(self) -> None:
        connection = _connection(use_ssl=True, port=443)
        connection._session = session = Mock()
        session.request.return_value = Mock(status_code=200)
        params = {"action": "invoke"}
        payload = {"method": "example"}

        connection.request("POST", "/RPC2", params=params, json=payload)

        session.request.assert_called_once_with(
            method="POST",
            url="https://recorder.example:443/RPC2",
            params=params,
            data=None,
            json=payload,
            timeout=10.0,
        )

    def test_json_request_preserves_authentication_error_handling(self) -> None:
        connection = _connection(use_ssl=False, port=80)
        connection._session = session = Mock()
        session.request.return_value = Mock(status_code=401)

        with self.assertRaises(AuthenticationError):
            connection.request("POST", "/RPC2", json={"method": "example"})

    def test_json_request_preserves_connection_error_handling(self) -> None:
        connection = _connection(use_ssl=False, port=80)
        connection._session = session = Mock()
        session.request.side_effect = requests.exceptions.ConnectionError

        with self.assertRaises(RecorderConnectionError):
            connection.request("POST", "/RPC2", json={"method": "example"})


def _connection(*, use_ssl: bool, port: int) -> _Connection:
    return _Connection(
        host="recorder.example",
        port=port,
        username="admin",
        password="password",
        timeout=10.0,
        use_ssl=use_ssl,
    )
