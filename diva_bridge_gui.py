#!/usr/bin/env python3
"""
DIVA Bridge GUI - a Windows / Linux / macOS front end for diva_bridge.py.

Starts diva_bridge.py (in the same folder) with the chosen listen address,
port and options, shows its log, and draws the controller's buttons and touch
slider live once the server is up. The bridge itself is unchanged: the GUI
watches its log for the attached controller and reads the input state over the
bridge's own TCP protocol, like any other client.

    pip install pyusb hidapi     # USB access for the bridge (see README)
    python3 diva_bridge_gui.py

Only the Python standard library (Tkinter) is needed for the window itself.
"""

import os
import sys

# Bridge mode: the packaged app runs itself with --bridge to start the server.
# Handled before Tkinter is imported, so the bridge also works without a display
# (e.g.  "diva-bridge --bridge --tui"  over SSH).
if __name__ == "__main__" and "--bridge" in sys.argv:
    sys.argv.remove("--bridge")
    import diva_bridge
    diva_bridge.main()
    sys.exit(0)

import json
import queue
import re
import socket
import subprocess
import threading
import time
import tkinter as tk
from tkinter import ttk

APP_NAME = "DIVA Bridge"
DEVICES = ["auto", "divaller", "zhousensor", "hori", "hori_ps4"]
BACKENDS = {"auto": "Auto", "hidapi": "hidapi (OS HID driver)", "libusb": "libusb (direct USB)"}
DEVICE_NAMES = {
    "auto": "Auto-detect",
    "divaller": "Divaller",
    "zhousensor": "ZhouSensor (HDD mode)",
    "hori": "HORI NSW-230",
    "hori_ps4": "HORI PS4-161",
}

# Per-controller labels: (small buttons fn/l1/l2/l3, extended lb/rb/lt/rt/ls/rs or None)
PROFILES = {
    "Divaller":       ({"fn": "FN", "l1": "L1", "l2": "L2", "l3": "L3"}, None),
    "ZhouSensor HDD": ({"fn": "+", "l1": "−", "l2": "◉", "l3": "⌂"},
                       {"lb": "L", "rb": "R", "lt": "ZL", "rt": "ZR", "ls": "LS", "rs": "RS"}),
    "HORI NSW-230":   ({"fn": "+", "l1": "−", "l2": "◉", "l3": "⌂"},
                       {"lb": "L", "rb": "R", "lt": "ZL", "rt": "ZR", "ls": "LS", "rs": "RS"}),
    "HORI PS4-161":   ({"fn": "OPT", "l1": "SHR", "l2": "TP", "l3": "PS"},
                       {"lb": "L1", "rb": "R1", "lt": "L2", "rt": "R2", "ls": "L3", "rs": "R3"}),
}
HORI_NAMES = ("HORI NSW-230", "HORI PS4-161")
EXT_BITS = ["up", "down", "left", "right", "lb", "rb", "lt", "rt", "ls", "rs"]
EXT = {name: 1 << i for i, name in enumerate(EXT_BITS)}

# Colours
BG = "#15161b"
PANEL = "#23252d"
TEXT = "#d8d8de"
DIM = "#6b6e7a"
CYAN = "#3fd7ea"
FACE = [  # key, name, colour
    ("triangle", "Triangle", "#4fd06a"),
    ("square", "Square", "#e45dd0"),
    ("cross", "Cross", "#5a8cff"),
    ("circle", "Circle", "#f05050"),
]


def config_path():
    if sys.platform == "win32":
        base = os.environ.get("APPDATA", os.path.expanduser("~"))
    elif sys.platform == "darwin":
        base = os.path.expanduser("~/Library/Application Support")
    else:
        base = os.environ.get("XDG_CONFIG_HOME", os.path.expanduser("~/.config"))
    return os.path.join(base, "diva_bridge_gui.json")


def decode(pkt):
    """Decode a Divaller packet the way TLAC does: (buttons, 32 sensors, 0 = left)."""
    s = (pkt or b"") + bytes(24)
    buttons = {
        "triangle": s[3] >> 4 & 1, "square": s[3] >> 2 & 1,
        "cross": s[3] >> 5 & 1, "circle": s[3] >> 3 & 1,
        "fn": s[3] >> 1 & 1, "l1": s[5] & 1, "l2": s[4] >> 7 & 1, "l3": s[4] >> 6 & 1,
    }
    raw = (s[5] >> 4 | s[6] << 4 | s[7] << 12 | s[8] << 20 | s[9] << 28) & 0xFFFFFFFF
    sensors = [raw >> i & 1 for i in range(32)]
    return buttons, sensors


def mix(c1, c2, t):
    """Blend two #rrggbb colours (t = 0 gives c1)."""
    a = [int(c1[i:i + 2], 16) for i in (1, 3, 5)]
    b = [int(c2[i:i + 2], 16) for i in (1, 3, 5)]
    return "#" + "".join(f"{round(x + (y - x) * t):02x}" for x, y in zip(a, b))


# ------------------------------------------------------------------ bridge process

class BridgeProcess:
    """Runs diva_bridge.py as a child process and collects its output lines."""

    def __init__(self, on_line):
        self.on_line = on_line
        self.proc = None

    @staticmethod
    def command(args):
        if getattr(sys, "frozen", False):        # packaged app: the exe runs the bridge itself
            return [sys.executable, "--bridge"] + args
        here = os.path.dirname(os.path.abspath(__file__))
        return [sys.executable, "-u", os.path.join(here, "diva_bridge.py")] + args

    def start(self, args):
        self.stop()
        flags, extra = 0, {}
        if sys.platform == "win32":
            flags = subprocess.CREATE_NO_WINDOW | subprocess.CREATE_NEW_PROCESS_GROUP
        else:
            extra["start_new_session"] = True      # own process group, so stop() gets all of it
        env = dict(os.environ, PYTHONUNBUFFERED="1", PYTHONIOENCODING="utf-8")
        self.proc = subprocess.Popen(
            self.command(args + ["--parent-pid", str(os.getpid())]),
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            stdin=subprocess.DEVNULL, text=True, encoding="utf-8", errors="replace",
            bufsize=1, env=env, creationflags=flags, **extra)
        proc = self.proc
        threading.Thread(target=self._pump, args=(proc,), daemon=True).start()

    def _pump(self, proc):
        for line in proc.stdout:
            self.on_line(line.rstrip("\n"))
        code = proc.wait()
        self.on_line(f"__EXIT__ {code}")

    def stop(self):
        """Stop the bridge and anything it started. A packaged one-file app runs as
        a small launcher plus the real process, so stop the whole tree."""
        proc, self.proc = self.proc, None
        if not proc or proc.poll() is not None:
            return
        if sys.platform == "win32":
            subprocess.run(["taskkill", "/PID", str(proc.pid), "/T", "/F"],
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                           creationflags=subprocess.CREATE_NO_WINDOW)
        else:
            import signal
            try:
                os.killpg(proc.pid, signal.SIGTERM)
            except (ProcessLookupError, PermissionError):
                proc.terminate()
        try:
            proc.wait(timeout=3)
        except subprocess.TimeoutExpired:
            if sys.platform != "win32":
                import signal
                try:
                    os.killpg(proc.pid, signal.SIGKILL)
                except (ProcessLookupError, PermissionError):
                    pass
            proc.kill()

    @property
    def running(self):
        return self.proc is not None and self.proc.poll() is None


class Monitor:
    """Reads the live input state from the bridge over its TCP protocol ('P' + 'X')."""

    def __init__(self):
        self.lock = threading.Lock()
        self.present = False
        self.packet = None
        self.ext = 0
        self.connected = False
        self.rate = 0.0
        self._stop = threading.Event()
        self._thread = None

    def start(self, host, port):
        self.stop()
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, args=(host, port, self._stop), daemon=True)
        self._thread.start()

    def stop(self):
        self._stop.set()
        with self.lock:
            self.connected = self.present = False
            self.packet, self.ext = None, 0

    def _run(self, host, port, stop):
        target = "127.0.0.1" if host in ("", "0.0.0.0") else ("::1" if host == "::" else host)
        while not stop.is_set():
            try:
                with socket.create_connection((target, port), timeout=1) as s:
                    s.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
                    with self.lock:
                        self.connected = True
                    count, t0 = 0, time.monotonic()
                    while not stop.is_set():
                        s.sendall(b"P")
                        present = self._recv(s, 1)[0] == 1
                        s.sendall(b"X\x84\x1a")
                        status, n = self._recv(s, 2)
                        data = self._recv(s, n) if n else b""
                        with self.lock:
                            self.present = present and status == 1
                            self.packet = data[:24] if self.present else None
                            self.ext = int.from_bytes(data[24:26], "little") if len(data) >= 26 else 0
                        count += 1
                        now = time.monotonic()
                        if now - t0 >= 1:
                            self.rate, count, t0 = count / (now - t0), 0, now
                        stop.wait(1 / 60)
            except (OSError, IndexError):
                pass
            with self.lock:
                self.connected = self.present = False
                self.packet, self.ext = None, 0
            stop.wait(0.5)

    @staticmethod
    def _recv(s, n):
        buf = b""
        while len(buf) < n:
            chunk = s.recv(n - len(buf))
            if not chunk:
                raise OSError("closed")
            buf += chunk
        return buf


# ------------------------------------------------------------------ drawing

class Visual(tk.Canvas):
    W, H = 920, 400

    def __init__(self, master):
        super().__init__(master, width=self.W, height=self.H, bg=BG, highlightthickness=0)

    # primitives -------------------------------------------------------
    def rrect(self, x0, y0, x1, y1, r, **kw):
        pts = [x0 + r, y0, x1 - r, y0, x1, y0, x1, y0 + r, x1, y1 - r, x1, y1,
               x1 - r, y1, x0 + r, y1, x0, y1, x0, y1 - r, x0, y0 + r, x0, y0]
        return self.create_polygon(pts, smooth=True, **kw)

    def symbol(self, key, cx, cy, size, colour, width):
        if key == "triangle":
            h = size * 0.95
            self.create_polygon(cx, cy - h * 0.62, cx - size * 0.62, cy + h * 0.42,
                                cx + size * 0.62, cy + h * 0.42, outline=colour, fill="", width=width,
                                joinstyle=tk.ROUND)
        elif key == "square":
            d = size * 0.5
            self.create_rectangle(cx - d, cy - d, cx + d, cy + d, outline=colour, width=width)
        elif key == "cross":
            d = size * 0.52
            self.create_line(cx - d, cy - d, cx + d, cy + d, fill=colour, width=width, capstyle=tk.ROUND)
            self.create_line(cx - d, cy + d, cx + d, cy - d, fill=colour, width=width, capstyle=tk.ROUND)
        else:
            d = size * 0.55
            self.create_oval(cx - d, cy - d, cx + d, cy + d, outline=colour, width=width)

    def dpad(self, cx, cy, ext, arm=13, colour="#f2c84b"):
        for name, dx, dy in (("up", 0, -1), ("down", 0, 1), ("left", -1, 0), ("right", 1, 0)):
            on = ext & EXT[name]
            x, y = cx + dx * arm, cy + dy * arm
            self.create_rectangle(x - arm / 2, y - arm / 2, x + arm / 2, y + arm / 2,
                                  fill=colour if on else "#3a3c46", outline="#55586a")
        self.create_rectangle(cx - arm / 2, cy - arm / 2, cx + arm / 2, cy + arm / 2,
                              fill="#3a3c46", outline="#55586a")

    def pill(self, x, y, label, on, colour="#f2c84b"):
        w = max(30, 9 * len(label) + 14)
        self.rrect(x, y, x + w, y + 22, 10, fill=colour if on else "#30323c",
                   outline=colour if on else "#4a4d5a")
        self.create_text(x + w / 2, y + 11, text=label, fill="#111" if on else TEXT,
                         font=("TkDefaultFont", 10, "bold"))
        return w

    # layouts ----------------------------------------------------------
    def draw(self, name, buttons, sensors, ext, present):
        self.delete("all")
        if not present:
            self.create_text(self.W / 2, self.H / 2 - 12, text="No controller", fill=TEXT,
                             font=("TkDefaultFont", 20, "bold"))
            self.create_text(self.W / 2, self.H / 2 + 20, fill=DIM, font=("TkDefaultFont", 11),
                             text="Plug in a Divaller, ZhouSensor (HDD mode), HORI NSW-230 or PS4-161 (arcade mode)")
            return
        if name in HORI_NAMES:
            self.draw_hori(name, buttons, sensors, ext)
        else:
            self.draw_panel(name, buttons, sensors, ext)

    def draw_panel(self, name, buttons, sensors, ext):
        """Divaller / ZhouSensor: a clean controller panel."""
        small, ext_labels = PROFILES.get(name, PROFILES["Divaller"])
        self.rrect(14, 14, self.W - 14, self.H - 14, 22, fill=PANEL, outline="#3a3d48", width=2)
        self.create_text(36, 40, text=name.upper(), anchor="w", fill=TEXT, font=("TkDefaultFont", 14, "bold"))
        x = self.W - 40
        for key, role in (("fn", "start"), ("l3", "test"), ("l2", "service"), ("l1", "coin")):
            label = small[key]
            w = max(30, 9 * len(label) + 14)
            x -= w
            self.pill(x, 28, label, buttons[key], "#e45dd0" if key != "fn" else "#f2c84b")
            self.create_text(x + w / 2, 60, text=role, fill=DIM, font=("TkDefaultFont", 8))
            x -= 14

        # slider
        sx0, sx1, sy = 40, self.W - 40, 86
        self.rrect(sx0 - 8, sy - 8, sx1 + 8, sy + 48, 12, fill="#11141a", outline="#2b6e78", width=2)
        cw = (sx1 - sx0) / 32
        for i, on in enumerate(sensors):
            x0 = sx0 + i * cw
            self.create_rectangle(x0 + 1, sy, x0 + cw - 1, sy + 40,
                                  fill=CYAN if on else ("#1e2a30" if i % 8 else "#26363d"), outline="")
        self.create_text(self.W / 2, sy + 62, text=f"TOUCH SLIDER   ·   {sum(sensors)}/32 touched",
                         fill=DIM, font=("TkDefaultFont", 9))

        # face buttons
        bw, gap = 150, 40
        x0 = (self.W - (4 * bw + 3 * gap)) / 2
        for i, (key, label, colour) in enumerate(FACE):
            on = buttons[key]
            x = x0 + i * (bw + gap)
            self.rrect(x, 170, x + bw, 290, 24, fill=colour if on else "#1a1c22", outline=colour, width=3)
            self.symbol(key, x + bw / 2, 230, 34, "#ffffff" if on else colour, 5)
            self.create_text(x + bw / 2, 306, text=label, fill=TEXT if on else DIM,
                             font=("TkDefaultFont", 10, "bold" if on else "normal"))

        # D-pad and shoulders (Mega Mix+ only)
        if ext_labels:
            self.create_text(40, 350, text="D-pad", anchor="w", fill=DIM, font=("TkDefaultFont", 9))
            self.dpad(110, 350, ext, arm=11)
            x = 150
            for key in ("lb", "rb", "lt", "rt", "ls", "rs"):
                x += self.pill(x, 339, ext_labels[key], ext & EXT[key], "#e45dd0") + 8
            self.create_text(self.W - 40, 350, text="D-pad and shoulder buttons go to Mega Mix+ only",
                             anchor="e", fill=DIM, font=("TkDefaultFont", 8))

    def draw_hori(self, name, buttons, sensors, ext):
        """HORI NSW-230 / PS4-161: black body, control strip, glass touch-slider
        bar and four big round buttons with coloured rings."""
        small, ext_labels = PROFILES[name]
        W = self.W
        # body + top strip
        self.rrect(10, 18, W - 10, self.H - 8, 18, fill="#0b0b0e", outline="#2a2b31", width=2)
        self.rrect(60, 6, W - 60, 70, 10, fill="#1a1a1f", outline="#2f3037", width=2)
        # small display
        self.create_rectangle(170, 18, 300, 56, fill="#081418", outline="#3a4a50")
        self.create_text(235, 30, text=name.replace("HORI ", ""), fill=CYAN, font=("TkDefaultFont", 11, "bold"))
        self.create_text(235, 46, text="ARCADE MODE", fill="#2b8c99", font=("TkDefaultFont", 8))
        # two knobs (decorative, as on the controller)
        for kx in (100, 132):
            self.create_oval(kx - 11, 26, kx + 11, 48, fill="#101014", outline="#45464e", width=2)
        # D-pad
        self.dpad(345, 37, ext, arm=11)
        # row of small round buttons
        items = [(small["l1"], buttons["l1"]), (small["fn"], buttons["fn"]),
                 (small["l3"], buttons["l3"]), (small["l2"], buttons["l2"])]
        items += [(ext_labels[k], ext & EXT[k]) for k in ("lb", "rb", "lt", "rt", "ls", "rs")]
        x = 400
        for label, on in items:
            self.create_oval(x - 8, 22, x + 8, 38, fill="#f2c84b" if on else "#202128",
                             outline="#f2c84b" if on else "#4a4b55", width=2)
            self.create_text(x, 52, text=label, fill=TEXT if on else DIM, font=("TkDefaultFont", 8, "bold"))
            x += 42

        # glass touch-slider bar
        gx0, gx1, gy0, gy1 = 22, W - 22, 82, 168
        for i in range(8):        # soft vertical gradient for the glass
            t = i / 7
            self.create_rectangle(gx0, gy0 + i * (gy1 - gy0) / 8, gx1, gy0 + (i + 1) * (gy1 - gy0) / 8,
                                  fill=mix("#1d5d6b", "#0c1c24", t), outline="")
        self.create_rectangle(gx0, gy0, gx1, gy1, outline="#7fe9f5", width=2)
        self.create_line(gx0 + 4, gy0 + 5, gx1 - 4, gy0 + 5, fill="#a9f3fb")
        self.create_text(gx0 + 24, gy0 + 22, text="◀", fill="#bff7ff", font=("TkDefaultFont", 16, "bold"))
        self.create_text(gx1 - 24, gy0 + 22, text="▶", fill="#bff7ff", font=("TkDefaultFont", 16, "bold"))
        self.create_text(W / 2, gy0 + 22, text="T O U C H        S L I D E R", fill="#9fe6ef",
                         font=("TkDefaultFont", 12))
        cx0, cx1, cy = gx0 + 46, gx1 - 46, gy0 + 46
        cw = (cx1 - cx0) / 32
        for i, on in enumerate(sensors):
            x0 = cx0 + i * cw
            if on:
                self.create_rectangle(x0 + 1, cy - 4, x0 + cw - 1, cy + 30, fill="#ffffff", outline="#bff7ff")
            else:
                self.create_rectangle(x0 + 1, cy + 22, x0 + cw - 1, cy + 28,
                                      fill="#3b8f9c" if i % 8 else "#5fc2cf", outline="")
        self.create_text(gx1 - 12, gy1 - 10, text=f"{sum(sensors)}/32", anchor="e", fill="#9fe6ef",
                         font=("TkDefaultFont", 8))

        # four big round buttons
        r, gap = 76, 52
        total = 4 * 2 * r + 3 * gap
        x = (W - total) / 2 + r
        cy = 278
        for key, label, colour in FACE:
            on = buttons[key]
            # glow
            if on:
                self.create_oval(x - r - 10, cy - r - 10, x + r + 10, cy + r + 10,
                                 fill=mix(colour, "#0b0b0e", 0.55), outline="")
            self.create_oval(x - r, cy - r, x + r, cy + r, fill="#16171c", outline="#3b3c44", width=3)
            self.create_oval(x - r + 9, cy - r + 9, x + r - 9, cy + r - 9,
                             fill=colour if on else mix(colour, "#0b0b0e", 0.82), outline=colour, width=7)
            self.symbol(key, x, cy, 36, "#ffffff" if on else colour, 7)
            x += 2 * r + gap


# ------------------------------------------------------------------ app

class App:
    def __init__(self, root):
        self.root = root
        root.title(APP_NAME)
        root.configure(bg=BG)
        root.minsize(1000, 640)
        self.lines = queue.Queue()
        self.bridge = BridgeProcess(self.lines.put)
        self.monitor = Monitor()
        self.controller = None       # name from the bridge log
        self.via = None              # "hidapi" / "libusb" from the bridge log
        self.server_up = False
        self.games = 0
        self.settings = self.load_settings()

        style = ttk.Style(root)
        try:
            style.theme_use("clam")
        except tk.TclError:
            pass
        for w in ("TFrame", "TLabel", "TCheckbutton"):
            style.configure(w, background=BG, foreground=TEXT)
        style.map("TCheckbutton", background=[("active", BG)])
        style.configure("Status.TLabel", foreground=DIM)

        top = ttk.Frame(root, padding=(12, 10, 12, 4))
        top.pack(fill="x")
        ttk.Label(top, text="Listen IP").grid(row=0, column=0, sticky="w")
        self.host = tk.StringVar(value=self.settings.get("host", "127.0.0.1"))
        ttk.Combobox(top, textvariable=self.host, width=16,
                     values=["127.0.0.1", "0.0.0.0", "::1", "::"]).grid(row=0, column=1, padx=(6, 14))
        ttk.Label(top, text="Port").grid(row=0, column=2, sticky="w")
        self.port = tk.StringVar(value=str(self.settings.get("port", 45710)))
        ttk.Entry(top, textvariable=self.port, width=7).grid(row=0, column=3, padx=(6, 14))
        ttk.Label(top, text="Controller").grid(row=0, column=4, sticky="w")
        self.device = tk.StringVar(value=DEVICE_NAMES.get(self.settings.get("device", "auto")))
        ttk.Combobox(top, textvariable=self.device, state="readonly", width=22,
                     values=[DEVICE_NAMES[d] for d in DEVICES]).grid(row=0, column=5, padx=(6, 14))
        ttk.Label(top, text="USB access").grid(row=0, column=6, sticky="w")
        self.backend = tk.StringVar(value=BACKENDS.get(self.settings.get("backend", "auto")))
        ttk.Combobox(top, textvariable=self.backend, state="readonly", width=20,
                     values=list(BACKENDS.values())).grid(row=0, column=7, padx=(6, 14))
        self.start_btn = ttk.Button(top, text="Start server", command=self.toggle, width=14)
        self.start_btn.grid(row=0, column=8, sticky="e")
        top.columnconfigure(8, weight=1)

        opts = ttk.Frame(root, padding=(12, 0, 12, 4))
        opts.pack(fill="x")
        self.slider_reverse = tk.BooleanVar(value=self.settings.get("slider_reverse", False))
        self.led_reverse = tk.BooleanVar(value=self.settings.get("led_reverse", False))
        self.no_leds = tk.BooleanVar(value=self.settings.get("no_leds", False))
        self.autostart = tk.BooleanVar(value=self.settings.get("autostart", False))
        for text, var in (("Reverse slider", self.slider_reverse), ("Reverse LEDs (ZhouSensor)", self.led_reverse),
                          ("Don't send LEDs", self.no_leds), ("Start server when the app opens", self.autostart)):
            ttk.Checkbutton(opts, text=text, variable=var).pack(side="left", padx=(0, 16))

        self.status = ttk.Label(root, style="Status.TLabel", padding=(12, 2))
        self.status.pack(fill="x")

        self.visual = Visual(root)
        self.visual.pack(padx=10, pady=6)

        logf = ttk.Frame(root, padding=(10, 0, 10, 10))
        logf.pack(fill="both", expand=True)
        self.log = tk.Text(logf, height=8, bg="#0f1014", fg=TEXT, insertbackground=TEXT,
                           relief="flat", font=("TkFixedFont", 9), state="disabled", wrap="none")
        sb = ttk.Scrollbar(logf, command=self.log.yview)
        self.log.configure(yscrollcommand=sb.set)
        self.log.pack(side="left", fill="both", expand=True)
        sb.pack(side="right", fill="y")

        root.protocol("WM_DELETE_WINDOW", self.quit)
        self.tick()
        if self.autostart.get():
            root.after(300, self.toggle)

    # settings ---------------------------------------------------------
    def load_settings(self):
        try:
            with open(config_path(), encoding="utf-8") as f:
                return json.load(f)
        except (OSError, ValueError):
            return {}

    def save_settings(self):
        data = {
            "host": self.host.get().strip(), "port": self.port_value(),
            "device": self.device_key(), "backend": self.backend_key(),
            "slider_reverse": self.slider_reverse.get(),
            "led_reverse": self.led_reverse.get(), "no_leds": self.no_leds.get(),
            "autostart": self.autostart.get(),
        }
        try:
            os.makedirs(os.path.dirname(config_path()), exist_ok=True)
            with open(config_path(), "w", encoding="utf-8") as f:
                json.dump(data, f, indent=2)
        except OSError:
            pass

    def port_value(self):
        try:
            p = int(self.port.get())
            return p if 0 < p < 65536 else None
        except ValueError:
            return None

    def backend_key(self):
        for k, v in BACKENDS.items():
            if v == self.backend.get():
                return k
        return "auto"

    def device_key(self):
        for k, v in DEVICE_NAMES.items():
            if v == self.device.get():
                return k
        return "auto"

    # server control ---------------------------------------------------
    def toggle(self):
        if self.bridge.running:
            self.stop_server()
        else:
            self.start_server()

    def start_server(self):
        port = self.port_value()
        host = self.host.get().strip() or "127.0.0.1"
        if port is None:
            self.append_log("Port must be a number from 1 to 65535.")
            return
        args = ["--host", host, "--port", str(port), "--device", self.device_key(),
                "--backend", self.backend_key()]
        if self.slider_reverse.get():
            args.append("--slider-reverse")
        if self.led_reverse.get():
            args.append("--led-reverse")
        if self.no_leds.get():
            args.append("--no-leds")
        self.save_settings()
        self.controller, self.server_up, self.games = None, False, 0
        self.append_log(f"--- starting bridge on {host}:{port} ({self.device.get()}) ---")
        try:
            self.bridge.start(args)
        except OSError as e:
            self.append_log(f"Could not start the bridge: {e}")
            return
        self.monitor.start(host, port)
        self.start_btn.configure(text="Stop server")

    def stop_server(self):
        self.monitor.stop()
        self.bridge.stop()
        self.server_up = False
        self.controller = None
        self.start_btn.configure(text="Start server")

    def quit(self):
        self.save_settings()
        self.monitor.stop()
        self.bridge.stop()
        self.root.destroy()

    # log parsing ------------------------------------------------------
    def handle_line(self, line):
        if line.startswith("__EXIT__"):
            code = line.split()[1]
            if self.server_up or code not in ("0", "-15", "15", "1"):
                self.append_log(f"--- bridge stopped (exit code {code}) ---")
            self.server_up = False
            self.controller = None
            self.monitor.stop()
            self.start_btn.configure(text="Start server")
            return
        self.append_log(line)
        if "listening on" in line:
            self.server_up = True
        m = re.match(r"^(.+?) connected \(([0-9A-F]{4}):([0-9A-F]{4})(?: via (\w+))?", line)
        if m:
            self.controller = m.group(1)
            self.via = m.group(4)
        if " lost: " in line:
            self.controller = self.via = None
        if line.startswith("game connected"):
            self.games += 1
        elif line.startswith("game disconnected"):
            self.games = max(0, self.games - 1)
        if "address already in use" in line.lower() or "Only one usage of each socket address" in line \
                or "WinError 10048" in line or "WinError 10013" in line or "Errno 48" in line:
            self.append_log(">>> That port is already in use - stop the other bridge or pick another port.")
        if "USB access available: none" in line:
            self.append_log(">>> Install USB access for the bridge:  pip install hidapi  (ZhouSensor/HORI)"
                            "  and/or  pip install pyusb  (Divaller; needs libusb-1.0).")
        if "No backend available" in line:
            self.append_log(">>> libusb-1.0 is missing - see the README for your platform.")

    def append_log(self, text):
        self.log.configure(state="normal")
        self.log.insert("end", text + "\n")
        lines = int(self.log.index("end-1c").split(".")[0])
        if lines > 500:
            self.log.delete("1.0", f"{lines - 500}.0")
        self.log.see("end")
        self.log.configure(state="disabled")

    # refresh ----------------------------------------------------------
    def tick(self):
        try:
            while True:
                self.handle_line(self.lines.get_nowait())
        except queue.Empty:
            pass
        with self.monitor.lock:
            pkt, ext, present = self.monitor.packet, self.monitor.ext, self.monitor.present
            connected, rate = self.monitor.connected, self.monitor.rate
        running = self.bridge.running
        if not running:
            state = "Server stopped"
        elif not self.server_up:
            state = "Server starting…"
        else:
            state = f"Server listening on {self.host.get().strip()}:{self.port.get()}"
        ctrl = self.controller or ("none" if running else "—")
        if self.controller and self.via:
            ctrl += f" (via {self.via})"
        games = max(0, self.games - (1 if connected else 0))   # don't count our own monitor
        self.status.configure(text=f"{state}     Controller: {ctrl}     Game connections: {games}"
                                   + (f"     Display {rate:.0f} Hz" if connected else ""))
        buttons, sensors = decode(pkt)
        self.visual.draw(self.controller or "", buttons, sensors, ext, running and present and pkt is not None)
        self.root.after(33, self.tick)


def main():
    root = tk.Tk()
    base = getattr(sys, "_MEIPASS", os.path.dirname(os.path.abspath(__file__)))
    icon = os.path.join(base, "assets", "icon.png")
    if os.path.exists(icon):
        try:
            root.iconphoto(True, tk.PhotoImage(file=icon))
        except tk.TclError:
            pass
    App(root)
    root.mainloop()


if __name__ == "__main__":
    main()
