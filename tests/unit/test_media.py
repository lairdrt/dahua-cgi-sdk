from datetime import datetime
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import TestCase
from unittest.mock import Mock, call

from dahua_cgi.exceptions import InvalidResponseError
from dahua_cgi.media import MediaService
from dahua_cgi.models import Recording


class MediaServiceDownloadTests(TestCase):
    def test_download_requests_recording_file_and_writes_response(self) -> None:
        connection = Mock()
        connection.get.return_value = Mock(
            status_code=200,
            content=b"recording data",
        )

        with TemporaryDirectory() as directory:
            destination = Path(directory) / "recording.dav"

            result = MediaService(connection).download(
                _recording(file_path="mnt/dvr/recording.dav"),
                destination,
            )

            self.assertEqual(result, destination)
            self.assertEqual(destination.read_bytes(), b"recording data")

        connection.get.assert_called_once_with(
            "/cgi-bin/RPC_Loadfile/mnt/dvr/recording.dav"
        )

    def test_download_accepts_file_paths_with_a_leading_slash(self) -> None:
        connection = Mock()
        connection.get.return_value = Mock(
            status_code=200,
            content=b"recording data",
        )

        with TemporaryDirectory() as directory:
            MediaService(connection).download(
                _recording(file_path="/mnt/dvr/recording.dav"),
                Path(directory) / "recording.dav",
            )

        connection.get.assert_called_once_with(
            "/cgi-bin/RPC_Loadfile/mnt/dvr/recording.dav"
        )

    def test_download_does_not_write_when_recorder_returns_an_error(self) -> None:
        connection = Mock()
        connection.get.return_value = Mock(status_code=404)

        with TemporaryDirectory() as directory:
            destination = Path(directory) / "recording.dav"

            with self.assertRaisesRegex(InvalidResponseError, "404"):
                MediaService(connection).download(_recording(), destination)

            self.assertFalse(destination.exists())


class MediaSearchLifecycleTests(TestCase):
    def setUp(self) -> None:
        self.connection = Mock()
        self.service = MediaService(self.connection)

    def search(self):
        return self.service.search(
            channel=1,
            start=datetime(2026, 8, 6, 7, 14, 13),
            end=datetime(2026, 8, 6, 7, 15, 45),
        )

    def test_search_is_lazy(self) -> None:
        self.search()

        self.connection.get.assert_not_called()

    def test_iteration_returns_recordings_and_closes_on_exhaustion(self) -> None:
        self.connection.get.side_effect = [
            _response("result=7"),
            _response(),
            _response(_recording_response()),
            _response(),
            _response(),
        ]
        search = self.search()

        self.assertEqual(list(search), [_recording()])

        self.assertEqual(
            self.connection.get.call_args_list[-1],
            call(
                "/cgi-bin/mediaFileFind.cgi",
                params={"action": "close", "object": 7},
            ),
        )
        with self.assertRaises(StopIteration):
            next(search)
        self.assertEqual(self.connection.get.call_count, 5)

    def test_factory_create_requires_successful_http_response(self) -> None:
        self.connection.get.return_value = _response(status_code=400)

        with self.assertRaisesRegex(InvalidResponseError, "400"):
            next(self.search())

    def test_find_file_requires_successful_http_response(self) -> None:
        self.connection.get.side_effect = [
            _response("result=7"),
            _response(status_code=400),
            _response(),
        ]

        with self.assertRaisesRegex(InvalidResponseError, "400"):
            next(self.search())

    def test_find_next_file_requires_successful_http_response(self) -> None:
        self.connection.get.side_effect = [
            _response("result=7"),
            _response(),
            _response(status_code=400),
            _response(),
        ]

        with self.assertRaisesRegex(InvalidResponseError, "400"):
            next(self.search())

    def test_close_failure_does_not_mask_iteration_error(self) -> None:
        self.connection.get.side_effect = [
            _response("result=7"),
            _response(),
            _response("items[0].Channel=1"),
            RuntimeError("close failed"),
        ]

        with self.assertRaises(InvalidResponseError):
            next(self.search())

    def test_explicit_close_closes_active_search_only_once(self) -> None:
        self.connection.get.side_effect = [
            _response("result=7"),
            _response(),
            _response(_recording_response()),
            _response(),
        ]
        search = self.search()
        next(search)

        search.close()
        search.close()

        self.assertEqual(self.connection.get.call_count, 4)
        self.assertEqual(
            self.connection.get.call_args_list[-1],
            call(
                "/cgi-bin/mediaFileFind.cgi",
                params={"action": "close", "object": 7},
            ),
        )

    def test_close_before_iteration_does_not_contact_recorder(self) -> None:
        search = self.search()

        search.close()

        self.connection.get.assert_not_called()
        with self.assertRaises(StopIteration):
            next(search)
        self.connection.get.assert_not_called()

    def test_context_manager_closes_search_normally(self) -> None:
        self.connection.get.side_effect = [
            _response("result=7"),
            _response(),
            _response(_recording_response()),
            _response(),
        ]

        with self.search() as search:
            next(search)

        self.assertEqual(self.connection.get.call_count, 4)
        self.assertEqual(
            self.connection.get.call_args_list[-1][1]["params"]["action"],
            "close",
        )

    def test_context_manager_closes_search_when_body_raises(self) -> None:
        self.connection.get.side_effect = [
            _response("result=7"),
            _response(),
            _response(_recording_response()),
            _response(),
        ]

        with self.assertRaisesRegex(RuntimeError, "body failed"):
            with self.search() as search:
                next(search)
                raise RuntimeError("body failed")

        self.assertEqual(self.connection.get.call_count, 4)
        self.assertEqual(
            self.connection.get.call_args_list[-1][1]["params"]["action"],
            "close",
        )

    def test_iteration_error_closes_active_search(self) -> None:
        self.connection.get.side_effect = [
            _response("result=7"),
            _response(),
            _response("items[0].Channel=1"),
            _response(),
        ]
        search = self.search()

        with self.assertRaises(InvalidResponseError):
            next(search)

        self.assertEqual(self.connection.get.call_count, 4)
        self.assertEqual(
            self.connection.get.call_args_list[-1],
            call(
                "/cgi-bin/mediaFileFind.cgi",
                params={"action": "close", "object": 7},
            ),
        )


def _recording(*, file_path: str = "mnt/dvr/recording.dav") -> Recording:
    return Recording(
        channel=1,
        cluster=0,
        disk=0,
        partition=0,
        start_time=datetime(2026, 8, 6, 7, 14, 13),
        end_time=datetime(2026, 8, 6, 7, 15, 45),
        file_path=file_path,
        type="dav",
        video_stream="Main",
        events=[],
        flags=[],
        length=1,
        cut_length=1,
    )


def _response(text: str = "", *, status_code: int = 200) -> Mock:
    return Mock(text=text, status_code=status_code)


def _recording_response() -> str:
    return "\n".join(
        [
            "items[0].Channel=1",
            "items[0].Cluster=0",
            "items[0].CutLength=1",
            "items[0].Disk=0",
            "items[0].EndTime=2026-08-06 07:15:45",
            "items[0].FilePath=mnt/dvr/recording.dav",
            "items[0].Length=1",
            "items[0].Partition=0",
            "items[0].StartTime=2026-08-06 07:14:13",
            "items[0].Type=dav",
            "items[0].VideoStream=Main",
        ]
    )
