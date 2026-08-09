from unittest import TestCase
from unittest.mock import Mock

from dahua_cgi._connection import _Connection


class ConnectionUrlTests(TestCase):
    def test_request_uses_http_url_when_ssl_is_disabled(self) -> None:
        connection = _connection(use_ssl=False, port=8080)
        connection._session = session = Mock()
        session.request.return_value = Mock(status_code=200)

        connection.get("/cgi-bin/magicBox.cgi")

        session.request.assert_called_once_with(
            method="GET",
            url="http://recorder.example:8080/cgi-bin/magicBox.cgi",
            params=None,
            data=None,
            timeout=10.0,
        )

    def test_request_uses_https_url_when_ssl_is_enabled(self) -> None:
        connection = _connection(use_ssl=True, port=8443)
        connection._session = session = Mock()
        session.request.return_value = Mock(status_code=200)

        connection.get("/cgi-bin/magicBox.cgi")

        session.request.assert_called_once_with(
            method="GET",
            url="https://recorder.example:8443/cgi-bin/magicBox.cgi",
            params=None,
            data=None,
            timeout=10.0,
        )


def _connection(*, use_ssl: bool, port: int) -> _Connection:
    return _Connection(
        host="recorder.example",
        port=port,
        username="admin",
        password="password",
        timeout=10.0,
        use_ssl=use_ssl,
    )
