from datetime import UTC, datetime
from unittest import TestCase
from zoneinfo import ZoneInfo

from dahua_rpc.exceptions import InvalidResponseError
from dahua_rpc.recorder_time import (
    parse_recorder_current_time,
    parse_recorder_timezone,
    recorder_search_time,
)


class RecorderTimeTests(TestCase):
    def test_timezone_discovery_uses_recorder_iana_description(self) -> None:
        timezone = parse_recorder_timezone(
            {
                "params": {
                    "table": {
                        "TimeZone": 28,
                        "TimeZoneDesc": "America/Los_Angeles",
                    }
                }
            }
        )
        self.assertIsInstance(timezone, ZoneInfo)
        self.assertEqual(timezone.key, "America/Los_Angeles")

    def test_malformed_or_unknown_timezone_is_rejected(self) -> None:
        for description in (None, "", "Not/A_Timezone"):
            with self.subTest(description=description):
                with self.assertRaises(InvalidResponseError):
                    parse_recorder_timezone(
                        {"params": {"table": {"TimeZoneDesc": description}}}
                    )

    def test_current_time_is_aware(self) -> None:
        timezone = ZoneInfo("America/Los_Angeles")
        value = parse_recorder_current_time(
            {"params": {"time": "2026-08-16 13:04:01"}}, timezone
        )
        self.assertEqual(value.tzinfo, timezone)
        self.assertEqual(value.isoformat(), "2026-08-16T13:04:01-07:00")

    def test_aware_utc_search_bound_converts_across_dst(self) -> None:
        timezone = ZoneInfo("America/Los_Angeles")
        self.assertEqual(
            recorder_search_time(
                datetime(2026, 3, 8, 10, 1, tzinfo=UTC), timezone
            ),
            "2026-03-08 03:01:00",
        )

    def test_naive_search_bound_is_rejected(self) -> None:
        with self.assertRaisesRegex(ValueError, "timezone-aware"):
            recorder_search_time(
                datetime(2026, 8, 14), ZoneInfo("America/Los_Angeles")
            )

    def test_ambiguous_fall_back_search_bound_is_rejected(self) -> None:
        timezone = ZoneInfo("America/Los_Angeles")
        value = datetime(2026, 11, 1, 1, 30, tzinfo=timezone, fold=1)
        with self.assertRaisesRegex(ValueError, "ambiguous"):
            recorder_search_time(value, timezone)
