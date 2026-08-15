from unittest import TestCase
from unittest.mock import Mock, patch

from dahua_cgi.client import DahuaClient


class DahuaClientRpcIntegrationTests(TestCase):
    @patch("dahua_cgi.client._RpcConnection")
    @patch("dahua_cgi.client._Connection")
    def test_client_owns_lazy_rpc_connection_and_closes_both(
        self, connection_type: Mock, rpc_type: Mock
    ) -> None:
        connection = connection_type.return_value
        connection.get.return_value = Mock(
            status_code=200,
            text="deviceType=NVR",
        )

        client = DahuaClient(
            host="recorder.example",
            username="admin",
            password="password",
            port=8080,
            timeout=5.0,
        )

        rpc_type.assert_called_once_with(
            host="recorder.example",
            port=8080,
            username="admin",
            password="password",
            timeout=5.0,
            use_ssl=False,
        )
        rpc_type.return_value.login.assert_not_called()
        self.assertIs(client.media._connection, rpc_type.return_value)

        client.close()

        rpc_type.return_value.close.assert_called_once_with()
        connection.close.assert_called_once_with()
