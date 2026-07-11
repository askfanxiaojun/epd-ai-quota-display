# BLE protocol notes

This project talks to the firmware from
[`YCD12/EPD-nRF5_DYC`](https://github.com/YCD12/EPD-nRF5_DYC). The notes below
describe the behavior verified with an nRF52811 price tag and a 400×300
SSD1619 black/white/red panel.

## GATT service

| Purpose | UUID |
| --- | --- |
| Service | `62750001-d828-918d-fb46-b6c11c675aec` |
| Command/data characteristic | `62750002-d828-918d-fb46-b6c11c675aec` |

The device advertises with a name beginning with `NRF_EPD`.

## Commands used

| Byte | Command |
| --- | --- |
| `0x01` | Initialize EPD driver |
| `0x02` | Clear and refresh panel |
| `0x30` | Write image data |
| `0x05` | Refresh panel |
| `0x20` | Set time / select built-in display mode |

## Image format

- Resolution: 400×300 pixels.
- One bit per pixel, MSB first.
- White is `1`; colored pixel is `0` in its color plane.
- One plane is 15,000 bytes (`400 × 300 ÷ 8`).
- Layer code `0x0F` selects black; `0x00` selects red.

The first packet for a plane uses its layer code directly. Continuation packets
OR the code with `0xF0`. Each packet begins with command byte `0x30`, followed
by the layer/start byte and image data.

The firmware reports the negotiated characteristic length, commonly `244`.
The sender therefore uses `244 - 2 = 242` image bytes per packet. To match the
original web client, it sends 50 packets without response and then a confirmed
write for flow control.

## Important clear behavior

On the tested SSD1619 configuration, `CLEAR` refreshes and then powers the EPD
driver off. Sending image data immediately afterwards succeeds at the BLE layer
but leaves the screen blank. The working sequence is:

1. `INIT`
2. `CLEAR`
3. wait for the clear operation
4. `INIT` again
5. send the complete black plane
6. send the complete red plane
7. `REFRESH`

The reinitialization after clear was the key fix for the original blank-screen
problem.

