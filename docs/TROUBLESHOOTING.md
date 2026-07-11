# Troubleshooting

## `BLEDevice` has no attribute `rssi`

Recent Bleak versions moved RSSI from `BLEDevice` to `AdvertisementData`. Scan
with `return_adv=True` and select the strongest `(device, advertisement)` pair
using `advertisement.rssi`. The current script already implements this.

## Calendar works, but a transferred image stays blank

This proves the panel, pin configuration and refresh path are basically sound.
On the tested SSD1619 driver, the clear command powers the driver down after its
own refresh. Reinitialize the driver after clearing and before sending image
data. Also send data using the negotiated MTU and the firmware's flow-control
pattern.

## Screen clears, but the new image does not appear

Check the log for all of these milestones:

```text
Sent black 62/62 chunks
Sent red 62/62 chunks
Requesting screen refresh …
Refresh command sent.
```

If transfer stops partway through, move the tag closer to the Mac and make sure
no browser or other BLE client is connected to it.

## No matching EPD found

- Confirm the tag is powered and advertising as `NRF_EPD_*`.
- Disconnect any other BLE client.
- Keep the tag close to the Mac for the initial test.
- Run the built-in calendar test to separate BLE/firmware problems from image
  encoding problems.

```zsh
.venv/bin/python epd_status.py --calendar-test
```

## Codex usage request fails

The program requires a locally signed-in Codex ChatGPT account and reads
`~/.codex/auth.json`. It does not support subscription quota retrieval from API
key mode. Reauthenticate Codex if the endpoint returns HTTP 401 or 403.

## The scheduled update did not run

The Mac must be awake and the user must be logged in. Inspect:

```zsh
launchctl print gui/$(id -u)/com.local.epd-ai-quota-display
tail -n 100 logs/update.log
tail -n 100 logs/error.log
```

`launchd` does not wake this Mac solely for the task. A run missed during sleep
will occur only after the system is available again.

