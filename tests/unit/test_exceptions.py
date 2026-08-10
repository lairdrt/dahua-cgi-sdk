from unittest import TestCase

from dahua_cgi.exceptions import DahuaError, InvalidResponseError, TransportError


class InvalidResponseErrorTests(TestCase):
    def test_invalid_response_error_is_not_a_transport_error(self) -> None:
        error = InvalidResponseError()

        self.assertIsInstance(error, DahuaError)
        self.assertNotIsInstance(error, TransportError)
