#!/usr/bin/env python3
"""Render live AI quota status and send it to an EPD-nRF5 device over BLE."""

import argparse
import asyncio
import calendar
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
import json
import math
import os
from pathlib import Path
import sqlite3
import subprocess
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from PIL import Image, ImageDraw, ImageFont, ImageOps

from calendar_data import calendar_label, holiday_marker, solar_to_lunar

SERVICE_UUID = "62750001-d828-918d-fb46-b6c11c675aec"
CHARACTERISTIC_UUID = "62750002-d828-918d-fb46-b6c11c675aec"
VERSION_UUID = "62750003-d828-918d-fb46-b6c11c675aec"

CMD_INIT = 0x01
CMD_CLEAR = 0x02
CMD_WRITE_IMAGE = 0x30
CMD_REFRESH = 0x05
CMD_SET_TIME = 0x20
CODEX_USAGE_URL = "https://chatgpt.com/backend-api/wham/usage"
DISPLAY_STATE_VERSION = 1


@dataclass(frozen=True)
class SensorReading:
    temperature: float
    humidity: float | None
    measured_at: datetime
    source: str


@dataclass(frozen=True)
class CalendarEvent:
    title: str
    start: datetime
    end: datetime
    calendar_name: str
    all_day: bool = False


def font(size: int):
    for candidate in (
        "/System/Library/Fonts/PingFang.ttc",
        "/System/Library/Fonts/Supplemental/Arial Unicode.ttf",
    ):
        if Path(candidate).exists():
            return ImageFont.truetype(candidate, size, index=0)
    return ImageFont.load_default()


def window_label(seconds: int | None) -> str:
    return {
        18_000: "5 HOURS",
        604_800: "7 DAYS",
        2_592_000: "30 DAYS",
    }.get(seconds, "WINDOW")


def format_reset(timestamp: int | None) -> str:
    if not timestamp:
        return "reset time unavailable"
    return "resets " + datetime.fromtimestamp(timestamp).astimezone().strftime("%m-%d %H:%M")


def fetch_codex_quota() -> list[dict]:
    """Read local Codex OAuth credentials and request the current usage windows.

    This follows cc-switch's Codex subscription query path. The access token is
    used only as an HTTPS Authorization header and is never printed or stored.
    """
    auth_path = Path.home() / ".codex" / "auth.json"
    if not auth_path.exists():
        raise RuntimeError("Codex auth file was not found. Sign in to Codex first.")

    try:
        auth = json.loads(auth_path.read_text())
    except (OSError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"Cannot read Codex auth file: {exc}") from exc

    if auth.get("auth_mode") != "chatgpt":
        raise RuntimeError("Codex is not using ChatGPT OAuth; API-key mode has no subscription quota here.")
    tokens = auth.get("tokens") or {}
    access_token = tokens.get("access_token")
    if not access_token:
        raise RuntimeError("Codex OAuth access token is missing. Sign in to Codex again.")

    headers = {
        "Authorization": f"Bearer {access_token}",
        "User-Agent": "codex-cli",
        "Accept": "application/json",
    }
    if account_id := tokens.get("account_id"):
        headers["ChatGPT-Account-Id"] = account_id

    request = Request(CODEX_USAGE_URL, headers=headers)
    try:
        with urlopen(request, timeout=15) as response:
            payload = json.loads(response.read())
    except HTTPError as exc:
        if exc.code in (401, 403):
            raise RuntimeError("Codex login has expired. Sign in again before refreshing the display.") from exc
        raise RuntimeError(f"Codex usage request failed (HTTP {exc.code}).") from exc
    except (URLError, TimeoutError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"Codex usage request failed: {exc}") from exc

    rate_limit = payload.get("rate_limit") or {}
    windows = []
    for key in ("primary_window", "secondary_window"):
        window = rate_limit.get(key) or {}
        used = window.get("used_percent")
        if isinstance(used, (int, float)):
            windows.append({
                "label": window_label(window.get("limit_window_seconds")),
                "used": max(0.0, min(100.0, float(used))),
                "reset_at": window.get("reset_at"),
            })
    if not windows:
        raise RuntimeError("Codex returned no usage windows for this account.")
    return windows


def weekly_quota_window(windows: list[dict]) -> dict:
    for window in windows:
        if window.get("label") == "7 DAYS":
            return window
    raise RuntimeError("Codex returned no 7-day usage window for this account.")


CALENDAR_FIELD_SEPARATOR = "\x1f"
CALENDAR_RECORD_SEPARATOR = "\x1e"
CALENDAR_APPLESCRIPT = r'''
set fieldSep to character id 31
set recordSep to character id 30
set dayStart to current date
set time of dayStart to 0
set dayEnd to dayStart + (1 * days)
set outputText to ""
tell application "Calendar"
    repeat with cal in calendars
        set todaysEvents to (every event of cal whose start date ≥ dayStart and start date < dayEnd)
        repeat with ev in todaysEvents
            set startValue to start date of ev
            set endValue to end date of ev
            set outputText to outputText & (name of cal) & fieldSep & (summary of ev) & fieldSep & ¬
                (year of startValue as integer) & fieldSep & (month of startValue as integer) & fieldSep & ¬
                (day of startValue as integer) & fieldSep & (hours of startValue as integer) & fieldSep & ¬
                (minutes of startValue as integer) & fieldSep & (year of endValue as integer) & fieldSep & ¬
                (month of endValue as integer) & fieldSep & (day of endValue as integer) & fieldSep & ¬
                (hours of endValue as integer) & fieldSep & (minutes of endValue as integer) & fieldSep & ¬
                (allday event of ev as string) & recordSep
        end repeat
    end repeat
end tell
return outputText
'''


def parse_calendar_output(payload: str, *, timezone=None) -> list[CalendarEvent]:
    timezone = timezone or datetime.now().astimezone().tzinfo
    events = []
    for record in payload.split(CALENDAR_RECORD_SEPARATOR):
        if not record.strip():
            continue
        fields = record.split(CALENDAR_FIELD_SEPARATOR)
        if len(fields) != 13:
            raise RuntimeError("Calendar returned an event in an unexpected format.")
        try:
            start = datetime(*(int(value) for value in fields[2:7]), tzinfo=timezone)
            end = datetime(*(int(value) for value in fields[7:12]), tzinfo=timezone)
        except ValueError as exc:
            raise RuntimeError("Calendar returned an invalid event date.") from exc
        events.append(CalendarEvent(
            title=" ".join(fields[1].split()),
            start=start,
            end=end,
            calendar_name=fields[0],
            all_day=fields[12].strip().lower() == "true",
        ))
    return events


def fetch_today_calendar_events(
    *,
    calendar_names: list[str] | None = None,
    max_events: int = 4,
) -> list[CalendarEvent]:
    database_path = Path.home() / "Library/Group Containers/group.com.apple.calendar/Calendar.sqlitedb"
    if database_path.exists() and not os.environ.get("XPC_SERVICE_NAME"):
        try:
            return fetch_today_calendar_events_from_database(
                database_path,
                calendar_names=calendar_names,
                max_events=max_events,
            )
        except (OSError, sqlite3.Error, RuntimeError) as exc:
            print(f"Direct Calendar database read failed; falling back to Calendar automation: {exc}")

    try:
        result = subprocess.run(
            ["/usr/bin/osascript", "-e", CALENDAR_APPLESCRIPT],
            capture_output=True,
            text=True,
            timeout=60,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise RuntimeError(f"Cannot read macOS Calendar: {exc}") from exc
    if result.returncode:
        message = result.stderr.strip() or result.stdout.strip() or f"exit {result.returncode}"
        raise RuntimeError(f"Cannot read macOS Calendar: {message}")

    events = parse_calendar_output(result.stdout)
    if calendar_names:
        allowed = set(calendar_names)
        events = [event for event in events if event.calendar_name in allowed]
    events.sort(key=lambda event: (not event.all_day, event.start, event.title))
    return events[:max(0, max_events)]


def fetch_today_calendar_events_from_database(
    database_path: Path,
    *,
    calendar_names: list[str] | None = None,
    max_events: int = 4,
    now: datetime | None = None,
) -> list[CalendarEvent]:
    now = (now or datetime.now().astimezone()).astimezone()
    day_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    day_end = day_start + timedelta(days=1)
    apple_epoch = datetime(2001, 1, 1, tzinfo=timezone.utc)
    start_value = (day_start.astimezone(timezone.utc) - apple_epoch).total_seconds()
    end_value = (day_end.astimezone(timezone.utc) - apple_epoch).total_seconds()
    query = """
        SELECT DISTINCT
            ci.summary,
            c.title,
            COALESCE(oc.occurrence_start_date, oc.occurrence_date),
            oc.occurrence_end_date,
            ci.all_day
        FROM OccurrenceCache AS oc
        JOIN CalendarItem AS ci ON ci.ROWID = oc.event_id
        JOIN Calendar AS c ON c.ROWID = oc.calendar_id
        WHERE oc.day >= ? AND oc.day < ?
          AND oc.next_reminder_date IS NULL
          AND COALESCE(ci.hidden, 0) = 0
        ORDER BY COALESCE(oc.occurrence_start_date, oc.occurrence_date), ci.summary
    """
    connection = sqlite3.connect(f"file:{database_path}?mode=ro", uri=True, timeout=5)
    try:
        rows = connection.execute(query, (start_value, end_value)).fetchall()
    finally:
        connection.close()

    allowed = set(calendar_names or [])
    events = []
    for title, calendar_name, start_seconds, end_seconds, all_day in rows:
        if allowed and calendar_name not in allowed:
            continue
        if start_seconds is None or end_seconds is None:
            continue
        start = (apple_epoch + timedelta(seconds=float(start_seconds))).astimezone()
        end = (apple_epoch + timedelta(seconds=float(end_seconds))).astimezone()
        events.append(CalendarEvent(
            title=" ".join((title or "未命名日程").split()),
            start=start,
            end=end,
            calendar_name=calendar_name or "Calendar",
            all_day=bool(all_day),
        ))
    events.sort(key=lambda event: (not event.all_day, event.start, event.title))
    return events[:max(0, max_events)]


def build_test_card(width: int, height: int) -> Image.Image:
    image = Image.new("1", (width, height), 1)
    draw = ImageDraw.Draw(image)
    title, body, small = font(28), font(38), font(18)

    draw.rectangle((0, 0, width - 1, height - 1), outline=0, width=3)
    draw.text((18, 16), "AI QUOTA STATUS", font=title, fill=0)
    draw.line((18, 58, width - 18, 58), fill=0, width=2)

    draw.text((22, 88), "BLE delivery test", font=body, fill=0)
    draw.text((22, 146), "Mac -> nRF52811 -> EPD", font=title, fill=0)

    draw.line((18, height - 56, width - 18, height - 56), fill=0, width=1)
    draw.text((20, height - 43), datetime.now().strftime("%Y-%m-%d %H:%M:%S"), font=small, fill=0)
    draw.text((width - 122, height - 43), "v0.2", font=small, fill=0)
    return image


def text_right(draw: ImageDraw.ImageDraw, x: int, y: int, text: str, text_font, fill=0):
    box = draw.textbbox((0, 0), text, font=text_font)
    draw.text((x - (box[2] - box[0]), y), text, font=text_font, fill=fill)


def text_center(draw: ImageDraw.ImageDraw, x: int, y: int, text: str, text_font, fill=0):
    box = draw.textbbox((0, 0), text, font=text_font)
    draw.text((x - (box[2] - box[0]) / 2, y), text, font=text_font, fill=fill)


def load_json_config(path: Path) -> dict:
    if not path.exists():
        return {}
    try:
        payload = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"Cannot read configuration {path}: {exc}") from exc
    if not isinstance(payload, dict):
        raise RuntimeError(f"Configuration {path} must contain a JSON object.")
    return payload


def nested_value(payload: dict, key: str | None):
    if not key:
        return None
    value = payload
    for part in key.split("."):
        if not isinstance(value, dict) or part not in value:
            return None
        value = value[part]
    return value


def first_value(payload: dict, configured_key: str | None, aliases: tuple[str, ...]):
    if configured_key:
        return nested_value(payload, configured_key)
    for key in aliases:
        value = nested_value(payload, key)
        if value is not None:
            return value
    return None


def parse_sensor_time(value, fallback: datetime) -> datetime:
    if value is None:
        return fallback.astimezone()
    if isinstance(value, (int, float)):
        timestamp = float(value)
        if timestamp > 10_000_000_000:
            timestamp /= 1000
        return datetime.fromtimestamp(timestamp).astimezone()
    if isinstance(value, str):
        try:
            return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone()
        except ValueError as exc:
            raise RuntimeError(f"Invalid sensor timestamp: {value}") from exc
    raise RuntimeError("Sensor timestamp must be an ISO 8601 string or Unix timestamp.")


def parse_sensor_reading(
    payload: dict,
    *,
    source: str,
    fallback_time: datetime,
    temperature_key: str | None = None,
    humidity_key: str | None = None,
    timestamp_key: str | None = None,
) -> SensorReading:
    temperature = first_value(payload, temperature_key, ("temperature", "temp", "temperature_c"))
    humidity = first_value(payload, humidity_key, ("humidity", "rh", "relative_humidity"))
    timestamp = first_value(payload, timestamp_key, ("timestamp", "measured_at", "updated_at"))

    try:
        temperature = float(temperature)
    except (TypeError, ValueError) as exc:
        raise RuntimeError("Sensor data does not contain a numeric temperature.") from exc
    if not math.isfinite(temperature) or not -50 <= temperature <= 80:
        raise RuntimeError(f"Sensor temperature is outside the supported range: {temperature}")

    if humidity is not None:
        try:
            humidity = float(humidity)
        except (TypeError, ValueError) as exc:
            raise RuntimeError("Sensor humidity must be numeric when provided.") from exc
        if not math.isfinite(humidity) or not 0 <= humidity <= 100:
            raise RuntimeError(f"Sensor humidity is outside 0-100%: {humidity}")

    return SensorReading(
        temperature=temperature,
        humidity=humidity,
        measured_at=parse_sensor_time(timestamp, fallback_time),
        source=source,
    )


def fetch_sensor_reading(
    *,
    temperature: float | None,
    humidity: float | None,
    sensor_file: str | None,
    sensor_url: str | None,
    sensor_token: str | None,
    temperature_key: str | None,
    humidity_key: str | None,
    timestamp_key: str | None,
    max_age_minutes: float,
    demo: bool,
) -> SensorReading:
    now = datetime.now().astimezone()
    if temperature is not None:
        payload = {"temperature": temperature, "humidity": humidity}
        reading = parse_sensor_reading(payload, source="command line", fallback_time=now)
    elif sensor_file:
        path = Path(sensor_file).expanduser()
        try:
            payload = json.loads(path.read_text())
            fallback = datetime.fromtimestamp(path.stat().st_mtime).astimezone()
        except (OSError, json.JSONDecodeError) as exc:
            raise RuntimeError(f"Cannot read sensor file {path}: {exc}") from exc
        if not isinstance(payload, dict):
            raise RuntimeError(f"Sensor file {path} must contain a JSON object.")
        reading = parse_sensor_reading(
            payload,
            source=str(path),
            fallback_time=fallback,
            temperature_key=temperature_key,
            humidity_key=humidity_key,
            timestamp_key=timestamp_key,
        )
    elif sensor_url:
        headers = {"Accept": "application/json", "User-Agent": "epd-ai-quota-display"}
        if sensor_token:
            headers["Authorization"] = f"Bearer {sensor_token}"
        try:
            with urlopen(Request(sensor_url, headers=headers), timeout=10) as response:
                payload = json.loads(response.read())
        except (HTTPError, URLError, TimeoutError, json.JSONDecodeError) as exc:
            raise RuntimeError(f"Sensor request failed: {exc}") from exc
        if not isinstance(payload, dict):
            raise RuntimeError("Sensor endpoint must return a JSON object.")
        reading = parse_sensor_reading(
            payload,
            source=sensor_url,
            fallback_time=now,
            temperature_key=temperature_key,
            humidity_key=humidity_key,
            timestamp_key=timestamp_key,
        )
    elif demo:
        reading = SensorReading(temperature=24.6, humidity=58, measured_at=now, source="demo")
    else:
        raise RuntimeError(
            "Calendar sensor mode needs a real sensor source. Configure sensor.file or sensor.url, "
            "or pass --temperature for a one-off test."
        )

    age_seconds = (now - reading.measured_at).total_seconds()
    if max_age_minutes > 0 and age_seconds > max_age_minutes * 60:
        raise RuntimeError(
            f"Sensor reading is stale ({age_seconds / 60:.0f} minutes old; limit is {max_age_minutes:g})."
        )
    return reading


def draw_dashed_box(draw: ImageDraw.ImageDraw, box: tuple[int, int, int, int], fill=0, dash=4):
    x1, y1, x2, y2 = box
    for x in range(x1, x2, dash * 2):
        draw.line((x, y1, min(x + dash, x2), y1), fill=fill)
        draw.line((x, y2, min(x + dash, x2), y2), fill=fill)
    for y in range(y1, y2, dash * 2):
        draw.line((x1, y, x1, min(y + dash, y2)), fill=fill)
        draw.line((x2, y, x2, min(y + dash, y2)), fill=fill)


def reset_label(window: dict) -> str:
    timestamp = window.get("reset_at")
    if not timestamp:
        return "reset unavailable"
    reset = datetime.fromtimestamp(timestamp).astimezone()
    if window.get("label") == "5 HOURS":
        return "resets " + reset.strftime("%H:%M")
    return "resets " + reset.strftime("%m-%d %H:%M")


def quota_display_state(windows: list[dict]) -> dict:
    """Return only values that can visibly change the quota card."""
    by_label = {window["label"]: window for window in windows}
    visible_windows = []
    for label in ("5 HOURS", "7 DAYS"):
        window = by_label.get(label)
        if window is None:
            visible_windows.append(None)
            continue
        remaining = max(0.0, min(100.0, 100.0 - window["used"]))
        visible_windows.append({
            "label": label,
            "remaining": f"{remaining:.0f}",
            "reset": reset_label(window),
        })
    return {
        "version": DISPLAY_STATE_VERSION,
        "mode": "quota",
        "windows": visible_windows,
    }


def calendar_sensor_display_state(reading: SensorReading, location: str, now: datetime) -> dict:
    return {
        "version": DISPLAY_STATE_VERSION,
        "mode": "calendar-sensor",
        "date": now.astimezone().strftime("%Y-%m-%d"),
        "temperature": f"{reading.temperature:.1f}",
        "humidity": None if reading.humidity is None else f"{reading.humidity:.0f}",
        "measured_at": reading.measured_at.astimezone().strftime("%Y-%m-%d %H:%M"),
        "location": location,
    }


def calendar_agenda_display_state(
    weekly_window: dict,
    events: list[CalendarEvent],
    now: datetime,
) -> dict:
    remaining = max(0.0, min(100.0, 100.0 - weekly_window["used"]))
    return {
        "version": DISPLAY_STATE_VERSION,
        "mode": "calendar-agenda",
        "date": now.astimezone().strftime("%Y-%m-%d"),
        "weekly_remaining": f"{remaining:.0f}",
        "weekly_reset": reset_label(weekly_window),
        "events": [
            {
                "title": event.title,
                "start": "全天" if event.all_day else event.start.astimezone().strftime("%H:%M"),
                "end": event.end.astimezone().strftime("%H:%M"),
                "calendar": event.calendar_name,
            }
            for event in events
        ],
    }


def load_display_state(path: Path) -> dict | None:
    if not path.exists():
        return None
    try:
        state = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError) as exc:
        print(f"Ignoring unreadable display state {path}: {exc}")
        return None
    return state if isinstance(state, dict) else None


def save_display_state(path: Path, state: dict):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(json.dumps(state, ensure_ascii=False, indent=2) + "\n")
    os.replace(temporary, path)


def build_quota_card(width: int, height: int, windows: list[dict]) -> tuple[Image.Image, Image.Image, Image.Image]:
    if (width, height) != (400, 300):
        raise ValueError("The approved quota layout currently targets the 400x300 panel.")

    black = Image.new("1", (width, height), 1)
    red = Image.new("1", (width, height), 1)
    black_draw = ImageDraw.Draw(black)
    red_draw = ImageDraw.Draw(red)

    panel_font = font(11)
    provider_font = font(14)
    label_font = font(11)
    number_font = font(33)
    percent_font = font(13)
    meta_font = font(10)

    black_draw.text((18, 14), "AI QUOTA PANEL", font=panel_font, fill=0)
    red_draw.ellipse((339, 17, 345, 23), fill=0)
    red_draw.text((350, 14), "LIVE", font=label_font, fill=0)
    black_draw.line((18, 42, 382, 42), fill=0, width=2)

    by_label = {window["label"]: window for window in windows}
    # Do not duplicate the only returned window into both columns. The usage
    # endpoint can temporarily omit one rate-limit window around a reset.
    codex_windows = [by_label.get("5 HOURS"), by_label.get("7 DAYS")]

    def provider_section(name: str, top: int, active_windows: list[dict] | None):
        title_draw = black_draw if active_windows is not None else red_draw
        title_draw.text((18, top), name, font=provider_font, fill=0)
        status = "CONNECTED" if active_windows is not None else "NOT CONNECTED"
        text_right(black_draw, 382, top + 2, status, meta_font)

        columns = ((18, 194), (206, 382))
        for index, (x1, x2) in enumerate(columns):
            label = "5 HOURS" if index == 0 else "7 DAYS"
            black_draw.text((x1, top + 22), label, font=label_font, fill=0)
            if active_windows is not None:
                window = active_windows[index]
                bar_top = top + 65
                if window is not None:
                    remaining = max(0.0, min(100.0, 100.0 - window["used"]))
                    value = f"{remaining:.0f}"
                    # PingFang's visible digits start well below Pillow's supplied
                    # Y coordinate. Keep the digit box above the progress bar even
                    # for a wide value such as 99%.
                    text_right(black_draw, x2 - 13, top + 25, value, number_font)
                    black_draw.text((x2 - 12, top + 47), "%", font=percent_font, fill=0)
                    black_draw.rectangle((x1, bar_top, x2, bar_top + 7), outline=0)
                    fill_width = round((x2 - x1 - 2) * remaining / 100)
                    if fill_width > 0:
                        black_draw.rectangle((x1 + 1, bar_top + 1, x1 + fill_width, bar_top + 6), fill=0)
                    black_draw.text((x1, top + 74), reset_label(window), font=meta_font, fill=0)
                else:
                    text_right(black_draw, x2, top + 25, "—", number_font)
                    draw_dashed_box(black_draw, (x1, bar_top, x2, bar_top + 7), fill=0)
                    black_draw.text((x1, top + 74), "unavailable", font=meta_font, fill=0)
            else:
                text_right(red_draw, x2, top + 25, "—", number_font)
                draw_dashed_box(red_draw, (x1, top + 65, x2, top + 72), fill=0)
                black_draw.text((x1, top + 74), "awaiting account", font=meta_font, fill=0)

    provider_section("CODEX", 51, codex_windows)
    black_draw.line((18, 146, 382, 146), fill=0)
    provider_section("CLAUDE CODE", 154, None)

    black_draw.line((18, 267, 382, 267), fill=0)
    black_draw.text((18, 274), datetime.now().strftime("UPDATED %Y-%m-%d %H:%M"), font=meta_font, fill=0)
    text_right(black_draw, 382, 274, "AI QUOTA", meta_font)

    preview = Image.new("RGB", (width, height), (251, 250, 246))
    preview.paste((23, 21, 19), mask=ImageOps.invert(black.convert("L")))
    preview.paste((188, 46, 46), mask=ImageOps.invert(red.convert("L")))
    return black, red, preview


def build_calendar_sensor_card(
    width: int,
    height: int,
    reading: SensorReading,
    *,
    now: datetime | None = None,
    location: str = "室内",
) -> tuple[Image.Image, Image.Image, Image.Image]:
    if (width, height) != (400, 300):
        raise ValueError("The calendar sensor layout currently targets the 400x300 panel.")

    now = (now or datetime.now().astimezone()).astimezone()
    black = Image.new("1", (width, height), 1)
    red = Image.new("1", (width, height), 1)
    black_draw = ImageDraw.Draw(black)
    red_draw = ImageDraw.Draw(red)

    month_font = font(21)
    weekday_font = font(12)
    day_font = font(15)
    eyebrow_font = font(10)
    section_font = font(14)
    temperature_font = font(47)
    unit_font = font(16)
    humidity_font = font(31)
    meta_font = font(10)

    calendar_left = 14
    calendar_right = 238
    divider_x = 248
    black_draw.text((calendar_left, 10), f"{now.year}年 {now.month}月", font=month_font, fill=0)
    black_draw.text((calendar_left, 39), now.strftime("%Y / %m"), font=eyebrow_font, fill=0)
    black_draw.line((calendar_left, 57, calendar_right, 57), fill=0, width=2)

    cell_width = 32
    weekday_y = 63
    for index, label in enumerate(("一", "二", "三", "四", "五", "六", "日")):
        center_x = calendar_left + index * cell_width + cell_width // 2
        target = red_draw if index == 6 else black_draw
        text_center(target, center_x, weekday_y, label, weekday_font)

    weeks = calendar.Calendar(firstweekday=0).monthdayscalendar(now.year, now.month)
    while len(weeks) < 6:
        weeks.append([0] * 7)
    grid_top = 86
    row_height = 31
    for row, week in enumerate(weeks):
        baseline_y = grid_top + row * row_height
        if row:
            black_draw.line((calendar_left, baseline_y - 5, calendar_right, baseline_y - 5), fill=0)
        for column, day in enumerate(week):
            if not day:
                continue
            center_x = calendar_left + column * cell_width + cell_width // 2
            if day == now.day:
                red_draw.ellipse((center_x - 12, baseline_y - 3, center_x + 12, baseline_y + 22), outline=0, width=2)
            target = red_draw if column == 6 else black_draw
            text_center(target, center_x, baseline_y, str(day), day_font)

    black_draw.line((divider_x, 10, divider_x, 286), fill=0, width=2)

    content_left = 260
    content_right = 388
    content_center = (content_left + content_right) // 2
    red_draw.ellipse((content_left, 15, content_left + 7, 22), fill=0)
    black_draw.text((content_left + 14, 10), "当前环境", font=section_font, fill=0)
    text_right(black_draw, content_right, 34, location, meta_font)
    black_draw.line((content_left, 55, content_right, 55), fill=0)

    temperature_text = f"{reading.temperature:.1f}"
    temperature_box = black_draw.textbbox((0, 0), temperature_text, font=temperature_font)
    unit_box = black_draw.textbbox((0, 0), "°C", font=unit_font)
    total_width = temperature_box[2] - temperature_box[0] + unit_box[2] - unit_box[0] + 3
    start_x = content_center - total_width / 2
    black_draw.text((start_x, 68), temperature_text, font=temperature_font, fill=0)
    black_draw.text((start_x + temperature_box[2] - temperature_box[0] + 3, 96), "°C", font=unit_font, fill=0)
    text_center(black_draw, content_center, 132, "实时温度", meta_font)

    if reading.humidity is not None:
        black_draw.line((content_left, 158, content_right, 158), fill=0)
        black_draw.text((content_left, 168), "湿度", font=section_font, fill=0)
        humidity_text = f"{reading.humidity:.0f}%"
        text_right(red_draw, content_right, 184, humidity_text, humidity_font)

    black_draw.line((content_left, 249, content_right, 249), fill=0)
    black_draw.text((content_left, 258), "实测时间", font=meta_font, fill=0)
    text_right(black_draw, content_right, 258, reading.measured_at.strftime("%H:%M"), meta_font)
    text_right(black_draw, content_right, 274, now.strftime("%m-%d 更新"), meta_font)

    preview = Image.new("RGB", (width, height), (251, 250, 246))
    preview.paste((23, 21, 19), mask=ImageOps.invert(black.convert("L")))
    preview.paste((188, 46, 46), mask=ImageOps.invert(red.convert("L")))
    return black, red, preview


def fit_text(draw: ImageDraw.ImageDraw, text: str, text_font, max_width: int) -> str:
    if draw.textlength(text, font=text_font) <= max_width:
        return text
    ellipsis = "…"
    fitted = text
    while fitted and draw.textlength(fitted + ellipsis, font=text_font) > max_width:
        fitted = fitted[:-1]
    return fitted + ellipsis if fitted else ellipsis


def build_calendar_agenda_card(
    width: int,
    height: int,
    weekly_window: dict,
    events: list[CalendarEvent],
    *,
    now: datetime | None = None,
) -> tuple[Image.Image, Image.Image, Image.Image]:
    if (width, height) != (400, 300):
        raise ValueError("The calendar/agenda layout currently targets the 400x300 panel.")

    now = (now or datetime.now().astimezone()).astimezone()
    today = now.date()
    lunar_today = solar_to_lunar(today)
    black = Image.new("1", (width, height), 1)
    red = Image.new("1", (width, height), 1)
    black_draw = ImageDraw.Draw(black)
    red_draw = ImageDraw.Draw(red)

    month_font = font(25)
    unit_font = font(12)
    header_font = font(14)
    header_meta_font = font(11)
    marker_font = font(8)
    weekday_font = font(12)
    section_font = font(16)
    quota_label_font = font(12)
    quota_value_font = font(38)
    percent_font = font(15)
    reset_font = font(11)
    event_time_font = font(11)
    event_font = font(12)
    footer_font = font(10)

    def text_width(draw: ImageDraw.ImageDraw, value: str, value_font) -> int:
        box = draw.textbbox((0, 0), value, font=value_font)
        return box[2] - box[0]

    cursor_x = 11
    red_draw.text((cursor_x, 6), str(now.year), font=month_font, fill=0)
    cursor_x += text_width(red_draw, str(now.year), month_font) + 2
    black_draw.text((cursor_x, 19), "年", font=unit_font, fill=0)
    cursor_x += text_width(black_draw, "年", unit_font) + 5
    red_draw.text((cursor_x, 6), str(now.month), font=month_font, fill=0)
    cursor_x += text_width(red_draw, str(now.month), month_font) + 2
    black_draw.text((cursor_x, 19), "月", font=unit_font, fill=0)

    black_draw.line((176, 6, 176, 43), fill=0)
    black_draw.text((186, 5), f"{lunar_today.cyclical_year}年〔{lunar_today.zodiac}〕", font=header_font, fill=0)
    lunar_header = f"{lunar_today.month_label}{lunar_today.day_label} · 第 {now.isocalendar().week} 周"
    black_draw.text((186, 27), lunar_header, font=header_meta_font, fill=0)
    black_draw.line((11, 51, 389, 51), fill=0, width=2)

    calendar_left = 11
    calendar_right = 237
    divider_x = 247
    week_top = 59
    week_height = 24
    cell_width = (calendar_right - calendar_left + 1) // 7
    week_labels = ("一", "二", "三", "四", "五", "六", "日")
    for column, label in enumerate(week_labels):
        x1 = calendar_left + column * cell_width
        x2 = calendar_right if column == 6 else x1 + cell_width - 1
        target = red_draw if column >= 5 else black_draw
        target.rectangle((x1, week_top, x2, week_top + week_height), fill=0)
        box = target.textbbox((0, 0), label, font=weekday_font)
        tx = x1 + (x2 - x1 + 1 - (box[2] - box[0])) / 2
        target.text((tx, week_top + 3), label, font=weekday_font, fill=1)

    weeks = calendar.Calendar(firstweekday=0).monthdayscalendar(now.year, now.month)
    six_row_month = len(weeks) == 6
    day_font = font(15 if six_row_month else 17)
    lunar_font = font(9 if six_row_month else 10)
    grid_top = 86
    grid_bottom = 272
    row_height = (grid_bottom - grid_top) / len(weeks)
    for row, week in enumerate(weeks):
        row_top = round(grid_top + row * row_height)
        row_bottom = round(grid_top + (row + 1) * row_height)
        if row:
            black_draw.line((calendar_left, row_top, calendar_right, row_top), fill=0)
        for column, day_number in enumerate(week):
            if not day_number:
                continue
            current = date(now.year, now.month, day_number)
            center_x = calendar_left + column * cell_width + cell_width // 2
            number_y = row_top
            is_today = day_number == now.day
            label, is_special = calendar_label(current)
            weekend = column >= 5
            if is_today:
                today_radius = 10 if six_row_month else 11
                today_bottom = row_top + (21 if six_row_month else 24)
                red_draw.ellipse((center_x - today_radius, row_top, center_x + today_radius, today_bottom), fill=0)
                number_box = red_draw.textbbox((0, 0), str(day_number), font=day_font)
                red_draw.text(
                    (center_x - (number_box[2] - number_box[0]) / 2, number_y),
                    str(day_number),
                    font=day_font,
                    fill=1,
                )
            else:
                target = red_draw if weekend else black_draw
                text_center(target, center_x, number_y, str(day_number), day_font)

            label_draw = red_draw if weekend or is_special or is_today else black_draw
            label_y = min(row_bottom - (10 if six_row_month else 12), row_top + (19 if six_row_month else 23))
            text_center(label_draw, center_x, label_y, label, lunar_font)
            marker = holiday_marker(current)
            if marker:
                marker_draw = black_draw if marker == "班" else red_draw
                marker_draw.text((calendar_left + column * cell_width + cell_width - 8, row_top), marker, font=marker_font, fill=0)

    black_draw.line((divider_x, 52, divider_x, 272), fill=0)
    content_left = 258
    content_right = 389
    red_draw.text((content_left, 59), "Codex 配额", font=section_font, fill=0)

    remaining = max(0.0, min(100.0, 100.0 - weekly_window["used"]))
    black_draw.text((content_left, 86), "7 天窗口", font=quota_label_font, fill=0)
    value_text = f"{remaining:.0f}"
    text_right(black_draw, content_right - 12, 73, value_text, quota_value_font)
    black_draw.text((content_right - 11, 99), "%", font=percent_font, fill=0)
    bar_top = 119
    black_draw.rectangle((content_left, bar_top, content_right, bar_top + 9), outline=0)
    fill_width = round((content_right - content_left - 2) * remaining / 100)
    if fill_width:
        red_draw.rectangle((content_left + 1, bar_top + 1, content_left + fill_width, bar_top + 8), fill=0)
    reset_at = weekly_window.get("reset_at")
    reset_text = "重置时间不可用" if not reset_at else "重置 " + datetime.fromtimestamp(reset_at).astimezone().strftime("%m-%d %H:%M")
    black_draw.text((content_left, 132), reset_text, font=reset_font, fill=0)
    black_draw.line((content_left, 154, content_right, 154), fill=0)

    red_draw.text((content_left, 162), "今日日程", font=section_font, fill=0)
    count_text = f"{len(events)} 项"
    text_right(black_draw, content_right, 164, count_text, reset_font)
    if events:
        list_top = 188
        item_height = 21
        for index, event in enumerate(events[:4]):
            item_top = list_top + index * item_height
            time_text = "全天" if event.all_day else event.start.astimezone().strftime("%H:%M")
            red_draw.text((content_left, item_top), time_text, font=event_time_font, fill=0)
            title = fit_text(black_draw, event.title, event_font, content_right - (content_left + 38))
            black_draw.text((content_left + 38, item_top - 1), title, font=event_font, fill=0)
            if index < min(len(events), 4) - 1:
                black_draw.line((content_left, item_top + 18, content_right, item_top + 18), fill=0)
    else:
        text_center(black_draw, (content_left + content_right) // 2, 211, "今天没有日程", header_font)

    black_draw.line((11, 273, 389, 273), fill=0)
    black_draw.text((11, 279), now.strftime("更新 %Y-%m-%d %H:%M"), font=footer_font, fill=0)
    text_right(black_draw, 389, 279, "月历 · 日程", footer_font)

    preview = Image.new("RGB", (width, height), (251, 250, 246))
    preview.paste((23, 21, 19), mask=ImageOps.invert(black.convert("L")))
    preview.paste((188, 46, 46), mask=ImageOps.invert(red.convert("L")))
    return black, red, preview


def pack_monochrome(image: Image.Image) -> bytes:
    """Match the project protocol: one bit/pixel, MSB first, white=1."""
    width, height = image.size
    if width % 8:
        raise ValueError("display width must be divisible by 8")
    pixels = image.load()
    data = bytearray()
    for y in range(height):
        for x0 in range(0, width, 8):
            value = 0
            for bit in range(8):
                value |= (1 if pixels[x0 + bit, y] else 0) << (7 - bit)
            data.append(value)
    return bytes(data)


async def find_device(name_prefix: str):
    from bleak import BleakScanner

    print(f"Scanning continuously for {name_prefix}* (up to 30 seconds) …")
    loop = asyncio.get_running_loop()
    found = loop.create_future()
    visible_names: set[str] = set()

    def on_advertisement(device, advertisement):
        name = advertisement.local_name or device.name
        if name:
            visible_names.add(name)
        if name and name.startswith(name_prefix) and not found.done():
            found.set_result((device, advertisement, name))

    async with BleakScanner(detection_callback=on_advertisement):
        try:
            device, advertisement, name = await asyncio.wait_for(found, timeout=30.0)
        except TimeoutError as exc:
            visible = ", ".join(sorted(visible_names)) or "none"
            raise RuntimeError(f"No matching EPD found. Visible named devices: {visible}") from exc

    print(f"Selected {name} ({device.address}, RSSI {advertisement.rssi} dBm)")
    return device


async def write_card(device, black_payload: bytes, red_payload: bytes | None, clear_first: bool):
    from bleak import BleakClient

    async with BleakClient(device) as client:
        if not client.is_connected:
            raise RuntimeError("BLE connection was not established")
        max_data_len = 20

        def notification_handler(_, value: bytearray):
            nonlocal max_data_len
            raw = bytes(value)
            if len(raw) == 13:
                labels = ("MOSI", "SCLK", "CS", "DC", "RST", "BUSY", "BS", "model", "wake", "LED", "EN", "mode", "week")
                config = ", ".join(f"{label}={item:02X}" for label, item in zip(labels, raw))
                print(f"Device configuration: {config}")
            else:
                try:
                    message = raw.decode("utf-8")
                    print(f"Device notification: {message}")
                    if message.startswith("mtu="):
                        max_data_len = int(message[4:])
                except UnicodeDecodeError:
                    print(f"Device notification: {raw.hex()}")

        await client.start_notify(CHARACTERISTIC_UUID, notification_handler)

        async def write_packet(packet: bytes, response: bool = True, timeout: float = 15.0):
            try:
                await asyncio.wait_for(
                    client.write_gatt_char(CHARACTERISTIC_UUID, packet, response=response),
                    timeout=timeout,
                )
            except TimeoutError as exc:
                raise RuntimeError("BLE write timed out; move the display closer and retry.") from exc

        print("Connected; initializing EPD driver …")
        await write_packet(bytes([CMD_INIT]))
        await asyncio.sleep(0.3)

        if clear_first:
            print("Clearing the previous frame …")
            await write_packet(bytes([CMD_CLEAR]))
            await asyncio.sleep(1.0)
            # On the configured SSD1619 driver, CLEAR performs a refresh and
            # powers the panel off. Reinitialize before writing the next frame.
            print("Reinitializing the panel after clear …")
            await write_packet(bytes([CMD_INIT]))
            await asyncio.sleep(0.3)

        # Match the firmware's web client: stream 50 packets without a response,
        # then require a confirmed write for flow control. A completed local
        # write-without-response only means CoreBluetooth queued the packet; it
        # does not prove the display received it.
        chunk_size = max_data_len - 2  # command + layer/start byte
        if chunk_size <= 0:
            raise RuntimeError(f"Invalid EPD write length: {max_data_len}")
        confirm_interval = 20
        async def send_layer(payload: bytes, layer_name: str, layer_code: int):
            total = (len(payload) + chunk_size - 1) // chunk_size
            for index, offset in enumerate(range(0, len(payload), chunk_size), start=1):
                config = layer_code if offset == 0 else (0xF0 | layer_code)
                packet = bytes([CMD_WRITE_IMAGE, config]) + payload[offset : offset + chunk_size]
                response = index % confirm_interval == 0 or index == total
                await write_packet(packet, response=response, timeout=60.0)
                if response:
                    await asyncio.sleep(0.08)
                else:
                    await asyncio.sleep(0.003)
                if index % 20 == 0 or index == total:
                    print(f"Sent {layer_name} {index}/{total} chunks")

        await send_layer(black_payload, "black", 0x0F)
        if red_payload is not None:
            await send_layer(red_payload, "red", 0x00)

        print("Requesting screen refresh …")
        await write_packet(bytes([CMD_REFRESH]), response=True, timeout=60.0)
        print("Refresh command sent. The panel may take several seconds to settle.")


async def write_card_with_retry(
    name_prefix: str,
    black_payload: bytes,
    red_payload: bytes | None,
    *,
    clear_first: bool = False,
    attempts: int = 2,
    retry_delay: float = 2.0,
):
    if attempts < 1:
        raise ValueError("BLE attempts must be at least 1")
    for attempt in range(1, attempts + 1):
        try:
            device = await find_device(name_prefix)
            await write_card(device, black_payload, red_payload, clear_first=clear_first)
            return
        except Exception as exc:
            if attempt == attempts:
                raise
            print(f"BLE update attempt {attempt}/{attempts} failed: {exc}")
            print(f"Retrying the complete frame in {retry_delay:g} seconds …")
            await asyncio.sleep(retry_delay)


async def show_device_mode(device, mode: int, mode_name: str):
    from bleak import BleakClient

    async with BleakClient(device) as client:
        if not client.is_connected:
            raise RuntimeError("BLE connection was not established")
        def notification_handler(_, value: bytearray):
            raw = bytes(value)
            print(f"Device notification: {raw.decode('utf-8', errors='replace') if len(raw) != 13 else raw.hex()}")

        await client.start_notify(CHARACTERISTIC_UUID, notification_handler)
        print(f"Connected; asking the device to render {mode_name} …")
        await client.write_gatt_char(CHARACTERISTIC_UUID, bytes([CMD_INIT]), response=True)
        local_now = datetime.now().astimezone()
        now = int(local_now.timestamp())
        utc_offset = int(local_now.utcoffset().total_seconds() // 3600)
        # The firmware expects UTC seconds, a signed UTC offset, then display mode 1 (calendar).
        packet = bytes([CMD_SET_TIME]) + now.to_bytes(4, "big") + bytes([utc_offset & 0xFF, mode])
        await client.write_gatt_char(CHARACTERISTIC_UUID, packet, response=True)
        print(f"{mode_name} command sent. Wait up to 30 seconds for the EPD refresh to finish.")


async def read_device_info(device):
    from bleak import BleakClient

    async with BleakClient(device) as client:
        if not client.is_connected:
            raise RuntimeError("BLE connection was not established")
        version = bytes(await client.read_gatt_char(VERSION_UUID))
        if len(version) != 1:
            raise RuntimeError(f"Unexpected firmware version value: {version.hex()}")
        print(f"Device firmware version: 0x{version[0]:02X}")


async def main():
    parser = argparse.ArgumentParser(description="Render quota, calendar/agenda, or calendar/sensor cards for an EPD-nRF5 display.")
    parser.add_argument("--config", default="config.json", help="optional JSON configuration file")
    parser.add_argument("--mode", choices=("quota", "calendar-agenda", "calendar-sensor"), help="display layout")
    parser.add_argument("--name-prefix", default="NRF_EPD", help="advertised BLE name prefix")
    parser.add_argument("--width", type=int, default=400)
    parser.add_argument("--height", type=int, default=300)
    parser.add_argument("--output", help="preview image path; defaults to test-card.png")
    parser.add_argument("--state-file", help="last successfully displayed state; defaults beside this script")
    parser.add_argument("--force", action="store_true", help="refresh even when visible data is unchanged")
    parser.add_argument("--dry-run", action="store_true", help="render only; do not use Bluetooth")
    clear_options = parser.add_mutually_exclusive_group()
    clear_options.add_argument(
        "--clear-first",
        action="store_true",
        help="physically clear the panel before writing; disabled by default for fail-safe updates",
    )
    clear_options.add_argument("--no-clear", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--calendar-test", action="store_true", help="render the firmware's built-in calendar instead of sending an image")
    parser.add_argument(
        "--device-calendar-temperature",
        action="store_true",
        help="enable firmware calendar/temperature mode 4 instead of sending an image",
    )
    parser.add_argument("--device-info", action="store_true", help="read the connected device firmware version")
    parser.add_argument("--fixed-test", action="store_true", help="show the fixed BLE test card instead of live Codex quota")
    parser.add_argument("--temperature", type=float, help="one-off measured temperature in Celsius")
    parser.add_argument("--humidity", type=float, help="optional one-off measured relative humidity")
    parser.add_argument("--sensor-file", help="JSON file containing a current sensor reading")
    parser.add_argument("--sensor-url", help="HTTP endpoint returning a current sensor reading as JSON")
    parser.add_argument("--sensor-token", help="Bearer token for --sensor-url; prefer EPD_SENSOR_TOKEN")
    parser.add_argument("--temperature-key", help="dotted JSON key for temperature")
    parser.add_argument("--humidity-key", help="dotted JSON key for humidity")
    parser.add_argument("--timestamp-key", help="dotted JSON key for measurement time")
    parser.add_argument("--location", help="short location label shown on the card")
    parser.add_argument("--max-sensor-age", type=float, help="reject readings older than this many minutes; 0 disables")
    parser.add_argument("--demo-sensor", action="store_true", help="render explicit demo values instead of real sensor data")
    parser.add_argument("--calendar-name", action="append", help="include only this macOS Calendar; repeat for more")
    parser.add_argument("--agenda-limit", type=int, help="maximum number of today's events shown; default 4")
    args = parser.parse_args()

    if args.device_info:
        device = await find_device(args.name_prefix)
        await read_device_info(device)
        return

    config_path = Path(args.config).expanduser()
    config = load_json_config(config_path)
    sensor_config = config.get("sensor") or {}
    if not isinstance(sensor_config, dict):
        raise RuntimeError("The sensor configuration must be a JSON object.")
    calendar_config = config.get("calendar") or {}
    if not isinstance(calendar_config, dict):
        raise RuntimeError("The calendar configuration must be a JSON object.")

    def configured(cli_value, env_name: str, config_value, default=None):
        if cli_value is not None:
            return cli_value
        if env_name in os.environ:
            return os.environ[env_name]
        if config_value is not None:
            return config_value
        return default

    mode = configured(args.mode, "EPD_DISPLAY_MODE", config.get("display_mode"), "quota")
    if mode not in ("quota", "calendar-agenda", "calendar-sensor"):
        raise RuntimeError(f"Unsupported display mode: {mode}")

    output = Path(args.output).expanduser() if args.output else Path(__file__).with_name("test-card.png")
    state_path = Path(args.state_file).expanduser() if args.state_file else Path(__file__).with_name(".last-display-state.json")
    output.parent.mkdir(parents=True, exist_ok=True)
    display_state = None

    def unchanged(state: dict) -> bool:
        if args.dry_run or args.force:
            return False
        if load_display_state(state_path) == state:
            print("No visible data change since the last successful refresh; skipping Bluetooth update.")
            return True
        return False

    if args.fixed_test or args.calendar_test or args.device_calendar_temperature:
        black_image = build_test_card(args.width, args.height)
        red_image = None
        black_image.save(output)
    elif mode == "quota":
        windows = fetch_codex_quota()
        print("Fetched Codex usage windows: " + ", ".join(
            f"{window['label']} {100 - window['used']:.0f}% left" for window in windows
        ))
        display_state = quota_display_state(windows)
        if unchanged(display_state):
            return
        black_image, red_image, preview = build_quota_card(args.width, args.height, windows)
        preview.save(output)
    elif mode == "calendar-agenda":
        windows = fetch_codex_quota()
        weekly_window = weekly_quota_window(windows)
        remaining = 100 - weekly_window["used"]
        print(f"Fetched Codex 7-day window: {remaining:.0f}% left")

        configured_names = configured(args.calendar_name, "EPD_CALENDAR_NAMES", calendar_config.get("names"), [])
        if isinstance(configured_names, str):
            configured_names = [name.strip() for name in configured_names.split(",") if name.strip()]
        if not isinstance(configured_names, list):
            raise RuntimeError("calendar.names must be a JSON array or comma-separated environment value.")
        agenda_limit = int(configured(args.agenda_limit, "EPD_AGENDA_LIMIT", calendar_config.get("max_events"), 4))
        events = fetch_today_calendar_events(calendar_names=configured_names, max_events=agenda_limit)
        print(f"Loaded {len(events)} event{'s' if len(events) != 1 else ''} from macOS Calendar for today.")

        card_now = datetime.now().astimezone()
        display_state = calendar_agenda_display_state(weekly_window, events, card_now)
        if unchanged(display_state):
            return
        black_image, red_image, preview = build_calendar_agenda_card(
            args.width,
            args.height,
            weekly_window,
            events,
            now=card_now,
        )
        preview.save(output)
    else:
        sensor_file = configured(args.sensor_file, "EPD_SENSOR_FILE", sensor_config.get("file"))
        if sensor_file and not Path(sensor_file).expanduser().is_absolute() and config_path.exists():
            sensor_file = str(config_path.resolve().parent / sensor_file)
        max_age = float(configured(
            args.max_sensor_age,
            "EPD_MAX_SENSOR_AGE_MINUTES",
            sensor_config.get("max_age_minutes"),
            30,
        ))
        temperature_value = configured(args.temperature, "EPD_TEMPERATURE", sensor_config.get("temperature"))
        humidity_value = configured(args.humidity, "EPD_HUMIDITY", sensor_config.get("humidity"))
        reading = fetch_sensor_reading(
            temperature=float(temperature_value) if temperature_value is not None else None,
            humidity=float(humidity_value) if humidity_value is not None else None,
            sensor_file=sensor_file,
            sensor_url=configured(args.sensor_url, "EPD_SENSOR_URL", sensor_config.get("url")),
            sensor_token=configured(args.sensor_token, "EPD_SENSOR_TOKEN", sensor_config.get("token")),
            temperature_key=configured(args.temperature_key, "EPD_TEMPERATURE_KEY", sensor_config.get("temperature_key")),
            humidity_key=configured(args.humidity_key, "EPD_HUMIDITY_KEY", sensor_config.get("humidity_key")),
            timestamp_key=configured(args.timestamp_key, "EPD_TIMESTAMP_KEY", sensor_config.get("timestamp_key")),
            max_age_minutes=max_age,
            demo=args.demo_sensor,
        )
        location = str(configured(args.location, "EPD_SENSOR_LOCATION", sensor_config.get("location"), "室内"))
        humidity_log = f", humidity {reading.humidity:.0f}%" if reading.humidity is not None else ""
        print(
            f"Loaded current sensor reading from {reading.source}: "
            f"{reading.temperature:.1f}°C{humidity_log} at {reading.measured_at:%Y-%m-%d %H:%M}"
        )
        card_now = datetime.now().astimezone()
        display_state = calendar_sensor_display_state(reading, location, card_now)
        if unchanged(display_state):
            return
        black_image, red_image, preview = build_calendar_sensor_card(
            args.width,
            args.height,
            reading,
            now=card_now,
            location=location,
        )
        preview.save(output)
    black_payload = pack_monochrome(black_image)
    red_payload = pack_monochrome(red_image) if red_image is not None else None
    layer_count = 2 if red_payload is not None else 1
    print(f"Rendered {output} ({len(black_payload)} bytes x {layer_count} layer{'s' if layer_count > 1 else ''})")

    if args.dry_run:
        return
    if args.calendar_test or args.device_calendar_temperature:
        device = await find_device(args.name_prefix)
        mode = 4 if args.device_calendar_temperature else 1
        mode_name = "calendar/temperature mode" if mode == 4 else "built-in calendar"
        await show_device_mode(device, mode, mode_name)
        return
    await write_card_with_retry(
        args.name_prefix,
        black_payload,
        red_payload,
        clear_first=args.clear_first,
    )
    if display_state is not None:
        save_display_state(state_path, display_state)
        print(f"Saved displayed state to {state_path}")


if __name__ == "__main__":
    asyncio.run(main())
