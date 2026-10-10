#!/usr/bin/env python3
"""
Combined Divaller / ZhouSensor / HORI NSW-230 / HORI PS4-161 bridge for TLAC
(PD-Loader) and BridgeSlider (Mega Mix+) under Wine.

Owns one slider controller (through libusb or hidapi) and serves it to DivallerBridge.dva
(TLAC) or BridgeSlider.dll (Mega Mix+) over TCP as a Divaller: the same
protocol, the same 24-byte "BVZ" input packets and the same 100-byte "DLa" LED
packets. The controller type is picked from its USB VID/PID:

    Divaller           0E8F:2213   passed through unchanged (slider LEDs + button LEDs)
    ZhouSensor (HDD)   1973:8000   translated (slider LEDs; button LEDs are board-only)
    HORI NSW-230       0F0D:00FB   translated from arcade-mode sticks (no LED control)
    HORI PS4-161       0F0D:013C   translated from arcade-mode sticks (no LED control)

    pip install pyusb hidapi          # pyusb needs libusb-1.0; hidapi brings its own library
    python3 diva_bridge.py            # --device auto|divaller|zhousensor|hori|hori_ps4
                                      # --backend auto|libusb|hidapi
                                      # --host ADDR, --port N, --verbose, --tui
                                      # --slider-reverse, --led-reverse, --no-leds

USB access (--backend)
    libusb   Talks to the USB device directly (pyusb). Needed for the Divaller,
             which is not a HID device. On Linux this is the default for all four.
    hidapi   Uses the operating system's HID driver (pip install hidapi). The
             ZhouSensor and both HORIs are HID devices, so on Windows and macOS
             this works without Zadig/WinUSB and without "Access denied".
    auto     hidapi for HID controllers on Windows/macOS, libusb otherwise; falls
             back to the other one when a library is missing or the open fails.

Notes per controller
    ZhouSensor  Must be in "HDD" USB mode (settings menu page 5, third slider zone
                cycles HDD -> Keyboard -> NS). Factory default is NS (Switch).
    HORI        (both) Must be in ARCADE mode: the slider is then reported as the 32-bit
                touch state spread across both analog sticks (each axis XOR 0x80).
                In the HORI's normal mode the slider moves the sticks instead and
                the decoded touches will be wrong.

Protocol (requests start with one opcode byte):
    'P'                         -> status(1)                   device present?
    'R' ep(1) max(1)            -> status(1) len(1) data[len]  latest IN packet
    'W' ep(1) len(1) data[len]  -> status(1)                   queue OUT packet
    'X' ep(1) max(1)            -> status(1) len(1) data[len]  latest IN packet + extended
                                   buttons: 24-byte packet, then 2 bytes little-endian:
                                   bit 0-3 D-pad up/down/left/right, 4 LB, 5 RB, 6 LT,
                                   7 RT, 8 LS (L3), 9 RS (R3). Used by BridgeSlider;
                                   TLAC never sends it, so TLAC sees no change.

Linux permissions (udev), e.g. /etc/udev/rules.d/70-diva-controllers.rules:
    SUBSYSTEM=="usb", ATTRS{idVendor}=="0e8f", ATTRS{idProduct}=="2213", MODE="0666"
    SUBSYSTEM=="usb", ATTRS{idVendor}=="1973", ATTRS{idProduct}=="8000", MODE="0666"
    SUBSYSTEM=="usb", ATTRS{idVendor}=="0f0d", ATTRS{idProduct}=="00fb", MODE="0666"
    SUBSYSTEM=="usb", ATTRS{idVendor}=="0f0d", ATTRS{idProduct}=="013c", MODE="0666"
"""

import argparse
import collections
import os
import socket
import socketserver
import sys
import threading
import time

try:
    import usb.core
    import usb.util
    HAVE_USB = True
except ImportError:          # hidapi-only installs are fine
    usb = None
    HAVE_USB = False

try:
    import hid
    # Two packages install a module called "hid": cython-hidapi ("pip install hidapi",
    # class hid.device) and the ctypes wrapper ("pip install hid", class hid.Device).
    HID_API = "cython" if hasattr(hid, "device") else ("ctypes" if hasattr(hid, "Device") else None)
except Exception:            # ImportError, or the ctypes wrapper failing to find hidapi
    hid = None
    HID_API = None


def _find_libusb():
    """Point pyusb at a bundled libusb-1.0 if there is one: next to this script,
    next to the packaged executable, or inside a PyInstaller bundle. Otherwise
    try the libusb-package wheel, and finally pyusb's normal system search."""
    dirs = []
    if getattr(sys, "frozen", False):
        dirs += [getattr(sys, "_MEIPASS", ""), os.path.dirname(sys.executable)]
    dirs.append(os.path.dirname(os.path.abspath(__file__)))
    names = ("libusb-1.0.dll", "libusb-1.0.0.dylib", "libusb-1.0.dylib", "libusb-1.0.so.0", "libusb-1.0.so")
    backend = None
    for d in dirs:
        for name in names:
            path = os.path.join(d, name) if d else ""
            if path and os.path.exists(path):
                try:
                    import usb.backend.libusb1 as libusb1
                    backend = libusb1.get_backend(find_library=lambda _name, p=path: p)
                except Exception:
                    backend = None
                if backend is not None:
                    break
        if backend is not None:
            break
    if backend is None:
        try:
            import libusb_package
            backend = libusb_package.get_libusb1_backend()
        except ImportError:
            pass
    global LIBUSB_BACKEND
    if backend is None:
        try:
            import usb.backend.libusb1 as libusb1
            backend_ok = libusb1.get_backend() is not None
        except Exception:
            backend_ok = False
        LIBUSB_BACKEND = "system" if backend_ok else None
        return
    LIBUSB_BACKEND = backend
    if backend is not None:
        _orig_find = usb.core.find

        def find(*args, **kwargs):
            kwargs.setdefault("backend", backend)
            return _orig_find(*args, **kwargs)
        usb.core.find = find


LIBUSB_BACKEND = None
if HAVE_USB:
    _find_libusb()

# TLAC checks for this header ("BVZ") on every input packet.
HEADER = bytes((0x42, 0x56, 0x5A))
PACKET_LEN = 24                          # TLAC's DivallerState is 24 bytes
LED_HEADER = bytes((0x44, 0x4C, 0x61))   # "DLa", start of TLAC's LED packet

# Where each Divaller button lives in the input packet: (byte, bit).
DIVALLER_BITS = {
    "fn": (3, 1), "square": (3, 2), "circle": (3, 3), "triangle": (3, 4),
    "cross": (3, 5), "l3": (4, 6), "l2": (4, 7), "l1": (5, 0),
}

# Extended buttons that the Divaller packet has no room for (sent via 'X' only).
EXT_BITS = ["up", "down", "left", "right", "lb", "rb", "lt", "rt", "ls", "rs"]
EXT = {name: 1 << i for i, name in enumerate(EXT_BITS)}

# Switch / PlayStation hat value (0 = up, clockwise, 8 = centred) -> D-pad bits.
HAT_TO_DPAD = {
    0: EXT["up"], 1: EXT["up"] | EXT["right"], 2: EXT["right"],
    3: EXT["down"] | EXT["right"], 4: EXT["down"], 5: EXT["down"] | EXT["left"],
    6: EXT["left"], 7: EXT["up"] | EXT["left"],
}

# Log sink: plain print normally; the TUI swaps in a scrolling buffer.
LOG = collections.deque(maxlen=200)
_log_to_buffer = False


def log(*args):
    msg = " ".join(str(a) for a in args)
    if _log_to_buffer:
        LOG.append(time.strftime("%H:%M:%S ") + msg)
    else:
        print(msg, flush=True)


def reverse32(v):
    return int(f"{v & 0xFFFFFFFF:032b}"[::-1], 2)


def build_divaller(pressed, sensors):
    """Build a 24-byte Divaller packet from a set of pressed button names and a
    32-bit mask where bit i is TLAC sensor i (0 = left)."""
    pkt = bytearray(HEADER + bytes(PACKET_LEN - 3))
    for name in pressed:
        byte, bit = DIVALLER_BITS[name]
        pkt[byte] |= 1 << bit
    # TLAC sensor i == bit i of (s5>>4 | s6<<4 | s7<<12 | s8<<20 | s9<<28).
    pkt[5] |= (sensors & 0x0F) << 4
    pkt[6] = sensors >> 4 & 0xFF
    pkt[7] = sensors >> 12 & 0xFF
    pkt[8] = sensors >> 20 & 0xFF
    pkt[9] = sensors >> 28 & 0x0F
    return bytes(pkt)


def decode(pkt):
    """Decode a Divaller input packet the same way TLAC's Divaller.cpp does.
    Returns (buttons dict, list of 32 slider sensors where index 0 is the
    sensor TLAC calls 0)."""
    s = (pkt or b"") + bytes(24)
    buttons = {
        "triangle": s[3] >> 4 & 1,
        "square":   s[3] >> 2 & 1,
        "cross":    s[3] >> 5 & 1,
        "circle":   s[3] >> 3 & 1,
        "fn":       s[3] >> 1 & 1,
        "l1":       s[5] & 1,
        "l2":       s[4] >> 7 & 1,
        "l3":       s[4] >> 6 & 1,
    }
    raw = (s[5] >> 4 | s[6] << 4 | s[7] << 12 | s[8] << 20 | s[9] << 28) & 0xFFFFFFFF
    rev = int(f"{raw:032b}"[::-1], 2)   # TLAC reverses the bit order
    # TLAC's TouchSliderEmulator: sensor i is bit (31 - i).
    sensors = [rev >> (31 - i) & 1 for i in range(32)]
    return buttons, sensors


def sticks_to_mask(axes, slider_reverse=False):
    """HORI arcade mode: LX, LY, RX, RY carry touched bits 0-7, 8-15, 16-23,
    24-31, each XOR 0x80. Touched bit 31 is the leftmost cell; returns a mask
    where bit i is TLAC sensor i (0 = leftmost)."""
    lx, ly, rx, ry = (b ^ 0x80 for b in axes)
    touched = lx | ly << 8 | rx << 16 | ry << 24
    return touched if slider_reverse else reverse32(touched)


# ------------------------------------------------------------------ device profiles

class Profile:
    """One controller type: how to find it and how to translate its packets."""
    key = name = ""
    vid = pid = 0
    ep_in = ep_out = 0
    min_in = 1
    led_note = ""
    is_hid = True          # HID-class device (can be read through hidapi)

    def __init__(self, slider_reverse=False, led_reverse=False):
        self.slider_reverse = slider_reverse
        self.led_reverse = led_reverse

    # Labels for the extended buttons on this controller (for the TUI).
    ext_labels = {}
    # Labels for the four small Divaller-equivalent buttons on this controller.
    fn_labels = {"fn": "FN", "l1": "L1", "l2": "L2", "l3": "L3"}

    def to_divaller(self, data):
        """Device IN report -> (24-byte Divaller packet, extended-button bits),
        or None to skip the report."""
        raise NotImplementedError

    def from_divaller_leds(self, dla):
        """100-byte Divaller LED packet -> device OUT report, or None to drop."""
        return None


class DivallerProfile(Profile):
    key, name = "divaller", "Divaller"
    vid, pid = 0x0E8F, 0x2213
    ep_in, ep_out = 0x84, 0x03
    is_hid = False         # vendor-specific (WinUSB) device: libusb only
    led_note = "slider + button LEDs"

    def to_divaller(self, data):
        return data, 0       # already native; the Divaller has no D-pad

    def from_divaller_leds(self, dla):
        return dla           # already native


class ZhouSensorProfile(Profile):
    """ZhouSensor / DIVA Pro in HDD mode.
    IN  (EP 0x81, 7 bytes): [0] bit0 triangle, bit1 square, bit2 circle,
        bit3 cross; [1] bits4-7 D-pad; [2] bit0 minus, bit1 plus, bit2 capture,
        bit3 home (top nibble board GPIO); [3..6] 32-bit slider mask, LE.
    OUT (EP 0x02, 48 bytes): 32 slider LEDs, 4 bits per channel."""
    key, name = "zhousensor", "ZhouSensor HDD"
    vid, pid = 0x1973, 0x8000
    ep_in, ep_out = 0x81, 0x02
    min_in = 7
    led_note = "slider LEDs (button LEDs are set in the board's menu)"
    BUTTONS = {   # Divaller name -> (byte, bit) in the 7-byte report
        "triangle": (0, 0), "square": (0, 1), "circle": (0, 2), "cross": (0, 3),
        "fn": (2, 1),        # plus    -> FN / start
        "l1": (2, 0),        # minus   -> L1 / coin
        "l2": (2, 2),        # capture -> L2 / service
        "l3": (2, 3),        # home    -> L3 / test
    }
    EXT_BUTTONS = {   # extended name -> (byte, bit)
        "up": (1, 4), "down": (1, 5), "left": (1, 6), "right": (1, 7),
        "lb": (0, 4), "rb": (1, 0), "lt": (0, 5), "rt": (1, 1), "ls": (0, 6), "rs": (1, 2),
    }
    ext_labels = {"lb": "L", "rb": "R", "lt": "ZL", "rt": "ZR", "ls": "LS", "rs": "RS"}
    fn_labels = {"fn": "+", "l1": "−", "l2": "◉", "l3": "⌂"}

    def to_divaller(self, data):
        pressed = [n for n, (b, bit) in self.BUTTONS.items() if data[b] >> bit & 1]
        ext = 0
        for n, (b, bit) in self.EXT_BUTTONS.items():
            if data[b] >> bit & 1:
                ext |= EXT[n]
        mask = int.from_bytes(data[3:7], "little")
        if self.slider_reverse:
            mask = reverse32(mask)
        return build_divaller(pressed, mask), ext

    def from_divaller_leds(self, dla):
        """TLAC puts sensor-n's colour at 4 + 3*(31-n) as G, R, B. The firmware
        unpacks each 3-byte group i into
            LED 2i:   R = b0 high nibble, G = b0 low nibble, B = b1 low nibble
            LED 2i+1: G = b1 high nibble, B = b2 high nibble, R = b2 low nibble"""
        if len(dla) < 100 or dla[:3] != LED_HEADER:
            return None
        rgb = []
        for n in range(32):
            o = 4 + 3 * (31 - n)
            g, r, b = dla[o], dla[o + 1], dla[o + 2]
            rgb.append((r >> 4, g >> 4, b >> 4))
        if self.led_reverse:
            rgb.reverse()
        out = bytearray(48)
        for i in range(16):
            (r0, g0, b0), (r1, g1, b1) = rgb[2 * i], rgb[2 * i + 1]
            out[3 * i] = r0 << 4 | g0
            out[3 * i + 1] = g1 << 4 | b0
            out[3 * i + 2] = b1 << 4 | r1
        return bytes(out)


class HoriProfile(Profile):
    """HORI NSW-230 (Project DIVA Mega39's controller) in arcade mode.
    IN (EP 0x81, 8 bytes, Switch HORI pad layout):
        [0..1] buttons, little-endian: 0 Y, 1 B, 2 A, 3 X, 4 L, 5 R, 6 ZL, 7 ZR,
               8 minus, 9 plus, 10 L3, 11 R3, 12 home, 13 capture
        [2]    hat (0-7, 8 = centred)
        [3..6] LX, LY, RX, RY: arcade mode packs the 32 touch cells into these,
               each byte XOR 0x80 (so an untouched slider reads as centred sticks)
        [7]    vendor byte
    The decoding matches DivaCon2040's NSW-230 emulation; Mega Mix+ decodes the
    real controller the same way. No LED control."""
    key, name = "hori", "HORI NSW-230"
    vid, pid = 0x0F0D, 0x00FB
    ep_in, ep_out = 0x81, 0x02
    min_in = 7
    led_note = "no LED control (the HORI lights itself)"
    BUTTONS = {   # Divaller name -> Switch button bit
        "triangle": 3,   # X (north)
        "square": 0,     # Y (west)
        "cross": 1,      # B (south)
        "circle": 2,     # A (east)
        "fn": 9,         # plus    -> FN / start
        "l1": 8,         # minus   -> L1 / coin
        "l2": 13,        # capture -> L2 / service
        "l3": 12,        # home    -> L3 / test
    }
    EXT_BUTTONS = {"lb": 4, "rb": 5, "lt": 6, "rt": 7, "ls": 10, "rs": 11}
    ext_labels = {"lb": "L", "rb": "R", "lt": "ZL", "rt": "ZR", "ls": "LS", "rs": "RS"}
    fn_labels = {"fn": "+", "l1": "−", "l2": "◉", "l3": "⌂"}

    def to_divaller(self, data):
        buttons = data[0] | data[1] << 8
        pressed = [n for n, bit in self.BUTTONS.items() if buttons >> bit & 1]
        ext = HAT_TO_DPAD.get(data[2] & 0x0F, 0)
        for n, bit in self.EXT_BUTTONS.items():
            if buttons >> bit & 1:
                ext |= EXT[n]
        return build_divaller(pressed, sticks_to_mask(data[3:7], self.slider_reverse)), ext


class HoriPs4Profile(Profile):
    """HORI PS4-161 (Project DIVA Future Tone DX controller) in arcade mode.
    IN (EP 0x81, DualShock 4 layout, report ID 0x01):
        [0]    report ID 0x01
        [1..4] LX, LY, RX, RY: arcade-mode slider, each byte XOR 0x80
        [5]    bits 0-3 hat (0-7, 8 = centred), 4 square, 5 cross, 6 circle, 7 triangle
        [6]    0 L1, 1 R1, 2 L2, 3 R2, 4 share, 5 options, 6 L3, 7 R3
        [7]    0 PS, 1 touchpad click, 2-7 report counter
    Layout as DivaCon2040's PS4-161 emulation / the Linux hid-playstation driver.
    No LED control."""
    key, name = "hori_ps4", "HORI PS4-161"
    vid, pid = 0x0F0D, 0x013C
    ep_in, ep_out = 0x84, 0x03   # DS4-style; the bridge falls back to whatever IN/OUT endpoints exist
    min_in = 8
    led_note = "no LED control (the HORI lights itself)"
    BUTTONS = {   # Divaller name -> (byte, bit)
        "square": (5, 4), "cross": (5, 5), "circle": (5, 6), "triangle": (5, 7),
        "fn": (6, 5),        # options -> FN / start
        "l1": (6, 4),        # share   -> L1 / coin
        "l2": (7, 1),        # touchpad click -> L2 / service
        "l3": (7, 0),        # PS      -> L3 / test
    }
    EXT_BUTTONS = {"lb": (6, 0), "rb": (6, 1), "lt": (6, 2), "rt": (6, 3), "ls": (6, 6), "rs": (6, 7)}
    ext_labels = {"lb": "L1", "rb": "R1", "lt": "L2", "rt": "R2", "ls": "L3", "rs": "R3"}
    fn_labels = {"fn": "OPT", "l1": "SHR", "l2": "TP", "l3": "PS"}

    def to_divaller(self, data):
        if data[0] != 0x01:
            return None          # not an input report
        pressed = [n for n, (b, bit) in self.BUTTONS.items() if data[b] >> bit & 1]
        ext = HAT_TO_DPAD.get(data[5] & 0x0F, 0)
        for n, (b, bit) in self.EXT_BUTTONS.items():
            if data[b] >> bit & 1:
                ext |= EXT[n]
        return build_divaller(pressed, sticks_to_mask(data[1:5], self.slider_reverse)), ext


PROFILES = [DivallerProfile, ZhouSensorProfile, HoriProfile, HoriPs4Profile]
PROFILE_KEYS = [p.key for p in PROFILES]

# Recognised but unusable configurations, so we can say what's wrong.
WRONG_MODE = {
    (0x1973, 0x1020): "ZhouSensor in Keyboard mode - switch it to HDD mode",
    (0x0F0D, 0x0092): "HORI-type pad 0F0D:0092 (also ZhouSensor NS mode) - "
                      "for a ZhouSensor, switch it to HDD mode",
}


# ------------------------------------------------------------------ transports

class TransportError(Exception):
    """The controller went away or failed; the reader drops it and rescans."""


class LibusbTransport:
    """Direct USB access through pyusb / libusb-1.0."""
    name = "libusb"

    def __init__(self, dev, prof, verbose=False):
        self.dev = dev
        self.verbose = verbose
        self.ep_in, self.ep_out = self._open(dev, prof)

    @staticmethod
    def find(vid, pid):
        if not HAVE_USB:
            return None
        try:
            return usb.core.find(idVendor=vid, idProduct=pid)
        except usb.core.NoBackendError:
            return None

    @staticmethod
    def _step(what, fn):
        try:
            return fn()
        except usb.core.USBError as e:
            raise usb.core.USBError(f"{what}: {e}") from e

    def _open(self, dev, prof):
        try:
            cfg = dev.get_active_configuration()
        except usb.core.USBError:
            cfg = None
        if cfg is None:
            self._step("setting configuration", dev.set_configuration)
            cfg = dev.get_active_configuration()

        # Prefer the interface that has the expected IN endpoint; otherwise take
        # the first interface with any IN endpoint (firmware revisions differ).
        intf = fallback = None
        for candidate in cfg:
            eps = [ep.bEndpointAddress for ep in candidate]
            if self.verbose:
                log(f"  interface {candidate.bInterfaceNumber} alt {candidate.bAlternateSetting}: "
                    f"class 0x{candidate.bInterfaceClass:02x}, endpoints "
                    + ", ".join(f"0x{e:02x}" for e in eps))
            if intf is None and prof.ep_in in eps:
                intf = candidate
            if fallback is None and any(e & 0x80 for e in eps):
                fallback = candidate
        intf = intf or fallback
        if intf is None:
            raise usb.core.USBError("no interface has an IN endpoint")
        eps = [ep.bEndpointAddress for ep in intf]
        ep_in = prof.ep_in if prof.ep_in in eps else next(e for e in eps if e & 0x80)
        outs = [e for e in eps if not e & 0x80]
        ep_out = prof.ep_out if prof.ep_out in outs else (outs[0] if outs else None)

        n = intf.bInterfaceNumber
        try:
            if dev.is_kernel_driver_active(n):
                self._step(f"detaching kernel driver from interface {n}",
                           lambda: dev.detach_kernel_driver(n))
        except (NotImplementedError, usb.core.USBError):
            pass
        self._step(f"claiming interface {n}", lambda: usb.util.claim_interface(dev, n))
        if intf.bAlternateSetting != 0:
            self._step(f"selecting alt setting {intf.bAlternateSetting}",
                       lambda: dev.set_interface_altsetting(n, intf.bAlternateSetting))
        log(f"Using interface {n}, IN 0x{ep_in:02x}"
            + (f", OUT 0x{ep_out:02x}" if ep_out is not None else ", no OUT endpoint"))
        return ep_in, ep_out

    @property
    def can_write(self):
        return self.ep_out is not None

    def read(self, timeout_ms=100):
        try:
            return bytes(self.dev.read(self.ep_in, 64, timeout=timeout_ms))
        except usb.core.USBTimeoutError:
            return None
        except usb.core.USBError as e:
            raise TransportError(e) from e

    def write(self, report):
        self.dev.write(self.ep_out, report, timeout=100)

    def close(self):
        try:
            usb.util.dispose_resources(self.dev)
        except usb.core.USBError:
            pass


class HidTransport:
    """Access through the operating system's HID driver (hidapi). Reports come
    back exactly as on the wire: with the report ID first when the device uses
    report IDs (the PS4-161's 0x01), without one otherwise."""
    name = "hidapi"

    def __init__(self, info, prof, verbose=False):
        self.verbose = verbose
        path = info["path"]
        if HID_API == "cython":
            self.dev = hid.device()
            self.dev.open_path(path)
        else:
            self.dev = hid.Device(path=path)
        log(f"Using HID interface {info.get('interface_number', -1)}, usage page "
            f"0x{info.get('usage_page', 0):04x} usage 0x{info.get('usage', 0):02x}")

    @staticmethod
    def find(vid, pid):
        """Pick the HID collection to open: a gamepad/joystick collection if there
        is one, else the first one (the ZhouSensor uses a vendor usage page)."""
        if HID_API is None:
            return None
        try:
            infos = hid.enumerate(vid, pid)
        except Exception:
            return None
        if not infos:
            return None

        def rank(i):
            game = i.get("usage_page") == 0x01 and i.get("usage") in (0x04, 0x05)
            return (0 if game else 1, i.get("interface_number", 0))
        return sorted(infos, key=rank)[0]

    @property
    def can_write(self):
        return True

    def read(self, timeout_ms=100):
        try:
            if HID_API == "cython":
                data = self.dev.read(64, timeout_ms)
            else:
                data = self.dev.read(64, timeout_ms)
        except (OSError, IOError, ValueError) as e:
            raise TransportError(e) from e
        except Exception as e:                    # hid.HIDException (ctypes wrapper)
            raise TransportError(e) from e
        if not data:
            return None
        return bytes(data)

    def write(self, report):
        # Report ID 0 prefix = "this device doesn't use report IDs".
        n = self.dev.write(b"\x00" + bytes(report))
        if isinstance(n, int) and n < 0:
            raise OSError("HID write failed")

    def close(self):
        try:
            self.dev.close()
        except Exception:
            pass


# ------------------------------------------------------------------ device

class Device:
    """Keeps the newest (translated) input packet and the newest LED packet, so
    the game never waits on USB: reads return the latest state, writes are
    queued. Presents whatever controller is attached as a Divaller."""

    def __init__(self, choice="auto", verbose=False, slider_reverse=False,
                 led_reverse=False, leds=True, backend="auto"):
        self.verbose = verbose
        self.leds = leds
        self.backend = backend
        keys = PROFILE_KEYS if choice == "auto" else [choice]
        self.candidates = [p(slider_reverse, led_reverse) for p in PROFILES if p.key in keys]
        self.lock = threading.Lock()
        self.dev = None             # the open transport
        self.profile = None
        self.latest = None          # newest translated IN packet (bytes)
        self.latest_ext = 0         # newest extended-button bits
        self.raw = None             # newest raw device report, for the TUI
        self.pending_out = None     # newest OUT packet waiting to be sent
        self.last_report = None     # last LED report sent to the device
        self.out_event = threading.Event()
        self.packets = 0            # IN packets received, for the rate display
        self.clients = 0            # connected game instances
        self._warned = set()
        self._report_backends()
        threading.Thread(target=self._reader, daemon=True).start()
        threading.Thread(target=self._writer, daemon=True).start()

    def _report_backends(self):
        have = []
        if HAVE_USB and LIBUSB_BACKEND is not None:
            have.append("libusb (pyusb)")
        elif HAVE_USB:
            log("pyusb is installed but the libusb-1.0 library wasn't found "
                "(pip install libusb-package, or see the README)")
        if HID_API:
            have.append(f"hidapi ({'pip hidapi' if HID_API == 'cython' else 'pip hid'})")
        log("USB access available: " + (", ".join(have) if have else "none!"))
        if not have:
            log("install one:  pip install pyusb   (plus libusb-1.0)   and/or   pip install hidapi")
        elif self.backend == "hidapi" and not HID_API:
            log("--backend hidapi requested but hidapi isn't installed: pip install hidapi")
        elif self.backend == "libusb" and not HAVE_USB:
            log("--backend libusb requested but pyusb isn't installed: pip install pyusb")
        if not HAVE_USB and any(not p.is_hid for p in self.candidates):
            log("note: the Divaller isn't a HID device and needs libusb: pip install pyusb "
                "(plus libusb-1.0)")

    @property
    def present(self):
        return self.dev is not None

    @property
    def name(self):
        p = self.profile
        return p.name if p else "none"

    def _order(self, prof):
        """Transports to try for this controller, best first."""
        if self.backend == "libusb":
            return [LibusbTransport]
        if self.backend == "hidapi":
            return [HidTransport] if prof.is_hid else [LibusbTransport]
        if prof.is_hid and sys.platform in ("win32", "darwin"):
            return [HidTransport, LibusbTransport]
        return [LibusbTransport, HidTransport] if prof.is_hid else [LibusbTransport]

    def _find_and_open(self):
        for prof in self.candidates:
            errors = []
            for transport in self._order(prof):
                handle = transport.find(prof.vid, prof.pid)
                if handle is None:
                    continue
                try:
                    return prof, transport(handle, prof, self.verbose)
                except Exception as e:
                    errors.append((transport.name, e))
            for tname, e in errors:
                hint = ""
                text = str(e)
                if getattr(e, "errno", None) == 13 or "Access denied" in text or "access" in text.lower():
                    hint = (" (check the udev rule / permissions)" if sys.platform.startswith("linux")
                            else " (try the other USB access mode; on Windows libusb needs the WinUSB driver)")
                elif "busy" in text.lower():
                    hint = " (something else has claimed it)"
                log(f"Found the {prof.name} but couldn't open it via {tname}: {e}{hint}")
            if errors:
                time.sleep(4)  # don't spam the same error every second
                return None, None
        self._warn_wrong_mode()
        return None, None

    def _warn_wrong_mode(self):
        for (vid, pid), msg in WRONG_MODE.items():
            if (vid, pid) in self._warned:
                continue
            if LibusbTransport.find(vid, pid) is not None or HidTransport.find(vid, pid) is not None:
                log(f"found {vid:04X}:{pid:04X}: {msg}")
                self._warned.add((vid, pid))

    def _drop(self, why):
        log(f"{self.name} lost: {why}")
        with self.lock:
            dev, self.dev, self.latest = self.dev, None, None
            self.latest_ext = 0
            self.raw = self.last_report = None
            self.profile = None
        if dev is not None:
            dev.close()

    def _reader(self):
        while True:
            if self.dev is None:
                prof, dev = self._find_and_open()
                if dev is None:
                    time.sleep(1)
                    continue
                with self.lock:
                    self.dev, self.profile = dev, prof
                log(f"{prof.name} connected ({prof.vid:04X}:{prof.pid:04X} via {dev.name}; {prof.led_note})")
            prof, dev = self.profile, self.dev
            try:
                data = dev.read(100)
            except TransportError as e:
                self._drop(e)
                continue
            if data is None:
                continue
            if len(data) < prof.min_in:
                if self.verbose:
                    log(f"short report: {data.hex()}")
                continue
            result = prof.to_divaller(data)
            if result is None:
                continue
            pkt, ext = result
            if pkt[:3] != HEADER and self.verbose:
                log(f"unexpected packet: {pkt.hex()}")
            with self.lock:
                self.raw = data
                self.latest = pkt
                self.latest_ext = ext
                self.packets += 1

    def _writer(self):
        while True:
            self.out_event.wait()
            self.out_event.clear()
            with self.lock:
                data, self.pending_out = self.pending_out, None
                dev, prof = self.dev, self.profile
            if data is None or dev is None or prof is None or not dev.can_write or not self.leds:
                continue
            report = prof.from_divaller_leds(data)
            if report is None:
                continue
            if report == self.last_report and prof.key != "divaller":
                continue            # don't resend identical frames
            try:
                dev.write(report)
                self.last_report = report
            except Exception as e:
                if self.verbose:
                    log(f"LED write failed: {e}")

    def read(self, max_len):
        with self.lock:
            if self.dev is None:
                return None
            if self.latest is None:
                # Nothing received yet: report "header, nothing pressed".
                return (HEADER + bytes(PACKET_LEN - 3))[:max_len]
            return self.latest[:max_len]

    def read_ext(self, max_len):
        """Divaller packet (24 bytes) followed by 2 bytes of extended buttons."""
        with self.lock:
            if self.dev is None:
                return None
            pkt = self.latest if self.latest is not None else HEADER + bytes(PACKET_LEN - 3)
            pkt = (pkt + bytes(PACKET_LEN))[:PACKET_LEN]
            return (pkt + self.latest_ext.to_bytes(2, "little"))[:max_len]

    def write(self, data):
        with self.lock:
            if self.dev is None:
                return False
            self.pending_out = data
        self.out_event.set()
        return True        # accepted (dropped silently for devices without LEDs)


def make_handler(device):
    class Handler(socketserver.BaseRequestHandler):
        def recv_exact(self, n):
            buf = b""
            while len(buf) < n:
                chunk = self.request.recv(n - len(buf))
                if not chunk:
                    raise ConnectionError
                buf += chunk
            return buf

        def handle(self):
            self.request.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
            log(f"game connected from {self.client_address[0]}")
            device.clients += 1
            try:
                while True:
                    op = self.recv_exact(1)
                    if op == b"P":
                        self.request.sendall(b"\x01" if device.present else b"\x00")
                    elif op == b"R":
                        _ep, max_len = self.recv_exact(2)
                        data = device.read(max_len)
                        if data is None:
                            self.request.sendall(b"\x00\x00")
                        else:
                            self.request.sendall(bytes((1, len(data))) + data)
                    elif op == b"X":
                        _ep, max_len = self.recv_exact(2)
                        data = device.read_ext(max_len)
                        if data is None:
                            self.request.sendall(b"\x00\x00")
                        else:
                            self.request.sendall(bytes((1, len(data))) + data)
                    elif op == b"W":
                        _ep, length = self.recv_exact(2)
                        data = self.recv_exact(length)
                        self.request.sendall(b"\x01" if device.write(data) else b"\x00")
                    else:
                        log(f"unknown opcode {op!r}, dropping client")
                        return
            except (ConnectionError, OSError):
                pass
            device.clients -= 1
            log("game disconnected")

    return Handler


class Server(socketserver.ThreadingTCPServer):
    # On Windows SO_REUSEADDR lets a second bridge silently share the port, so
    # claim it exclusively there instead.
    allow_reuse_address = sys.platform != "win32"
    daemon_threads = True

    def server_bind(self):
        if sys.platform == "win32" and hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
            self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        super().server_bind()


# ------------------------------------------------------------------ TUI

FACE = [  # left to right, as on the controller
    ("triangle", "△", "Triangle", "green"),
    ("square",   "□", "Square",   "magenta"),
    ("cross",    "✕", "Cross",    "blue"),
    ("circle",   "○", "Circle",   "red"),
]
SMALL = [("l1", "L1", "coin"), ("l2", "L2", "service"), ("l3", "L3", "test")]
HORI_KEYS = ("hori", "hori_ps4")
# Large outline symbols for the HORI-style buttons (3 rows each).
GLYPHS = {
    "triangle": ["╱╲", "╱  ╲", "╱▁▁▁▁╲"],
    "square":   ["┏━━━━┓", "┃    ┃", "┗━━━━┛"],
    "cross":    ["╲  ╱", "╳", "╱  ╲"],
    "circle":   ["╭──╮", "│    │", "╰──╯"],
}


def tui(stdscr, device, listen):
    import curses

    curses.curs_set(0)
    stdscr.nodelay(True)
    stdscr.timeout(16)  # ~60 fps

    pairs = {}
    if curses.has_colors():
        curses.start_color()
        try:
            curses.use_default_colors()
            bg = -1
        except curses.error:
            bg = curses.COLOR_BLACK
        for n, (name, col) in enumerate([
                ("green", curses.COLOR_GREEN), ("magenta", curses.COLOR_MAGENTA),
                ("blue", curses.COLOR_BLUE), ("red", curses.COLOR_RED),
                ("cyan", curses.COLOR_CYAN), ("yellow", curses.COLOR_YELLOW),
                ("white", curses.COLOR_WHITE)], start=1):
            curses.init_pair(n, col, bg)
            pairs[name] = curses.color_pair(n)

    def color(name):
        return pairs.get(name, 0)

    def put(y, x, text, attr=0):
        try:
            stdscr.addstr(y, x, text, attr)
        except curses.error:
            pass  # off-screen on a small terminal

    def lit(on, col="white"):
        return color(col) | (curses.A_REVERSE | curses.A_BOLD if on else curses.A_DIM)

    # ---------------------------------------------------------- classic layout
    def draw_classic(prof, buttons, sensors, ext):
        W, x0 = 76, 1
        put(0, x0, "╭" + "─" * (W - 2) + "╮", color("cyan"))
        for y in range(1, 13):
            put(y, x0, "│", color("cyan"))
            put(y, x0 + W - 1, "│", color("cyan"))
        put(13, x0, "╰" + "─" * (W - 2) + "╯", color("cyan"))
        put(0, x0 + 3, f" {(prof.name if prof else 'NO CONTROLLER').upper()} ", curses.A_BOLD)

        # Small buttons (top left) and FN (top right)
        x = x0 + 3
        labels = prof.fn_labels if prof else Profile.fn_labels
        for key_, label, role in SMALL:
            on = buttons[key_]
            attr = color("magenta") | (curses.A_REVERSE | curses.A_BOLD if on else 0)
            text = labels.get(key_, label)
            put(1, x, f" {'●' if on else '○'} {text} ", attr)
            put(1, x + len(text) + 5, role, curses.A_DIM)
            x += len(text) + 5 + len(role) + 2
        on = buttons["fn"]
        attr = color("yellow") | (curses.A_REVERSE | curses.A_BOLD if on else 0)
        put(1, x0 + W - 18, f" {'●' if on else '○'} {labels.get('fn', 'FN')} ", attr)
        put(1, x0 + W - 10, "start", curses.A_DIM)

        # Slider: 32 sensors, 2 columns each
        sx = x0 + 4
        put(3, sx + 27, "TOUCH SLIDER", curses.A_DIM)
        put(4, sx, "L ▕", curses.A_BOLD)
        for i, on in enumerate(sensors):
            if on:
                put(4, sx + 3 + i * 2, "██", color("cyan") | curses.A_BOLD)
            else:
                put(4, sx + 3 + i * 2, "··" if i % 4 else "╎·", curses.A_DIM)
        put(4, sx + 3 + 64, "▏ R", curses.A_BOLD)
        for i in (0, 8, 16, 24, 31):
            put(5, sx + 3 + i * 2, str(i), curses.A_DIM)
        touched = sum(sensors)
        bits = "".join(str(b) for b in sensors)
        put(6, sx + 3, " ".join(bits[i:i + 8] for i in range(0, 32, 8)), curses.A_DIM)
        put(6, sx + 3 + 40, f"{touched:2d}/32 touched", curses.A_DIM)

        # Face buttons
        bx = x0 + 9
        for key_, sym, name, col in FACE:
            on = buttons[key_]
            box = color(col) | (curses.A_BOLD if on else 0)
            fill = color(col) | curses.A_REVERSE | curses.A_BOLD if on else color(col)
            put(8, bx, "╭───────╮", box)
            put(9, bx, "│", box)
            put(9, bx + 1, f"   {sym}   ", fill)
            put(9, bx + 8, "│", box)
            put(10, bx, "╰───────╯", box)
            put(11, bx + (9 - len(name)) // 2, name, curses.A_BOLD if on else curses.A_DIM)
            bx += 16

        # Extended buttons (Mega Mix+ only)
        if prof is not None and prof.ext_labels:
            x = x0 + 3
            put(12, x, "D-pad", curses.A_DIM)
            x += 6
            for n, sym in (("up", "↑"), ("down", "↓"), ("left", "←"), ("right", "→")):
                put(12, x, f" {sym} ", lit(ext & EXT[n], "yellow"))
                x += 4
            x += 2
            for n in ("lb", "rb", "lt", "rt", "ls", "rs"):
                label = prof.ext_labels.get(n, n.upper())
                put(12, x, f" {label} ", lit(ext & EXT[n], "magenta"))
                x += len(label) + 3
            put(12, x0 + W - 15, "(Mega Mix+)", curses.A_DIM)
        return 14

    # ------------------------------------------------- HORI arcade-style panel
    def draw_hori(prof, buttons, sensors, ext):
        """Modelled on the NSW-230 / PS4-161: a black top strip with the D-pad
        and small system buttons, a glass TOUCH SLIDER bar with arrows at each
        end, and four big round buttons with coloured rings."""
        W, x0 = 76, 1
        body = color("white")
        # Top strip
        put(0, x0, "▗" + "▄" * (W - 2) + "▖", body)
        put(1, x0, "▐", body)
        put(1, x0 + W - 1, "▌", body)
        put(2, x0, "▐", body)
        put(2, x0 + W - 1, "▌", body)
        put(3, x0, "▐", body)
        put(3, x0 + W - 1, "▌", body)

        # Left: small "display" window with the controller name and mode
        put(1, x0 + 3, "┌──────────────┐", curses.A_DIM)
        put(2, x0 + 3, "│", curses.A_DIM)
        put(2, x0 + 4, f"{prof.name[5:]:^14}"[:14], color("cyan") | curses.A_BOLD)
        put(2, x0 + 18, "│", curses.A_DIM)
        put(3, x0 + 3, "└──ARCADE MODE─┘", curses.A_DIM)

        # Centre: D-pad cross
        dx = x0 + 23
        put(1, dx + 2, "▲", lit(ext & EXT["up"], "yellow"))
        put(2, dx, "◀", lit(ext & EXT["left"], "yellow"))
        put(2, dx + 2, "✚", curses.A_DIM)
        put(2, dx + 4, "▶", lit(ext & EXT["right"], "yellow"))
        put(3, dx + 2, "▼", lit(ext & EXT["down"], "yellow"))

        # Right: row of small round system buttons
        sx = dx + 9
        small = [(prof.fn_labels["l1"], buttons["l1"]), (prof.fn_labels["fn"], buttons["fn"]),
                 (prof.fn_labels["l3"], buttons["l3"]), (prof.fn_labels["l2"], buttons["l2"])]
        small += [(prof.ext_labels[n], ext & EXT[n]) for n in ("lb", "rb", "lt", "rt", "ls", "rs")]
        x = sx
        for label, on in small:
            put(1, x, "●" if on else "○", color("yellow") | curses.A_BOLD if on else curses.A_DIM)
            put(2, x - (len(label) - 1) // 2, label, curses.A_BOLD if on else curses.A_DIM)
            x += 4
        fl = prof.fn_labels
        put(3, sx, f"{fl['fn']} start  {fl['l1']} select  {fl['l3']} test  {fl['l2']} service"[:W - sx - 2],
            curses.A_DIM)

        # Glass touch-slider bar
        gy = 4
        put(gy, x0, "▐" + "▀" * (W - 2) + "▌", color("cyan"))
        put(gy + 1, x0, "▐", color("cyan"))
        put(gy + 1, x0 + W - 1, "▌", color("cyan"))
        put(gy + 1, x0 + 2, "◀", color("cyan") | curses.A_BOLD)
        put(gy + 1, x0 + W - 3, "▶", color("cyan") | curses.A_BOLD)
        put(gy + 1, x0 + 25, "T O U C H     S L I D E R", color("cyan") | curses.A_DIM)
        put(gy + 2, x0, "▐", color("cyan"))
        put(gy + 2, x0 + W - 1, "▌", color("cyan"))
        cx = x0 + 6
        for i, on in enumerate(sensors):
            if on:
                put(gy + 2, cx + i * 2, "██", color("white") | curses.A_BOLD)
            else:
                put(gy + 2, cx + i * 2, "▁▁" if i % 8 else "▏▁", color("cyan") | curses.A_DIM)
        put(gy + 3, x0, "▐" + "▄" * (W - 2) + "▌", color("cyan"))
        put(gy + 1, x0 + W - 19, f"{sum(sensors):2d}/32 touched", color("cyan") | curses.A_DIM)

        # Four big round buttons with coloured rings
        by = gy + 5
        bx = x0 + 2
        for key_, sym, name, col in FACE:
            on = buttons[key_]
            ring = color(col) | curses.A_BOLD
            # Pressed: the whole face lights up in the button colour.
            inner = color(col) | (curses.A_REVERSE | curses.A_BOLD if on else curses.A_BOLD)
            put(by,     bx, "  ▄█▀▀▀▀▀▀▀█▄  ", ring)
            for r in range(3):
                put(by + 1 + r, bx, " █", ring)
                put(by + 1 + r, bx + 2, f"{GLYPHS[key_][r]:^11}", inner)
                put(by + 1 + r, bx + 13, "█ ", ring)
            put(by + 4, bx, "  ▀█▄▄▄▄▄▄▄█▀  ", ring)
            put(by + 5, bx + (15 - len(name)) // 2, name, curses.A_BOLD if on else curses.A_DIM)
            bx += 19
        # Front lip of the body
        put(by + 6, x0, "▝" + "▀" * (W - 2) + "▘", body)
        return by + 7

    rate, last_count, last_t = 0.0, 0, time.monotonic()

    while True:
        key = stdscr.getch()
        if key in (ord("q"), ord("Q"), 27):
            return

        now = time.monotonic()
        if now - last_t >= 0.5:
            rate = (device.packets - last_count) / (now - last_t)
            last_count, last_t = device.packets, now

        with device.lock:
            pkt = device.latest
            ext = device.latest_ext
            raw_dev = device.raw
            present = device.dev is not None
            prof = device.profile
        buttons, sensors = decode(pkt)

        stdscr.erase()
        W, x0 = 76, 1
        if prof is not None and prof.key in HORI_KEYS:
            y = draw_hori(prof, buttons, sensors, ext)
        else:
            y = draw_classic(prof, buttons, sensors, ext)

        # Status
        dev_txt = "connected" if present else "not found"
        put(y, x0 + 1, "Device: ", curses.A_DIM)
        put(y, x0 + 11, dev_txt, color("green" if present else "red") | curses.A_BOLD)
        game_txt = f"connected ({device.clients})" if device.clients else "waiting"
        put(y, x0 + 24, "Game: ", curses.A_DIM)
        put(y, x0 + 30, game_txt, color("green" if device.clients else "yellow") | curses.A_BOLD)
        put(y, x0 + 48, f"{rate:6.0f} pkt/s   {listen}", curses.A_DIM)
        if pkt and raw_dev and prof and prof.key != "divaller":
            raw = "dev " + raw_dev[:12].hex(" ") + " | dvl " + pkt[:10].hex(" ")
        else:
            raw = pkt.hex(" ") if pkt else "(no packet yet)"
        put(y + 1, x0 + 1, "raw: " + raw[:W - 7], curses.A_DIM)
        put(y + 2, x0 + 1, "q to quit", curses.A_DIM)

        # Log tail
        h, _ = stdscr.getmaxyx()
        top = y + 4
        lines = list(LOG)[-(max(0, h - top)):] if h > top else []
        for n, line in enumerate(lines):
            put(top + n, x0 + 1, line[:W])

        stdscr.refresh()


def _parent_alive(pid):
    if sys.platform == "win32":
        import ctypes
        k32 = ctypes.windll.kernel32
        SYNCHRONIZE, WAIT_TIMEOUT = 0x00100000, 0x102
        h = k32.OpenProcess(SYNCHRONIZE, False, pid)
        if not h:
            return False
        try:
            return k32.WaitForSingleObject(h, 0) == WAIT_TIMEOUT
        finally:
            k32.CloseHandle(h)
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True


def watch_parent(pid):
    """Exit when the launching app goes away (crash, force-quit), so no bridge is
    left behind holding the port."""
    def run():
        while True:
            time.sleep(1)
            if not _parent_alive(pid):
                log("launcher exited - stopping bridge")
                os._exit(0)
    threading.Thread(target=run, daemon=True).start()


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--device", choices=["auto"] + PROFILE_KEYS, default="auto",
                    help="controller to use (default auto: first found of "
                         + ", ".join(PROFILE_KEYS) + ")")
    ap.add_argument("--backend", choices=["auto", "libusb", "hidapi"], default="auto",
                    help="USB access: hidapi (OS HID driver) or libusb; auto picks per platform")
    ap.add_argument("--host", default="127.0.0.1",
                    help="address to listen on (default 127.0.0.1; 0.0.0.0 for all interfaces)")
    ap.add_argument("--port", type=int, default=45710)
    ap.add_argument("--verbose", action="store_true")
    ap.add_argument("--tui", action="store_true",
                    help="show a live diagram of the buttons and slider")
    ap.add_argument("--slider-reverse", action="store_true",
                    help="flip the slider's touch direction (ZhouSensor / HORI) if left/right come out swapped")
    ap.add_argument("--led-reverse", action="store_true",
                    help="flip the slider LED order (ZhouSensor) if the lights come out mirrored")
    ap.add_argument("--no-leds", action="store_true",
                    help="don't forward LEDs to the controller")
    ap.add_argument("--parent-pid", type=int, help=argparse.SUPPRESS)   # set by the GUI
    args = ap.parse_args()
    if args.parent_pid:
        watch_parent(args.parent_pid)

    global _log_to_buffer
    _log_to_buffer = args.tui

    device = Device(args.device, args.verbose, args.slider_reverse, args.led_reverse, not args.no_leds,
                    args.backend)
    listen = f"{args.host}:{args.port}"
    try:
        srv = Server((args.host, args.port), make_handler(device))
    except OSError as e:
        log(f"Can't listen on {listen}: {e}")
        if e.errno in (48, 98, 10048) or getattr(e, "winerror", None) in (10013, 10048):
            log("Address already in use - another bridge (or program) has this port. "
                "Stop it or pick another port.")
        sys.exit(2)
    with srv:
        log(f"DIVA bridge listening on {listen} (looking for: "
            + ", ".join(p.name for p in device.candidates) + ")")
        if args.host not in ("127.0.0.1", "localhost", "::1"):
            log("note: the bridge has no authentication; only expose it on a network you trust")
        if not args.tui:
            try:
                srv.serve_forever()
            except KeyboardInterrupt:
                pass
            return

        import curses
        import locale
        locale.setlocale(locale.LC_ALL, "")
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        try:
            curses.wrapper(tui, device, listen)
        except KeyboardInterrupt:
            pass
        finally:
            _log_to_buffer = False
            srv.shutdown()


if __name__ == "__main__":
    main()
