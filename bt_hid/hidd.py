#!/usr/bin/env python3
"""Bluetooth HID keyboard + mouse + media keys for an iPhone, run as root inside WSL.

The PC's Bluetooth adapter (passed into WSL with usbipd) presents itself as a classic
Bluetooth HID device. iOS supports Bluetooth keyboards natively and Bluetooth mice through
AssistiveTouch, so the iPhone needs no app, no cable and no jailbreak.

iPhone Mirror (Windows) sends commands over TCP (default 127.0.0.1:7200 via WSL's
localhost forwarding), one per line:

    m <dx> <dy> <buttons> <wheel> [pan]   mouse report (relative, buttons bit0=L bit1=R bit2=M)
    k <mods> [k1 .. k6]                   keyboard report (HID usage ids, modifier bitmask)
    c <usage>                             consumer key press+release (e.g. 0xE9 vol up, 0x223 home)
    r                                     release everything
    s                                     status line back: "ok connected <addr>" / "ok waiting"

Requires bluetoothd started with --noplugin=input so that this program can own L2CAP
PSMs 17 (HID control) and 19 (HID interrupt).
"""
import argparse
import json
import logging
import os
import select
import socket
import subprocess
import threading
import time

import dbus
import dbus.service
import dbus.mainloop.glib
from gi.repository import GLib

log = logging.getLogger("hidd")

PSM_CTRL, PSM_INTR = 17, 19
STATE_FILE = "/var/lib/iphone-mirror-hid.json"  # remembers the last connected host
PROFILE_PATH = "/org/iphonemirror/hid"
AGENT_PATH = "/org/iphonemirror/agent"
HID_UUID = "00001124-0000-1000-8000-00805f9b34fb"

# Report 1: keyboard (8 bytes) + LED output; report 2: mouse (buttons, X, Y, wheel, pan,
# 16-bit relative X/Y for smooth motion); report 3: consumer control (one 16-bit usage).
REPORT_MAP = bytes([
    0x05, 0x01, 0x09, 0x06, 0xA1, 0x01, 0x85, 0x01,             # Keyboard, report 1
    0x05, 0x07, 0x19, 0xE0, 0x29, 0xE7, 0x15, 0x00, 0x25, 0x01,
    0x75, 0x01, 0x95, 0x08, 0x81, 0x02,                         #   modifiers
    0x95, 0x01, 0x75, 0x08, 0x81, 0x01,                         #   reserved
    0x95, 0x05, 0x75, 0x01, 0x05, 0x08, 0x19, 0x01, 0x29, 0x05, 0x91, 0x02,  # LEDs
    0x95, 0x01, 0x75, 0x03, 0x91, 0x01,
    0x95, 0x06, 0x75, 0x08, 0x15, 0x00, 0x26, 0xFF, 0x00,
    0x05, 0x07, 0x19, 0x00, 0x2A, 0xFF, 0x00, 0x81, 0x00,       #   6 keys
    0xC0,
    0x05, 0x01, 0x09, 0x02, 0xA1, 0x01, 0x85, 0x02,             # Mouse, report 2
    0x09, 0x01, 0xA1, 0x00,
    0x05, 0x09, 0x19, 0x01, 0x29, 0x05, 0x15, 0x00, 0x25, 0x01,
    0x95, 0x05, 0x75, 0x01, 0x81, 0x02, 0x95, 0x01, 0x75, 0x03, 0x81, 0x01,
    0x05, 0x01, 0x09, 0x30, 0x09, 0x31,
    0x16, 0x01, 0x80, 0x26, 0xFF, 0x7F, 0x75, 0x10, 0x95, 0x02, 0x81, 0x06,  # X, Y int16 rel
    0x09, 0x38, 0x15, 0x81, 0x25, 0x7F, 0x75, 0x08, 0x95, 0x01, 0x81, 0x06,  # wheel
    0x05, 0x0C, 0x0A, 0x38, 0x02, 0x95, 0x01, 0x81, 0x06,                    # AC Pan
    0xC0, 0xC0,
    0x05, 0x0C, 0x09, 0x01, 0xA1, 0x01, 0x85, 0x03,             # Consumer, report 3
    0x15, 0x00, 0x26, 0xFF, 0x03, 0x19, 0x00, 0x2A, 0xFF, 0x03,
    0x75, 0x10, 0x95, 0x01, 0x81, 0x00,
    0xC0,
])

SDP_RECORD = """<?xml version="1.0" encoding="UTF-8" ?>
<record>
  <attribute id="0x0001"><sequence><uuid value="0x1124" /></sequence></attribute>
  <attribute id="0x0004">
    <sequence>
      <sequence><uuid value="0x0100" /><uint16 value="0x0011" /></sequence>
      <sequence><uuid value="0x0011" /></sequence>
    </sequence>
  </attribute>
  <attribute id="0x0005"><sequence><uuid value="0x1002" /></sequence></attribute>
  <attribute id="0x0006"><sequence><uint16 value="0x656e" /><uint16 value="0x006a" /><uint16 value="0x0100" /></sequence></attribute>
  <attribute id="0x0009"><sequence><sequence><uuid value="0x1124" /><uint16 value="0x0101" /></sequence></sequence></attribute>
  <attribute id="0x000d">
    <sequence><sequence>
      <sequence><uuid value="0x0100" /><uint16 value="0x0013" /></sequence>
      <sequence><uuid value="0x0011" /></sequence>
    </sequence></sequence>
  </attribute>
  <attribute id="0x0100"><text value="iPhone Mirror Control" /></attribute>
  <attribute id="0x0101"><text value="Keyboard and mouse" /></attribute>
  <attribute id="0x0102"><text value="iPhone Mirror" /></attribute>
  <attribute id="0x0200"><uint16 value="0x0100" /></attribute>
  <attribute id="0x0201"><uint16 value="0x0111" /></attribute>
  <attribute id="0x0202"><uint8 value="0xC0" /></attribute>
  <attribute id="0x0203"><uint8 value="0x00" /></attribute>
  <attribute id="0x0204"><boolean value="false" /></attribute>
  <attribute id="0x0205"><boolean value="true" /></attribute>
  <attribute id="0x0206">
    <sequence><sequence><uint8 value="0x22" /><text encoding="hex" value="%s" /></sequence></sequence>
  </attribute>
  <attribute id="0x0207"><sequence><sequence><uint16 value="0x0409" /><uint16 value="0x0100" /></sequence></sequence></attribute>
  <attribute id="0x020b"><uint16 value="0x0100" /></attribute>
  <attribute id="0x020c"><uint16 value="0x0c80" /></attribute>
  <attribute id="0x020d"><boolean value="true" /></attribute>
  <attribute id="0x020e"><boolean value="false" /></attribute>
  <attribute id="0x020f"><uint16 value="0x0640" /></attribute>
  <attribute id="0x0210"><uint16 value="0x0320" /></attribute>
</record>
""" % REPORT_MAP.hex()


class Profile(dbus.service.Object):
    @dbus.service.method("org.bluez.Profile1", in_signature="", out_signature="")
    def Release(self):
        pass

    @dbus.service.method("org.bluez.Profile1", in_signature="oha{sv}", out_signature="")
    def NewConnection(self, path, fd, props):
        os.close(fd.take())  # we accept on our own L2CAP sockets

    @dbus.service.method("org.bluez.Profile1", in_signature="o", out_signature="")
    def RequestDisconnection(self, path):
        pass


class Agent(dbus.service.Object):
    """Accepts pairing without a PIN (Secure Simple Pairing, 'just works')."""

    @dbus.service.method("org.bluez.Agent1", in_signature="", out_signature="")
    def Release(self):
        pass

    @dbus.service.method("org.bluez.Agent1", in_signature="os", out_signature="")
    def AuthorizeService(self, device, uuid):
        log.info("authorize service %s %s", device, uuid)

    @dbus.service.method("org.bluez.Agent1", in_signature="o", out_signature="s")
    def RequestPinCode(self, device):
        return "0000"

    @dbus.service.method("org.bluez.Agent1", in_signature="o", out_signature="u")
    def RequestPasskey(self, device):
        return dbus.UInt32(0)

    @dbus.service.method("org.bluez.Agent1", in_signature="ouq", out_signature="")
    def DisplayPasskey(self, device, passkey, entered):
        log.info("passkey %06d", passkey)

    @dbus.service.method("org.bluez.Agent1", in_signature="ou", out_signature="")
    def RequestConfirmation(self, device, passkey):
        log.info("pairing confirm %06d for %s (auto-accepted)", passkey, device)

    @dbus.service.method("org.bluez.Agent1", in_signature="o", out_signature="")
    def RequestAuthorization(self, device):
        log.info("pairing authorized for %s", device)

    @dbus.service.method("org.bluez.Agent1", in_signature="", out_signature="")
    def Cancel(self):
        pass


class Hid:
    def __init__(self, name):
        self.name = name
        self.ctrl = self.intr = None
        self.host = None
        self.lock = threading.Lock()
        self.keys = [0] * 6
        self.mods = 0
        self.buttons = 0

    # ------------------------------------------------------------------ setup
    def setup_bluez(self):
        dbus.mainloop.glib.DBusGMainLoop(set_as_default=True)
        bus = dbus.SystemBus()
        self.bus = bus
        adapter_path = "/org/bluez/hci0"
        props = dbus.Interface(bus.get_object("org.bluez", adapter_path), "org.freedesktop.DBus.Properties")
        props.Set("org.bluez.Adapter1", "Alias", dbus.String(self.name))
        props.Set("org.bluez.Adapter1", "Powered", dbus.Boolean(True))
        props.Set("org.bluez.Adapter1", "Pairable", dbus.Boolean(True))
        props.Set("org.bluez.Adapter1", "PairableTimeout", dbus.UInt32(0))
        props.Set("org.bluez.Adapter1", "DiscoverableTimeout", dbus.UInt32(0))
        props.Set("org.bluez.Adapter1", "Discoverable", dbus.Boolean(True))
        self.adapter_props = props
        # keyboard + pointing device class so iOS lists it as a keyboard
        subprocess.run(["hciconfig", "hci0", "class", "0x002540"], check=False)
        subprocess.run(["hciconfig", "hci0", "sspmode", "1"], check=False)
        Profile(bus, PROFILE_PATH)
        mgr = dbus.Interface(bus.get_object("org.bluez", "/org/bluez"), "org.bluez.ProfileManager1")
        mgr.RegisterProfile(PROFILE_PATH, HID_UUID, {
            "ServiceRecord": SDP_RECORD, "Role": "server",
            "RequireAuthentication": False, "RequireAuthorization": False,
        })
        Agent(bus, AGENT_PATH)
        am = dbus.Interface(bus.get_object("org.bluez", "/org/bluez"), "org.bluez.AgentManager1")
        am.RegisterAgent(AGENT_PATH, "NoInputNoOutput")
        am.RequestDefaultAgent(AGENT_PATH)
        log.info("adapter '%s' discoverable, HID profile registered", self.name)

    def set_discoverable(self, on):
        try:
            self.adapter_props.Set("org.bluez.Adapter1", "Discoverable", dbus.Boolean(on))
        except Exception as e:
            log.warning("discoverable: %s", e)

    def trust(self, addr):
        path = "/org/bluez/hci0/dev_" + addr.replace(":", "_")
        try:
            p = dbus.Interface(self.bus.get_object("org.bluez", path), "org.freedesktop.DBus.Properties")
            p.Set("org.bluez.Device1", "Trusted", dbus.Boolean(True))
        except Exception as e:
            log.debug("trust %s: %s", addr, e)

    # ------------------------------------------------------------------ connections
    def listen(self):
        def mk(psm):
            s = socket.socket(socket.AF_BLUETOOTH, socket.SOCK_SEQPACKET, socket.BTPROTO_L2CAP)
            s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            s.bind((socket.BDADDR_ANY, psm))
            s.listen(1)
            return s
        self.lctrl, self.lintr = mk(PSM_CTRL), mk(PSM_INTR)
        threading.Thread(target=self._accept_loop, daemon=True).start()
        threading.Thread(target=self._reconnect_loop, daemon=True).start()

    def _accept_loop(self):
        while True:
            c, (addr, _) = self.lctrl.accept()
            log.info("control channel from %s", addr)
            i, _ = self.lintr.accept()
            log.info("interrupt channel from %s", addr)
            self._connected(c, i, addr)

    def _reconnect_loop(self):
        """HID devices reconnect to their host: dial the last iPhone when not connected."""
        while True:
            time.sleep(3)
            if self.intr is not None:
                continue
            host = self._load_host()
            if not host:
                continue
            try:
                c = socket.socket(socket.AF_BLUETOOTH, socket.SOCK_SEQPACKET, socket.BTPROTO_L2CAP)
                c.settimeout(8)
                c.connect((host, PSM_CTRL))
                i = socket.socket(socket.AF_BLUETOOTH, socket.SOCK_SEQPACKET, socket.BTPROTO_L2CAP)
                i.settimeout(8)
                i.connect((host, PSM_INTR))
                c.settimeout(None)
                i.settimeout(None)
                log.info("reconnected to %s", host)
                self._connected(c, i, host)
            except OSError as e:
                log.debug("reconnect %s: %s", host, e)
                time.sleep(4)

    def _connected(self, c, i, addr):
        with self.lock:
            for s in (self.ctrl, self.intr):
                try:
                    s and s.close()
                except OSError:
                    pass
            self.ctrl, self.intr, self.host = c, i, addr
        self._save_host(addr)
        self.trust(addr)
        self.set_discoverable(False)
        threading.Thread(target=self._ctrl_loop, args=(c,), daemon=True).start()

    def _ctrl_loop(self, c):
        """Minimal HID control channel: acknowledge SET_* and answer GET_REPORT with zeros."""
        try:
            while True:
                data = c.recv(64)
                if not data:
                    break
                t = data[0] >> 4
                if t in (0x5, 0x6, 0x7):  # SET_REPORT / SET_PROTOCOL / SET_IDLE (legacy)
                    c.send(b"\x00")
                elif t == 0x4:  # GET_REPORT
                    rid = data[1] if len(data) > 1 else 1
                    size = {1: 8, 2: 7, 3: 2}.get(rid, 8)
                    c.send(bytes([0xA1, rid]) + bytes(size))
                elif t == 0x1 and len(data) and (data[0] & 0x0F) == 0x5:  # HID_CONTROL virtual unplug
                    break
        except OSError:
            pass
        log.info("disconnected from %s", self.host)
        with self.lock:
            if self.ctrl is c:
                for s in (self.ctrl, self.intr):
                    try:
                        s.close()
                    except OSError:
                        pass
                self.ctrl = self.intr = None

    def _load_host(self):
        try:
            with open(STATE_FILE) as f:
                return json.load(f).get("host")
        except Exception:
            return None

    def _save_host(self, addr):
        try:
            with open(STATE_FILE, "w") as f:
                json.dump({"host": addr}, f)
        except OSError:
            pass

    # ------------------------------------------------------------------ reports
    def _send(self, payload):
        with self.lock:
            if self.intr is None:
                return False
            try:
                self.intr.send(b"\xA1" + payload)
                return True
            except OSError as e:
                log.info("send failed: %s", e)
                return False

    def mouse(self, dx, dy, buttons, wheel, pan=0):
        self.buttons = buttons & 0x1F
        while True:  # split big moves into int16-sized reports
            sx = max(-32767, min(32767, dx))
            sy = max(-32767, min(32767, dy))
            self._send(bytes([2, self.buttons]) + sx.to_bytes(2, "little", signed=True)
                       + sy.to_bytes(2, "little", signed=True)
                       + max(-127, min(127, wheel)).to_bytes(1, "little", signed=True)
                       + max(-127, min(127, pan)).to_bytes(1, "little", signed=True))
            dx, dy, wheel, pan = dx - sx, dy - sy, 0, 0
            if not dx and not dy:
                break

    def keyboard(self, mods, keys):
        self.mods = mods & 0xFF
        self.keys = (list(keys) + [0] * 6)[:6]
        self._send(bytes([1, self.mods, 0] + self.keys))

    def consumer(self, usage):
        self._send(bytes([3]) + int(usage).to_bytes(2, "little"))
        time.sleep(0.04)
        self._send(bytes([3, 0, 0]))

    def release(self):
        self.keyboard(0, [])
        self.mouse(0, 0, 0, 0)


def serve_commands(hid, host, port):
    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind((host, port))
    srv.listen(4)
    log.info("command port %s:%d", host, port)

    def client(conn):
        conn.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        buf = b""
        try:
            while True:
                chunk = conn.recv(4096)
                if not chunk:
                    break
                buf += chunk
                while b"\n" in buf:
                    line, buf = buf.split(b"\n", 1)
                    parts = line.decode("ascii", "ignore").split()
                    if not parts:
                        continue
                    cmd, args = parts[0], [int(x, 0) for x in parts[1:]]
                    if cmd == "m":
                        hid.mouse(*(args + [0] * (5 - len(args)))[:5])
                    elif cmd == "k":
                        hid.keyboard(args[0] if args else 0, args[1:])
                    elif cmd == "c" and args:
                        hid.consumer(args[0])
                    elif cmd == "r":
                        hid.release()
                    elif cmd == "d":
                        hid.set_discoverable(True)
                        conn.sendall(b"ok discoverable\n")
                    elif cmd == "s":
                        st = f"ok connected {hid.host}" if hid.intr is not None else "ok waiting"
                        conn.sendall(st.encode() + b"\n")
        except OSError:
            pass
        finally:
            hid.release()
            conn.close()

    while True:
        c, _ = srv.accept()
        threading.Thread(target=client, args=(c,), daemon=True).start()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--name", default="iPhone Mirror")
    ap.add_argument("--host", default="0.0.0.0")
    ap.add_argument("--port", type=int, default=7200)
    ap.add_argument("-v", action="store_true")
    a = ap.parse_args()
    logging.basicConfig(level=logging.DEBUG if a.v else logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    hid = Hid(a.name)
    hid.setup_bluez()
    hid.listen()
    threading.Thread(target=serve_commands, args=(hid, a.host, a.port), daemon=True).start()
    GLib.MainLoop().run()


if __name__ == "__main__":
    main()
