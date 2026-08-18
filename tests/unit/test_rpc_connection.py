import time
from unittest import TestCase
from unittest.mock import Mock

import requests

from dahua_rpc._rpc_connection import _RpcConnection
from dahua_rpc.exceptions import (
    AuthenticationError,
    InvalidResponseError,
    RecorderConnectionError,
)


class RpcLoginTests(TestCase):
    def setUp(self) -> None:
        self.connection = _connection()
        self.connection._http = self.http = Mock()

    def tearDown(self) -> None:
        self.connection.stop_keepalive()

    def test_login_accepts_expected_challenge_and_retains_session(self) -> None:
        self.http.request.side_effect = [
            _response(
                {
                    "result": False,
                    "error": {"code": 268632079},
                    "session": "challenge-session",
                    "params": {
                        "realm": "Login to recorder",
                        "random": "01234567",
                        "encryption": "Default",
                    },
                }
            ),
            _response({"result": True, "session": "active-session"}),
        ]
        self.connection.login()
        self.assertEqual(self.connection._session, "active-session")
        first_payload = self.http.request.call_args_list[0].kwargs["json"]
        second_payload = self.http.request.call_args_list[1].kwargs["json"]
        self.assertEqual(first_payload["id"], 1)
        self.assertNotIn("session", first_payload)
        self.assertEqual(second_payload["id"], 2)
        self.assertEqual(second_payload["session"], "challenge-session")
        self.assertEqual(
            second_payload["params"]["password"],
            "06E82A0BBA966C1146A3492DA3CF4487",
        )

    def test_other_first_login_error_fails(self) -> None:
        self.http.request.return_value = _response(
            {"result": False, "error": {"code": 123}}
        )
        with self.assertRaises(AuthenticationError):
            self.connection.login()

    def test_incomplete_challenge_fails(self) -> None:
        self.http.request.return_value = _response(
            {
                "result": False,
                "error": {"code": 268632079},
                "session": "session",
                "params": {"realm": "realm"},
            }
        )
        with self.assertRaisesRegex(InvalidResponseError, "incomplete"):
            self.connection.login()

    def test_second_login_failure_clears_session(self) -> None:
        self.http.request.side_effect = [
            _challenge(),
            _response({"result": False, "error": {"code": 401}}),
        ]
        with self.assertRaises(AuthenticationError):
            self.connection.login()
        self.assertIsNone(self.connection._session)


class RpcCallTests(TestCase):
    def setUp(self) -> None:
        self.connection = _connection()
        self.connection._session = "session-id"
        self.connection._http = self.http = Mock()

    def tearDown(self) -> None:
        self.connection.stop_keepalive()

    def test_ids_increment_and_session_and_object_are_inserted(self) -> None:
        self.http.request.side_effect = [
            _response({"result": True}),
            _response({"result": True}),
        ]
        self.connection.call("first", {"value": 1})
        self.connection.call("second", object_id=7)
        payloads = [item.kwargs["json"] for item in self.http.request.call_args_list]
        self.assertEqual([item["id"] for item in payloads], [1, 2])
        self.assertEqual(payloads[0]["session"], "session-id")
        self.assertEqual(payloads[1]["object"], 7)

    def test_rpc_rejection_raises_sdk_error(self) -> None:
        self.http.request.return_value = _response(
            {"result": False, "error": {"code": 1}}
        )
        with self.assertRaisesRegex(InvalidResponseError, "rejected"):
            self.connection.call("mediaFileFind.findFile")

    def test_malformed_json_and_envelopes_raise_sdk_errors(self) -> None:
        invalid_json = Mock(status_code=200)
        invalid_json.json.side_effect = requests.exceptions.JSONDecodeError(
            "bad", "", 0
        )
        for response in (
            invalid_json,
            _response([]),
            _response({"params": {}}),
        ):
            with self.subTest(response=response):
                self.http.request.return_value = response
                with self.assertRaises(InvalidResponseError):
                    self.connection.call("method")

    def test_transport_failure_is_translated(self) -> None:
        self.http.request.side_effect = requests.exceptions.ConnectionError
        with self.assertRaises(RecorderConnectionError):
            self.connection.call("method")

    def test_get_uses_digest_auth_and_exact_path(self) -> None:
        self.http.request.return_value = Mock(status_code=200)
        self.connection.get("/cgi-bin/RPC_Loadfile/mnt/dvr/file.dav")
        request = self.http.request.call_args
        self.assertEqual(request.kwargs["method"], "GET")
        self.assertEqual(
            request.kwargs["url"],
            "http://recorder.example:80/cgi-bin/RPC_Loadfile/mnt/dvr/file.dav",
        )
        self.assertIsInstance(request.kwargs["auth"], requests.auth.HTTPDigestAuth)
        self.assertNotIn("stream", request.kwargs)

    def test_close_logs_out_only_when_session_exists(self) -> None:
        self.http.request.return_value = _response({"result": True})
        self.connection.close()
        payload = self.http.request.call_args.kwargs["json"]
        self.assertEqual(payload["method"], "global.logout")
        self.assertEqual(payload["session"], "session-id")
        self.http.close.assert_called_once_with()


class RpcKeepaliveTests(TestCase):
    def setUp(self) -> None:
        self.connection = _connection(keepalive_interval=0.01)
        self.connection._http = self.http = Mock()

    def tearDown(self) -> None:
        self.connection.stop_keepalive()

    def test_login_starts_periodic_keepalive_with_evidenced_payload(self) -> None:
        self.http.request.side_effect = [
            _challenge(),
            _response({"result": True, "session": "active-session"}),
            _response({"result": True, "params": {"timeout": 60}}),
        ]

        self.connection.login()
        self.assertTrue(_wait_for(lambda: self.connection.keepalive_count == 1))
        self.connection.stop_keepalive()

        payload = self.http.request.call_args_list[2].kwargs["json"]
        self.assertEqual(payload["method"], "global.keepAlive")
        self.assertEqual(payload["session"], "active-session")
        self.assertEqual(payload["params"], {"timeout": 300, "active": True})
        self.assertEqual(payload["id"], 3)
        self.assertEqual(self.connection.keepalive_statuses, [200])

    def test_normal_call_and_keepalive_share_serial_ids(self) -> None:
        self.connection._session = "session-id"
        self.http.request.side_effect = [
            _response({"result": True, "params": {"timeout": 60}}),
            _response({"result": True}),
        ]
        self.connection._start_keepalive()
        self.assertTrue(_wait_for(lambda: self.connection.keepalive_count == 1))

        self.connection.call("global.getCurrentTime")
        self.connection.stop_keepalive()

        payloads = [item.kwargs["json"] for item in self.http.request.call_args_list]
        self.assertEqual([payload["id"] for payload in payloads], [1, 2])
        self.assertEqual(
            [payload["session"] for payload in payloads],
            ["session-id", "session-id"],
        )

    def test_stop_prevents_keepalive_from_racing_logout(self) -> None:
        self.connection._session = "session-id"
        self.http.request.return_value = _response({"result": True})
        self.connection._start_keepalive()
        self.assertTrue(_wait_for(lambda: self.connection.keepalive_count >= 1))

        self.connection.logout()
        request_count = self.http.request.call_count
        time.sleep(0.03)

        self.assertEqual(self.http.request.call_count, request_count)
        methods = [
            item.kwargs["json"]["method"]
            for item in self.http.request.call_args_list
        ]
        self.assertEqual(methods[-1], "global.logout")
        self.assertIsNone(self.connection._keepalive_thread)

    def test_transient_failure_is_retried_without_invalidating_session(self) -> None:
        self.connection._session = "session-id"
        self.http.request.side_effect = [
            requests.exceptions.Timeout(),
            _response({"result": True, "params": {"timeout": 60}}),
        ]
        self.connection._start_keepalive()

        self.assertTrue(_wait_for(lambda: self.connection.keepalive_count == 1))
        self.connection.stop_keepalive()

        self.assertEqual(self.connection.keepalive_transient_failures, 1)
        self.assertEqual(self.connection._session, "session-id")
        self.assertIsNone(self.connection._session_error)

    def test_rejected_keepalive_marks_session_invalid_without_relogin(self) -> None:
        self.connection._session = "session-id"
        self.http.request.return_value = _response(
            {"result": False, "error": {"code": 287637505}}
        )
        self.connection._start_keepalive()
        self.assertTrue(_wait_for(lambda: self.connection._session_error is not None))

        with self.assertRaisesRegex(InvalidResponseError, "keepalive was rejected"):
            self.connection.call("global.getCurrentTime")
        self.assertEqual(self.http.request.call_count, 1)

    def test_response_timeout_derives_half_timeout_interval(self) -> None:
        connection = _connection()
        connection._update_keepalive_interval(
            {"result": True, "params": {"timeout": 80}}
        )
        self.assertEqual(connection._keepalive_interval, 40)


def _connection(*, keepalive_interval: float | None = None) -> _RpcConnection:
    return _RpcConnection(
        host="recorder.example",
        port=80,
        username="admin",
        password="password",
        timeout=10.0,
        use_ssl=False,
        keepalive_interval=keepalive_interval,
    )


def _wait_for(predicate) -> bool:
    deadline = time.monotonic() + 1.0
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.005)
    return False


def _response(data) -> Mock:
    response = Mock(status_code=200)
    response.json.return_value = data
    return response


def _challenge() -> Mock:
    return _response(
        {
            "result": False,
            "error": {"code": 268632079},
            "session": "challenge-session",
            "params": {
                "realm": "Login to recorder",
                "random": "01234567",
                "encryption": "Default",
            },
        }
    )
