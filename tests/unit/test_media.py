from datetime import datetime
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import TestCase
from unittest.mock import Mock, call

from dahua_cgi.exceptions import InvalidResponseError
from dahua_cgi.media import MediaService
from dahua_cgi.models import Recording
from dahua_cgi.parsers.recording import parse_rpc_recordings


class MediaServiceDownloadTests(TestCase):
    def test_download_requests_exact_recording_file_and_writes_bytes(self) -> None:
        connection = Mock()
        connection.get.return_value = Mock(
            status_code=200,
            content=b"recording data",
            headers={"Content-Type": "application/http"},
        )
        with TemporaryDirectory() as directory:
            destination = Path(directory) / "recording.dav"
            result = MediaService(connection).download(
                _recording(file_path="/mnt/dvr/recording.dav"), destination
            )
            self.assertEqual(result, destination)
            self.assertEqual(destination.read_bytes(), b"recording data")
        connection.get.assert_called_once_with(
            "/cgi-bin/RPC_Loadfile/mnt/dvr/recording.dav"
        )

    def test_download_does_not_write_after_http_failure(self) -> None:
        connection = Mock()
        connection.get.return_value = Mock(status_code=404)
        with TemporaryDirectory() as directory:
            destination = Path(directory) / "recording.dav"
            with self.assertRaisesRegex(InvalidResponseError, "404"):
                MediaService(connection).download(_recording(), destination)
            self.assertFalse(destination.exists())

    def test_download_propagates_rpc_transport_sdk_error(self) -> None:
        connection = Mock()
        connection.get.side_effect = InvalidResponseError("download failed")
        with TemporaryDirectory() as directory:
            with self.assertRaisesRegex(InvalidResponseError, "download failed"):
                MediaService(connection).download(
                    _recording(), Path(directory) / "recording.dav"
                )


class MediaSearchLifecycleTests(TestCase):
    def setUp(self) -> None:
        self.connection = Mock()
        self.service = MediaService(self.connection)

    def search(self):
        return self.service.search(
            channel=1,
            start=datetime(2026, 8, 14, 5, 27, 10),
            end=datetime(2026, 8, 14, 5, 28, 25),
        )

    def test_search_is_lazy(self) -> None:
        self.search()
        self.connection.call.assert_not_called()

    def test_search_rejects_non_public_channel(self) -> None:
        with self.assertRaisesRegex(ValueError, "at least 1"):
            self.service.search(
                channel=0,
                start=datetime(2026, 8, 14),
                end=datetime(2026, 8, 15),
            )
        self.connection.call.assert_not_called()

    def test_iteration_uses_rpc_lifecycle_and_parses_multiple_infos(self) -> None:
        self.connection.call.side_effect = [
            {"result": 7},
            {"result": True},
            _page(_info(), _info(Channel=1, FilePath="/second.dav")),
            _page(),
            {"result": True},
            {"result": True},
        ]
        recordings = list(self.search())
        self.assertEqual([item.channel for item in recordings], [1, 2])
        self.assertEqual(recordings[0].events, ("VideoMotion",))
        self.assertEqual(recordings[0].flags, ("Event",))
        self.assertEqual(recordings[0].disk, 1)
        self.assertEqual(recordings[0].cluster, 108756)
        self.assertEqual(recordings[0].partition, 1)
        self.assertEqual(
            self.connection.call.call_args_list,
            [
                call("mediaFileFind.factory.create"),
                call(
                    "mediaFileFind.findFile",
                    {
                        "condition": {
                            "Channel": 0,
                            "Dirs": None,
                            "Types": ["dav"],
                            "Order": "Ascent",
                            "Redundant": "Exclusion",
                            "Events": None,
                            "StartTime": "2026-08-14 05:27:10",
                            "EndTime": "2026-08-14 05:28:25",
                            "Flags": ["Timing", "Event", "Manual"],
                        }
                    },
                    object_id=7,
                ),
                call(
                    "mediaFileFind.findNextFile", {"count": 100}, object_id=7
                ),
                call(
                    "mediaFileFind.findNextFile", {"count": 100}, object_id=7
                ),
                call("mediaFileFind.close", object_id=7),
                call("mediaFileFind.destroy", object_id=7),
            ],
        )

    def test_rpc_error_does_not_masquerade_as_exhaustion(self) -> None:
        self.connection.call.side_effect = [
            {"result": 7},
            {"result": True},
            InvalidResponseError("RPC rejected request"),
            {"result": True},
            {"result": True},
        ]
        with self.assertRaisesRegex(InvalidResponseError, "rejected"):
            next(self.search())
        self.assertEqual(
            self.connection.call.call_args_list[-2:],
            [
                call("mediaFileFind.close", object_id=7),
                call("mediaFileFind.destroy", object_id=7),
            ],
        )

    def test_parse_failure_cleans_up_without_masking_original_error(self) -> None:
        self.connection.call.side_effect = [
            {"result": 7},
            {"result": True},
            _page({"Channel": 0}),
            RuntimeError("close failed"),
            RuntimeError("destroy failed"),
        ]
        with self.assertRaisesRegex(InvalidResponseError, "recording 0"):
            next(self.search())

    def test_explicit_close_is_idempotent_and_uses_both_cleanup_calls(self) -> None:
        self.connection.call.side_effect = [
            {"result": 7},
            {"result": True},
            _page(_info()),
            {"result": True},
            {"result": True},
        ]
        search = self.search()
        next(search)
        search.close()
        search.close()
        self.assertEqual(
            self.connection.call.call_args_list[-2:],
            [
                call("mediaFileFind.close", object_id=7),
                call("mediaFileFind.destroy", object_id=7),
            ],
        )

    def test_close_before_iteration_does_not_contact_recorder(self) -> None:
        search = self.search()
        search.close()
        with self.assertRaises(StopIteration):
            next(search)
        self.connection.call.assert_not_called()

    def test_context_manager_cleans_up_after_body_failure(self) -> None:
        self.connection.call.side_effect = [
            {"result": 7},
            {"result": True},
            _page(_info()),
            {"result": True},
            {"result": True},
        ]
        with self.assertRaisesRegex(RuntimeError, "body failed"):
            with self.search() as search:
                next(search)
                raise RuntimeError("body failed")
        self.assertEqual(
            self.connection.call.call_args_list[-2:],
            [
                call("mediaFileFind.close", object_id=7),
                call("mediaFileFind.destroy", object_id=7),
            ],
        )


class RpcRecordingPageTests(TestCase):
    def test_live_empty_page_with_null_infos_is_exhaustion(self) -> None:
        self.assertEqual(
            parse_rpc_recordings(
                {
                    "id": 6,
                    "params": {"found": 0, "infos": None},
                    "result": True,
                    "session": "session-id",
                }
            ),
            [],
        )

    def test_positive_found_still_requires_infos_list(self) -> None:
        with self.assertRaisesRegex(InvalidResponseError, "malformed"):
            parse_rpc_recordings(
                {
                    "id": 5,
                    "params": {"found": 2, "infos": None},
                    "result": True,
                    "session": "session-id",
                }
            )


def _recording(file_path: str = "/mnt/dvr/recording.dav") -> Recording:
    return Recording(
        channel=1,
        cluster=108756,
        disk=1,
        partition=1,
        start_time=datetime(2026, 8, 14, 5, 27, 10),
        end_time=datetime(2026, 8, 14, 5, 28, 25),
        file_path=file_path,
        type="dav",
        video_stream="Main",
        events=("VideoMotion",),
        flags=("Event",),
        length=41943040,
        cut_length=0,
    )


def _page(*infos: dict) -> dict:
    return {
        "id": 5,
        "params": {"found": len(infos), "infos": list(infos) or None},
        "result": True,
        "session": "session-id",
    }


def _info(**changes) -> dict:
    value = {
        "Channel": 0,
        "Cluster": 108756,
        "Disk": 1,
        "StartTime": "2026-08-14 05:27:10",
        "EndTime": "2026-08-14 05:28:25",
        "Events": ["VideoMotion"],
        "FilePath": "/mnt/dvr/recording.dav",
        "Flags": ["Event"],
        "Length": 41943040,
        "Partition": 1,
        "Type": "dav",
        "VideoStream": "Main",
    }
    value.update(changes)
    return value
