# Development history

This document condenses the full implementation conversation into the decisions
and evidence that matter for reproducing the project.

## 1. Feasibility

The original firmware presents a BLE command channel capable of initializing
the EPD, receiving raw image bytes and asking the panel to refresh. It is not
limited to its built-in calendar: text, progress bars and arbitrary UI can be
rendered on the Mac as pixels and transferred as image planes.

The first architecture was deliberately simple:

```text
Codex local OAuth → macOS Python → Pillow renderer → Bleak → nRF52811 → EPD
```

The Mac performs the network request and rendering. The price tag remains a
low-power BLE display and does not need Wi-Fi.

## 2. First BLE sender

A Python proof of concept generated a 400×300 test card, packed it as one-bit
pixels and sent it through the firmware's custom GATT characteristic. A Bleak
API change caused the first error: `BLEDevice.rssi` no longer exists. Reading
RSSI from advertisement data fixed device selection.

## 3. Blank-screen investigation

The sender appeared successful: every chunk was acknowledged and the refresh
command was sent, yet the panel remained blank. The firmware's built-in
calendar test rendered correctly, proving the hardware configuration worked.

The remaining differences from the working web client were then mirrored:

- use the negotiated `mtu=244` notification;
- send 242 image bytes per packet;
- use write-without-response in batches with periodic confirmed writes;
- reinitialize the SSD1619 after clear because clear powers it off.

After the second initialization was added, transferred images appeared.

## 4. Real Codex quota

Quota retrieval follows the approach used by
[`farion1231/cc-switch`](https://github.com/farion1231/cc-switch): read the local
ChatGPT OAuth session from `~/.codex/auth.json`, call the Codex usage endpoint,
and parse its primary and secondary rate-limit windows. Tokens are used only in
the HTTPS authorization header and are never logged or copied into the image.

Claude Code is intentionally shown as an unconfigured placeholder until an
account and data source are available.

## 5. Display design

Several layouts were considered before choosing a single-column ledger. The
final hierarchy is:

1. AI quota panel status.
2. Codex provider title.
3. Equal 5-hour and 7-day windows, each with remaining percentage, progress bar
   and reset time.
4. An equal Claude Code section with empty placeholders.
5. Full update date and time.

The physical test showed that small metadata was hard to read, so provider
names, window labels, percent signs, reset times and the footer timestamp were
enlarged. The panel uses only black, red and white.

## 6. Scheduled operation

The proven script was registered as a user LaunchAgent. It runs every 30
minutes, logs output, and works without an open Terminal window. A live
LaunchAgent test successfully fetched quota, discovered the tag, transferred
both 15,000-byte planes, refreshed the display and exited with code 0.

