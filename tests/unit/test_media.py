from dataclasses import FrozenInstanceError
from datetime import datetime
from unittest import TestCase
from unittest.mock import Mock, call

from dahua_rpc.exceptions import InvalidResponseError
from dahua_rpc.media import MediaService
from dahua_rpc.models import Recording, Snapshot
from dahua_rpc.parsers.recording import parse_rpc_recordings
from dahua_rpc.parsers.snapshot import parse_rpc_snapshots


class RecordingBytesTests(TestCase):
    def test_recording_bytes_requests_exact_file_and_returns_bytes(self) -> None:
        connection = Mock()
        payload = b"DHII recording data"
        connection.get.return_value = Mock(
            status_code=200,
            content=payload,
            headers={"Content-Type": "application/http"},
        )
        result = MediaService(connection).recording_bytes(
            _recording(file_path="/mnt/dvr/recording.dav")
        )
        self.assertIs(result, payload)
        self.assertIsInstance(result, bytes)
        connection.get.assert_called_once_with(
            "/cgi-bin/RPC_Loadfile/mnt/dvr/recording.dav"
        )

    def test_recording_bytes_rejects_http_failure(self) -> None:
        connection = Mock()
        connection.get.return_value = Mock(status_code=404)
        with self.assertRaisesRegex(InvalidResponseError, "404"):
            MediaService(connection).recording_bytes(_recording())

    def test_recording_bytes_propagates_rpc_transport_sdk_error(self) -> None:
        connection = Mock()
        connection.get.side_effect = InvalidResponseError("download failed")
        with self.assertRaisesRegex(InvalidResponseError, "download failed"):
            MediaService(connection).recording_bytes(_recording())

    def test_obsolete_generic_methods_are_not_exposed(self) -> None:
        service = MediaService(Mock())
        self.assertFalse(hasattr(service, "search"))
        self.assertFalse(hasattr(service, "download"))


class StoredSnapshotTests(TestCase):
    def setUp(self) -> None:
        self.connection = Mock()
        self.service = MediaService(self.connection)

    def snapshots(self):
        return self.service.snapshots(
            channel=1,
            start=datetime(2026, 8, 14, 5, 23),
            end=datetime(2026, 8, 14, 5, 23, 12),
        )

    def test_snapshot_model_is_immutable(self) -> None:
        snapshot = _snapshot()
        with self.assertRaises(FrozenInstanceError):
            snapshot.length = 1

    def test_search_uses_jpg_type_and_parses_live_shape(self) -> None:
        self.connection.call.side_effect = [
            {"result": 9},
            {"result": True},
            _page(_snapshot_info()),
            _page(),
            {"result": True},
            {"result": True},
        ]
        snapshots = list(self.snapshots())
        self.assertEqual(snapshots, [_snapshot()])
        condition = self.connection.call.call_args_list[1].args[1]["condition"]
        self.assertEqual(condition["Channel"], 0)
        self.assertEqual(condition["Types"], ["jpg"])
        self.assertEqual(
            self.connection.call.call_args_list[-2:],
            [
                call("mediaFileFind.close", object_id=9),
                call("mediaFileFind.destroy", object_id=9),
            ],
        )

    def test_missing_optional_video_stream_is_accepted(self) -> None:
        info = _snapshot_info()
        del info["VideoStream"]
        self.assertIsNone(parse_rpc_snapshots(_page(info))[0].video_stream)

    def test_missing_required_field_is_rejected(self) -> None:
        info = _snapshot_info()
        del info["Cluster"]
        with self.assertRaisesRegex(InvalidResponseError, "snapshot 0"):
            parse_rpc_snapshots(_page(info))

    def test_non_jpg_record_is_rejected(self) -> None:
        with self.assertRaisesRegex(InvalidResponseError, "snapshot 0"):
            parse_rpc_snapshots(_page(_snapshot_info(Type="dav")))

    def test_empty_terminal_page_is_exhaustion(self) -> None:
        self.assertEqual(parse_rpc_snapshots(_page()), [])

    def test_parse_error_cleans_up(self) -> None:
        self.connection.call.side_effect = [
            {"result": 9},
            {"result": True},
            _page({"Channel": 0}),
            {"result": True},
            {"result": True},
        ]
        with self.assertRaisesRegex(InvalidResponseError, "snapshot 0"):
            next(self.snapshots())
        self.assertEqual(
            self.connection.call.call_args_list[-2:],
            [
                call("mediaFileFind.close", object_id=9),
                call("mediaFileFind.destroy", object_id=9),
            ],
        )

    def test_snapshot_bytes_tolerates_bogus_length_and_validates_jpeg(self) -> None:
        response = Mock(status_code=200)
        response.raw.stream.return_value = [b"\xff\xd8jpeg", b" data\xff\xd9"]
        self.connection.get.return_value = response
        self.assertEqual(
            self.service.snapshot_bytes(_snapshot()), b"\xff\xd8jpeg data\xff\xd9"
        )
        self.connection.get.assert_called_once_with(
            "/cgi-bin/RPC_Loadfile/mnt/dvr/snapshot.jpg", stream=True
        )
        self.assertFalse(response.raw.enforce_content_length)
        response.raw.stream.assert_called_once_with(8192, decode_content=False)
        response.close.assert_called_once_with()

    def test_snapshot_bytes_rejects_invalid_or_truncated_jpeg(self) -> None:
        for payload in (b"not a jpeg\xff\xd9", b"\xff\xd8truncated"):
            with self.subTest(payload=payload):
                response = Mock(status_code=200)
                response.raw.stream.return_value = [payload]
                self.connection.get.return_value = response
                with self.assertRaisesRegex(InvalidResponseError, "malformed"):
                    self.service.snapshot_bytes(_snapshot())
                response.close.assert_called_once_with()


class MediaSearchLifecycleTests(TestCase):
    def setUp(self) -> None:
        self.connection = Mock()
        self.service = MediaService(self.connection)

    def recordings(self):
        return self.service.recordings(
            channel=1,
            start=datetime(2026, 8, 14, 5, 27, 10),
            end=datetime(2026, 8, 14, 5, 28, 25),
        )

    def test_search_is_lazy(self) -> None:
        self.recordings()
        self.connection.call.assert_not_called()

    def test_search_rejects_non_public_channel(self) -> None:
        with self.assertRaisesRegex(ValueError, "at least 1"):
            self.service.recordings(
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
        recordings = list(self.recordings())
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
            next(self.recordings())
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
            next(self.recordings())

    def test_explicit_close_is_idempotent_and_uses_both_cleanup_calls(self) -> None:
        self.connection.call.side_effect = [
            {"result": 7},
            {"result": True},
            _page(_info()),
            {"result": True},
            {"result": True},
        ]
        search = self.recordings()
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
        search = self.recordings()
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
            with self.recordings() as search:
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


def _snapshot() -> Snapshot:
    return Snapshot(
        channel=1,
        start=datetime(2026, 8, 14, 5, 23, 6),
        end=datetime(2026, 8, 14, 5, 23, 6),
        file_path="/mnt/dvr/snapshot.jpg",
        length=28672,
        disk=1,
        cluster=108687,
        partition=1,
        video_stream="Main",
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


def _snapshot_info(**changes) -> dict:
    value = {
        "Channel": 0,
        "Cluster": 108687,
        "Disk": 1,
        "StartTime": "2026-08-14 05:23:06",
        "EndTime": "2026-08-14 05:23:06",
        "FilePath": "/mnt/dvr/snapshot.jpg",
        "Length": 28672,
        "Partition": 1,
        "Type": "jpg",
        "VideoStream": "Main",
    }
    value.update(changes)
    return value
