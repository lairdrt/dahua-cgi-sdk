from unittest import TestCase
from unittest.mock import Mock, call, patch

from dahua_cgi.client import DahuaClient
from dahua_cgi.exceptions import InvalidResponseError


class DahuaClientRpcIntegrationTests(TestCase):
    @patch("dahua_cgi.client._RpcConnection")
    def test_client_owns_lazy_rpc_connection_and_closes_it(
        self, rpc_type: Mock
    ) -> None:
        rpc = rpc_type.return_value
        rpc.call.side_effect = _identity_responses()

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
        self.assertEqual(
            rpc.call.call_args_list,
            [
                call("magicBox.getSystemInfo"),
                call("magicBox.getSoftwareVersion"),
                call("magicBox.getHardwareVersion"),
            ],
        )
        self.assertEqual(client.model, "DHI-NVR5216-16P-4KS2E")
        self.assertEqual(client.serial_number, "serial")
        self.assertEqual(client.hardware_revision, "hardware")
        self.assertEqual(client.firmware_version, "software")
        self.assertEqual(client.api_version, "web")
        self.assertEqual(client.processor, "processor")
        self.assertIsNone(client.manufacturer)
        self.assertIs(client.media._connection, rpc)
        client.close()

        rpc_type.return_value.close.assert_called_once_with()

    @patch("dahua_cgi.client._RpcConnection")
    def test_malformed_rpc_identity_is_rejected(
        self, rpc_type: Mock
    ) -> None:
        rpc_type.return_value.call.return_value = {"params": []}
        with self.assertRaisesRegex(InvalidResponseError, "valid params"):
            DahuaClient(host="recorder.example", username="admin", password="x")

    @patch("dahua_cgi.client._RpcConnection")
    def test_missing_model_is_rejected_without_cgi_fallback(
        self, rpc_type: Mock
    ) -> None:
        responses = _identity_responses()
        responses[0] = {"params": {"serialNumber": "serial"}}
        rpc_type.return_value.call.side_effect = responses
        with self.assertRaisesRegex(InvalidResponseError, "determine recorder model"):
            DahuaClient(host="recorder.example", username="admin", password="x")


def _identity_responses() -> list[dict]:
    return [
        {
            "params": {
                "processor": "processor",
                "serialNumber": "serial",
                "updateSerial": "DHI-NVR5216-16P-4KS2E",
            }
        },
        {"params": {"version": {"Version": "software", "WebVersion": "web"}}},
        {"params": {"version": "hardware"}},
    ]
