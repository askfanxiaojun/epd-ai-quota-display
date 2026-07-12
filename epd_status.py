#!/usr/bin/env python3
"""Render live AI quota status and send it to an EPD-nRF5 device over BLE."""

import argparse
import asyncio
from datetime import datetime
import json
from pathlib import Path
import time
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from bleak import BleakClient, BleakScanner
from PIL import Image, ImageDraw, ImageFont, ImageOps

SERVICE_UUID = "62750001-d828-918d-fb46-b6c11c675aec"
CHARACTERISTIC_UUID = "62750002-d828-918d-fb46-b6c11c675aec"

CMD_INIT = 0x01
CMD_CLEAR = 0x02
CMD_WRITE_IMAGE = 0x30
CMD_REFRESH = 0x05
CMD_SET_TIME = 0x20
CODEX_USAGE_URL = "https://chatgpt.com/backend-api/wham/usage"


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
    codex_windows = [
        by_label.get("5 HOURS", windows[0]),
        by_label.get("7 DAYS", windows[min(1, len(windows) - 1)]),
    ]

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
                remaining = max(0.0, min(100.0, 100.0 - window["used"]))
                value = f"{remaining:.0f}"
                # PingFang's visible digits start well below Pillow's supplied
                # Y coordinate. Keep the digit box above the progress bar even
                # for a wide value such as 99%.
                text_right(black_draw, x2 - 13, top + 25, value, number_font)
                black_draw.text((x2 - 12, top + 47), "%", font=percent_font, fill=0)

                bar_top = top + 65
                black_draw.rectangle((x1, bar_top, x2, bar_top + 7), outline=0)
                fill_width = round((x2 - x1 - 2) * remaining / 100)
                if fill_width > 0:
                    black_draw.rectangle((x1 + 1, bar_top + 1, x1 + fill_width, bar_top + 6), fill=0)
                black_draw.text((x1, top + 74), reset_label(window), font=meta_font, fill=0)
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
    print(f"Scanning for {name_prefix}* …")
    # Bleak 3.x keeps RSSI in AdvertisementData rather than BLEDevice.
    advertisements = await BleakScanner.discover(timeout=8.0, return_adv=True)
    matches = [
        (device, advertisement)
        for device, advertisement in advertisements.values()
        if device.name and device.name.startswith(name_prefix)
    ]
    if not matches:
        visible = ", ".join(sorted({device.name for device, _ in advertisements.values() if device.name})) or "none"
        raise RuntimeError(f"No matching EPD found. Visible named devices: {visible}")
    device, advertisement = max(matches, key=lambda item: item[1].rssi if item[1].rssi is not None else -999)
    print(f"Selected {device.name} ({device.address}, RSSI {advertisement.rssi} dBm)")
    return device


async def write_card(device, black_payload: bytes, red_payload: bytes | None, clear_first: bool):
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
        print("Connected; initializing EPD driver …")
        await client.write_gatt_char(CHARACTERISTIC_UUID, bytes([CMD_INIT]), response=True)
        await asyncio.sleep(0.3)

        if clear_first:
            print("Clearing the previous frame …")
            await client.write_gatt_char(CHARACTERISTIC_UUID, bytes([CMD_CLEAR]), response=True)
            await asyncio.sleep(1.0)
            # On the configured SSD1619 driver, CLEAR performs a refresh and
            # powers the panel off. Reinitialize before writing the next frame.
            print("Reinitializing the panel after clear …")
            await client.write_gatt_char(CHARACTERISTIC_UUID, bytes([CMD_INIT]), response=True)
            await asyncio.sleep(0.3)

        # Mirror the original web client: it uses the negotiated characteristic
        # length, sends 50 write-without-response packets, then one confirmed
        # write as flow control. Sending every packet with a response is safe
        # at the GATT layer but does not match this firmware's proven path.
        chunk_size = max_data_len - 2  # command + layer/start byte
        if chunk_size <= 0:
            raise RuntimeError(f"Invalid EPD write length: {max_data_len}")
        confirm_interval = 50
        async def send_layer(payload: bytes, layer_name: str, layer_code: int):
            total = (len(payload) + chunk_size - 1) // chunk_size
            for index, offset in enumerate(range(0, len(payload), chunk_size), start=1):
                config = layer_code if offset == 0 else (0xF0 | layer_code)
                packet = bytes([CMD_WRITE_IMAGE, config]) + payload[offset : offset + chunk_size]
                response = index % (confirm_interval + 1) == 0 or index == total
                await client.write_gatt_char(CHARACTERISTIC_UUID, packet, response=response)
                if response:
                    await asyncio.sleep(0.08)
                if index % 20 == 0 or index == total:
                    print(f"Sent {layer_name} {index}/{total} chunks")

        await send_layer(black_payload, "black", 0x0F)
        if red_payload is not None:
            await send_layer(red_payload, "red", 0x00)

        print("Requesting screen refresh …")
        await client.write_gatt_char(CHARACTERISTIC_UUID, bytes([CMD_REFRESH]), response=True)
        print("Refresh command sent. The panel may take several seconds to settle.")


async def show_calendar(device):
    async with BleakClient(device) as client:
        if not client.is_connected:
            raise RuntimeError("BLE connection was not established")
        def notification_handler(_, value: bytearray):
            raw = bytes(value)
            print(f"Device notification: {raw.decode('utf-8', errors='replace') if len(raw) != 13 else raw.hex()}")

        await client.start_notify(CHARACTERISTIC_UUID, notification_handler)
        print("Connected; asking the device to render its built-in calendar …")
        await client.write_gatt_char(CHARACTERISTIC_UUID, bytes([CMD_INIT]), response=True)
        now = int(time.time())
        # The firmware expects UTC seconds, a signed UTC offset, then display mode 1 (calendar).
        packet = bytes([CMD_SET_TIME]) + now.to_bytes(4, "big") + bytes([8, 1])
        await client.write_gatt_char(CHARACTERISTIC_UUID, packet, response=True)
        print("Calendar command sent. Wait up to 30 seconds for the EPD refresh to finish.")


async def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--name-prefix", default="NRF_EPD", help="advertised BLE name prefix")
    parser.add_argument("--width", type=int, default=400)
    parser.add_argument("--height", type=int, default=300)
    parser.add_argument("--dry-run", action="store_true", help="render only; do not use Bluetooth")
    parser.add_argument("--no-clear", action="store_true", help="keep the existing color plane")
    parser.add_argument("--calendar-test", action="store_true", help="render the firmware's built-in calendar instead of sending an image")
    parser.add_argument("--fixed-test", action="store_true", help="show the fixed BLE test card instead of live Codex quota")
    args = parser.parse_args()

    windows = None
    if not args.fixed_test and not args.calendar_test:
        windows = fetch_codex_quota()
        print("Fetched Codex usage windows: " + ", ".join(
            f"{window['label']} {100 - window['used']:.0f}% left" for window in windows
        ))
    output = Path(__file__).with_name("test-card.png")
    if windows is None:
        black_image = build_test_card(args.width, args.height)
        red_image = None
        black_image.save(output)
    else:
        black_image, red_image, preview = build_quota_card(args.width, args.height, windows)
        preview.save(output)
    black_payload = pack_monochrome(black_image)
    red_payload = pack_monochrome(red_image) if red_image is not None else None
    layer_count = 2 if red_payload is not None else 1
    print(f"Rendered {output} ({len(black_payload)} bytes x {layer_count} layer{'s' if layer_count > 1 else ''})")

    if args.dry_run:
        return
    device = await find_device(args.name_prefix)
    if args.calendar_test:
        await show_calendar(device)
        return
    await write_card(device, black_payload, red_payload, clear_first=not args.no_clear)


if __name__ == "__main__":
    asyncio.run(main())
