from datetime import datetime
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import TestCase
from unittest.mock import Mock

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
