# Bluetooth control (experimental, not tested yet)

Control the iPhone from the PC **without a cable and without a jailbreak** by making the
PC's own Bluetooth adapter act as a Bluetooth keyboard + mouse + media keys.

iOS supports Bluetooth keyboards natively and Bluetooth mice through AssistiveTouch, so
nothing is installed on the iPhone and nothing expires (unlike WebDriverAgent with a free
Apple ID). The screen still comes over AirPlay; only input goes over Bluetooth.

## How it works

Windows cannot act as a Bluetooth HID device, but Linux can. The adapter is handed to WSL:

```
iPhone Mirror (Windows) --TCP 127.0.0.1:7200--> hidd.py (WSL, BlueZ) --Bluetooth HID--> iPhone
```

- `hidd.py`: registers a classic Bluetooth HID profile (keyboard report 1, mouse report 2
  with 16-bit relative motion, wheel and pan, consumer keys report 3), accepts pairing
  without a PIN, reconnects to the last iPhone, and takes commands on TCP port 7200.
- `start.sh`: loads `btusb`/`btintel`, starts `bluetoothd --noplugin=input` and `hidd.py`.

Command protocol (one per line): `m dx dy buttons wheel [pan]`, `k mods [keys...]`,
`c usage` (consumer key, e.g. `0xE9` volume up), `r` (release all), `d` (discoverable),
`s` (status).

## Setup (planned)

1. Install [usbipd-win](https://github.com/dorssel/usbipd-win) and, in WSL (Ubuntu 24.04),
   `sudo apt install bluez python3-dbus python3-gi`.
2. Share the adapter once (admin): `usbipd bind --busid <id>` (`usbipd list` shows it).
3. Attach and start: `usbipd attach --wsl --busid <id>`, then
   `wsl -u root bash bt_hid/start.sh`.
4. On the iPhone: Settings > Bluetooth, pair **iPhone Mirror**. For the pointer, turn on
   Settings > Accessibility > Touch > AssistiveTouch.
5. Detach to give Bluetooth back to Windows: `usbipd detach --busid <id>`.

While the adapter is attached to WSL, Windows has no Bluetooth (headphones, speakers).
A cheap USB Bluetooth dongle avoids that.

Some adapters (e.g. Intel 8260) may need their firmware file in `bt_hid/fw/` (from
linux-firmware, `intel/ibt-11-5.sfi`); it is not included in this repository.

## Limits

- The iPhone screen cannot be mirrored while the phone is locked (iOS stops AirPlay
  mirroring on lock). A Bluetooth keyboard can wake the phone; whether it can type the
  passcode depends on the iOS version.
- Not yet wired into the iPhone Mirror window (pointer capture, key mapping).
