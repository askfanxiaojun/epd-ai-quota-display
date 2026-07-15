import unittest
from datetime import datetime
import json
from pathlib import Path
import tempfile
from unittest.mock import AsyncMock, patch

from epd_status import (
    CALENDAR_FIELD_SEPARATOR,
    CALENDAR_RECORD_SEPARATOR,
    CalendarEvent,
    SensorReading,
    build_calendar_agenda_card,
    build_calendar_sensor_card,
    build_quota_card,
    calendar_agenda_display_state,
    load_display_state,
    fetch_sensor_reading,
    pack_monochrome,
    parse_calendar_output,
    parse_sensor_reading,
    quota_display_state,
    save_display_state,
    weekly_quota_window,
    write_card_with_retry,
)
from calendar_data import calendar_label, holiday_marker, solar_to_lunar


class SensorReadingTests(unittest.TestCase):
    def test_humidity_is_optional(self):
        reading = parse_sensor_reading(
            {"temperature": 23.75, "timestamp": "2026-07-13T10:20:00+08:00"},
            source="test",
            fallback_time=datetime.now().astimezone(),
        )
        self.assertEqual(reading.temperature, 23.75)
        self.assertIsNone(reading.humidity)

    def test_nested_sensor_keys(self):
        reading = parse_sensor_reading(
            {"state": {"temp": 25.2, "rh": 61}},
            source="test",
            fallback_time=datetime.now().astimezone(),
            temperature_key="state.temp",
            humidity_key="state.rh",
        )
        self.assertEqual(reading.temperature, 25.2)
        self.assertEqual(reading.humidity, 61)

    def test_stale_sensor_file_is_rejected(self):
        with tempfile.NamedTemporaryFile(mode="w", suffix=".json") as sensor_file:
            json.dump({"temperature": 24.0, "timestamp": "2020-01-01T00:00:00Z"}, sensor_file)
            sensor_file.flush()
            with self.assertRaisesRegex(RuntimeError, "stale"):
                fetch_sensor_reading(
                    temperature=None,
                    humidity=None,
                    sensor_file=sensor_file.name,
                    sensor_url=None,
                    sensor_token=None,
                    temperature_key=None,
                    humidity_key=None,
                    timestamp_key=None,
                    max_age_minutes=30,
                    demo=False,
                )


class CalendarSensorCardTests(unittest.TestCase):
    def test_card_renders_two_complete_planes(self):
        reading = SensorReading(
            temperature=24.6,
            humidity=58,
            measured_at=datetime.fromisoformat("2026-07-13T14:30:00+08:00"),
            source="test",
        )
        black, red, preview = build_calendar_sensor_card(
            400,
            300,
            reading,
            now=datetime.fromisoformat("2026-07-13T14:31:00+08:00"),
            location="书房",
        )
        self.assertEqual(preview.size, (400, 300))
        self.assertEqual(len(pack_monochrome(black)), 15_000)
        self.assertEqual(len(pack_monochrome(red)), 15_000)

    def test_card_renders_without_humidity(self):
        reading = SensorReading(
            temperature=22.0,
            humidity=None,
            measured_at=datetime.fromisoformat("2026-07-13T14:30:00+08:00"),
            source="test",
        )
        black, red, _ = build_calendar_sensor_card(
            400,
            300,
            reading,
            now=datetime.fromisoformat("2026-07-13T14:31:00+08:00"),
        )
        self.assertEqual(black.size, (400, 300))
        self.assertEqual(red.size, (400, 300))


class ExistingQuotaCardTests(unittest.TestCase):
    def test_existing_quota_layout_still_renders(self):
        windows = [
            {"label": "5 HOURS", "used": 1, "reset_at": 1_800_000_000},
            {"label": "7 DAYS", "used": 26, "reset_at": 1_800_086_400},
        ]
        black, red, preview = build_quota_card(400, 300, windows)
        self.assertEqual(preview.size, (400, 300))
        self.assertEqual(len(pack_monochrome(black)), 15_000)
        self.assertEqual(len(pack_monochrome(red)), 15_000)

    def test_state_changes_only_when_visible_quota_changes(self):
        first = quota_display_state([
            {"label": "5 HOURS", "used": 3.1, "reset_at": 1_800_000_000},
            {"label": "7 DAYS", "used": 26, "reset_at": 1_800_086_400},
        ])
        same_visible_values = quota_display_state([
            {"label": "5 HOURS", "used": 3.4, "reset_at": 1_800_000_000},
            {"label": "7 DAYS", "used": 26, "reset_at": 1_800_086_400},
        ])
        changed_percentage = quota_display_state([
            {"label": "5 HOURS", "used": 4.0, "reset_at": 1_800_000_000},
            {"label": "7 DAYS", "used": 26, "reset_at": 1_800_086_400},
        ])
        self.assertEqual(first, same_visible_values)
        self.assertNotEqual(first, changed_percentage)

    def test_display_state_round_trip(self):
        state = quota_display_state([
            {"label": "7 DAYS", "used": 26, "reset_at": 1_800_086_400},
        ])
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "state.json"
            save_display_state(path, state)
            self.assertEqual(load_display_state(path), state)


class CalendarAgendaTests(unittest.TestCase):
    def test_original_calendar_labels_are_preserved(self):
        lunar = solar_to_lunar(datetime.fromisoformat("2026-07-15T12:00:00+08:00"))
        self.assertEqual((lunar.month, lunar.day, lunar.cyclical_year, lunar.zodiac), (6, 2, "丙午", "马"))
        self.assertEqual(calendar_label(datetime(2026, 7, 1).date()), ("建党节", True))
        self.assertEqual(calendar_label(datetime(2026, 7, 7).date()), ("小暑", True))
        self.assertEqual(calendar_label(datetime(2026, 7, 23).date()), ("大暑", True))
        self.assertEqual(holiday_marker(datetime(2026, 10, 1).date()), "休")
        self.assertEqual(holiday_marker(datetime(2026, 10, 10).date()), "班")

    def test_calendar_output_parser(self):
        fs = CALENDAR_FIELD_SEPARATOR
        rs = CALENDAR_RECORD_SEPARATOR
        payload = fs.join((
            "工作", "项目评审", "2026", "7", "15", "17", "0",
            "2026", "7", "15", "18", "0", "false",
        )) + rs
        events = parse_calendar_output(payload, timezone=datetime.now().astimezone().tzinfo)
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0].title, "项目评审")
        self.assertEqual(events[0].start.hour, 17)
        self.assertFalse(events[0].all_day)

    def test_only_weekly_window_is_selected(self):
        windows = [
            {"label": "5 HOURS", "used": 99, "reset_at": 1_800_000_000},
            {"label": "7 DAYS", "used": 26, "reset_at": 1_800_086_400},
        ]
        self.assertEqual(weekly_quota_window(windows)["used"], 26)

    def test_calendar_agenda_card_renders_both_planes(self):
        weekly = {"label": "7 DAYS", "used": 26, "reset_at": 1_800_086_400}
        events = [
            CalendarEvent(
                title="项目评审",
                start=datetime.fromisoformat("2026-07-15T17:00:00+08:00"),
                end=datetime.fromisoformat("2026-07-15T18:00:00+08:00"),
                calendar_name="工作",
            )
        ]
        black, red, preview = build_calendar_agenda_card(
            400,
            300,
            weekly,
            events,
            now=datetime.fromisoformat("2026-07-15T14:31:00+08:00"),
        )
        self.assertEqual(preview.size, (400, 300))
        self.assertEqual(len(pack_monochrome(black)), 15_000)
        self.assertEqual(len(pack_monochrome(red)), 15_000)

    def test_display_state_changes_when_today_schedule_changes(self):
        weekly = {"label": "7 DAYS", "used": 26, "reset_at": 1_800_086_400}
        now = datetime.fromisoformat("2026-07-15T14:31:00+08:00")
        empty = calendar_agenda_display_state(weekly, [], now)
        event = CalendarEvent(
            title="项目评审",
            start=datetime.fromisoformat("2026-07-15T17:00:00+08:00"),
            end=datetime.fromisoformat("2026-07-15T18:00:00+08:00"),
            calendar_name="工作",
        )
        scheduled = calendar_agenda_display_state(weekly, [event], now)
        self.assertNotEqual(empty, scheduled)

    def test_six_row_month_with_four_events_stays_renderable(self):
        weekly = {"label": "7 DAYS", "used": 1, "reset_at": 1_800_086_400}
        events = [
            CalendarEvent(
                title=f"较长的日程标题 {index + 1}",
                start=datetime.fromisoformat(f"2026-08-15T{9 + index:02d}:00:00+08:00"),
                end=datetime.fromisoformat(f"2026-08-15T{10 + index:02d}:00:00+08:00"),
                calendar_name="工作",
            )
            for index in range(4)
        ]
        black, red, preview = build_calendar_agenda_card(
            400,
            300,
            weekly,
            events,
            now=datetime.fromisoformat("2026-08-15T14:31:00+08:00"),
        )
        self.assertEqual(preview.size, (400, 300))
        self.assertEqual(len(pack_monochrome(black)), 15_000)
        self.assertEqual(len(pack_monochrome(red)), 15_000)


class BluetoothRetryTests(unittest.IsolatedAsyncioTestCase):
    async def test_complete_frame_is_retried_without_preclear(self):
        with (
            patch("epd_status.find_device", new_callable=AsyncMock, side_effect=("first", "second")) as find,
            patch(
                "epd_status.write_card",
                new_callable=AsyncMock,
                side_effect=(RuntimeError("disconnected"), None),
            ) as write,
        ):
            await write_card_with_retry(
                "NRF_EPD",
                b"black",
                b"red",
                retry_delay=0,
            )

        self.assertEqual(find.await_count, 2)
        self.assertEqual(write.await_count, 2)
        self.assertTrue(all(call.kwargs["clear_first"] is False for call in write.await_args_list))


if __name__ == "__main__":
    unittest.main()
