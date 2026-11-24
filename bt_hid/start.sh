#!/bin/bash
# Runs as root inside WSL after the Bluetooth adapter has been attached with usbipd.
# Loads the drivers, starts bluetoothd without its input plugin (so hidd.py can own the
# HID channels) and starts hidd.py. Logs: /var/log/iphone-mirror-hid.log
set -u
HERE="$(cd "$(dirname "$0")" && pwd)"
LOG=/var/log/iphone-mirror-hid.log

# Intel 8260 firmware (only needed if the chip comes up in bootloader mode)
if [ -d "$HERE/fw" ]; then
  mkdir -p /lib/firmware/intel
  cp -n "$HERE"/fw/ibt-* /lib/firmware/intel/ 2>/dev/null
fi

modprobe btusb 2>/dev/null
modprobe btintel 2>/dev/null

# wait for the adapter
for _ in $(seq 1 40); do
  [ -e /sys/class/bluetooth/hci0 ] && break
  sleep 0.5
done
if [ ! -e /sys/class/bluetooth/hci0 ]; then
  echo "no hci0: is the adapter attached (usbipd attach --wsl)?" >&2
  exit 2
fi

# D-Bus system bus
if [ ! -S /run/dbus/system_bus_socket ]; then
  mkdir -p /run/dbus
  dbus-daemon --system --fork 2>/dev/null || service dbus start
fi

# bluetoothd without the input plugin
pkill -x bluetoothd 2>/dev/null
pkill -f "bt_hid/hidd.py" 2>/dev/null
sleep 0.5
BTD=$(command -v bluetoothd || echo /usr/libexec/bluetooth/bluetoothd)
[ -x "$BTD" ] || BTD=/usr/lib/bluetooth/bluetoothd
"$BTD" --noplugin=input,sap,network >>"$LOG" 2>&1 &
for _ in $(seq 1 20); do
  dbus-send --system --print-reply --dest=org.bluez /org/bluez/hci0 org.freedesktop.DBus.Properties.Get \
    string:org.bluez.Adapter1 string:Address >/dev/null 2>&1 && break
  sleep 0.5
done
hciconfig hci0 up 2>/dev/null

nohup python3 "$HERE/hidd.py" --name "${HID_NAME:-iPhone Mirror}" >>"$LOG" 2>&1 &
echo "started (log: $LOG)"
