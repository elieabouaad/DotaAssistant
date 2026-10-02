"""In-game HUD overlay for single-monitor setups.

The web dashboard assumes a second screen; this doesn't. It's a frameless,
click-through, colour-keyed window that floats over Dota and shows only what
you can actually read mid-fight: the next few timers, the Roshan window,
buyback when you're dead, your lane CS pace, and the enemy-activity flags GSI
gives us that the game's own UI never shows. Everything else stays on the
dashboard, which is for between games.

Runs as its own process, polling the server's /api/live like any other client,
so it can be restarted or killed without disturbing GSI ingestion:

    dota-assistant overlay            # normal use
    dota-assistant overlay --setup    # opaque and draggable — drop it where you want

Dota must be in **Fullscreen Windowed (Borderless)**. Exclusive fullscreen owns
the display and will hide the overlay (or flicker whenever it repaints).

Windows only for the good bits: click-through is WS_EX_TRANSPARENT and the
transparent background is a colour key. Elsewhere it degrades to a small opaque
always-on-top box that still shows everything.
"""

from __future__ import annotations

import math
import sys
import threading
import time
import tkinter as tk
import tkinter.font as tkfont

import requests

from . import config as app_config
from . import settings
from .timer_engine import ACTIVE_STATES

_server_cfg = app_config.section("server")
API = f"http://{_server_cfg.get('host', '127.0.0.1')}:{_server_cfg.get('port', 53100)}/api/live"

POLL_SECONDS = 1.0
REDRAW_MS = 250
TOPMOST_REFRESH_MS = 2000

# Any pixel exactly this colour is punched out of the window. Near-black rather
# than a garish key colour: text antialiases against it into a dark fringe,
# which reads as a drop shadow over bright parts of the map instead of a halo.
COLORKEY = "#0a0b0c"

FG = "#e8e8ea"        # normal text
DIM = "#8b9298"       # labels, idle chatter
SOON = "#ffcc44"      # 30s out
URGENT = "#ff5c5c"    # 10s out, or bad news
GOOD = "#5ce08a"
WARN = "#ff9a4d"

DEFAULTS = app_config.DEFAULTS["overlay"]

# The timer engine's labels are written for speech and the dashboard; a HUD
# wants them short and scannable.
SHORT_LABELS = {
    "Bounty runes": "BOUNTY",
    "Water rune": "WATER",
    "Power rune": "POWER",
    "Wisdom shrine": "WISDOM",
    "Stack camps": "STACK",
    "Night": "NIGHT",
    "Day": "DAY",
    "Tormentor": "TORMENTOR",
    "Lotus": "LOTUS",
    # Roshan/Aegis arrive as one-shots, whose "label" is the spoken sentence.
    "Aegis expires in 30 seconds": "AEGIS ENDS",
    "Roshan possible in one minute": "ROSH SOON",
    "Roshan may be up": "ROSH MAYBE",
    "Roshan is up": "ROSHAN UP",
}


def load_config() -> dict:
    """config.toml [overlay] over the defaults, with settings.json overrides on
    top (that's where --setup writes the position you dragged it to)."""
    cfg = app_config.section("overlay")
    cfg.update({k: v for k, v in settings.overlay_overrides().items() if k in DEFAULTS})
    return cfg


def short(label: str) -> str:
    return SHORT_LABELS.get(label, label.upper())


def countdown(seconds: float) -> str:
    s = max(0, math.ceil(seconds))
    return f"{s // 60}:{s % 60:02d}"


def urgency(seconds: float) -> str:
    if seconds <= 10:
        return URGENT
    if seconds <= 30:
        return SOON
    return FG


# --------------------------------------------------------------------------- #
# Polling
# --------------------------------------------------------------------------- #

class Poller(threading.Thread):
    """Pulls /api/live once a second into a snapshot the UI thread reads.

    Keeps `fetched_at` alongside the payload so the UI can interpolate the game
    clock between polls — otherwise every countdown visibly stutters, which is
    exactly the thing you notice in peripheral vision."""

    def __init__(self) -> None:
        super().__init__(daemon=True)
        self._lock = threading.Lock()
        self._payload: dict | None = None
        self._fetched_at = 0.0
        self._online = False

    def snapshot(self) -> tuple[dict | None, float, bool]:
        with self._lock:
            return self._payload, self._fetched_at, self._online

    def run(self) -> None:
        session = requests.Session()
        while True:
            try:
                data = session.get(API, timeout=2).json()
                with self._lock:
                    self._payload, self._fetched_at, self._online = data, time.monotonic(), True
            except Exception:  # server not up yet, restarting, or mid-shutdown
                with self._lock:
                    self._online = False
            time.sleep(POLL_SECONDS)


# --------------------------------------------------------------------------- #
# What to draw
# --------------------------------------------------------------------------- #
# Rows are ("pair", left, right, colour) | ("chips", [(text, colour), ...]).

def build_rows(payload: dict | None, age: float, online: bool, cfg: dict) -> list[tuple]:
    if not online:
        return [("pair", "ASSISTANT", "offline", DIM)]
    if not payload:
        return [("pair", "ASSISTANT", "...", DIM)]

    state = payload.get("state") or {}
    map_ = state.get("map") or {}
    hero = state.get("hero") or {}
    player = state.get("player") or {}

    if map_.get("game_state") not in ACTIVE_STATES:
        return [("pair", "ASSISTANT", "no game", DIM)]

    # Interpolate the clock forward from the last poll so countdowns tick every
    # frame rather than once a second, in a 1-second staircase.
    clock = map_.get("clock_time")
    paused = bool(map_.get("paused"))
    if clock is None:
        return [("pair", "ASSISTANT", "no clock", DIM)]
    now = clock if paused else clock + age

    rows: list[tuple] = []

    for event in (payload.get("upcoming") or [])[: int(cfg["timers"])]:
        remaining = event["at"] - now
        rows.append(("pair", short(event["label"]), countdown(remaining), urgency(remaining)))

    # Dead: the one moment you genuinely can't afford to go read the shop UI.
    if hero.get("alive") is False:
        respawn = hero.get("respawn_seconds")
        rows.append(("pair", "RESPAWN", f"{respawn}s" if respawn else "--", WARN))
        cost = hero.get("buyback_cost")
        cooldown = hero.get("buyback_cooldown") or 0
        gold = player.get("gold")
        if cooldown > 0:
            rows.append(("pair", "BUYBACK", f"cd {countdown(cooldown)}", DIM))
        elif cost is not None and gold is not None:
            if gold >= cost:
                rows.append(("pair", "BUYBACK", f"{cost}g YES", GOOD))
            else:
                rows.append(("pair", "BUYBACK", f"-{cost - gold}g", URGENT))

    # Lane pace, while it still means anything. Your CS against the benchmark
    # for this hero is the number the post-game review keeps flagging, so it's
    # worth having in front of you while you can still act on it.
    lane = payload.get("lane") or {}
    if lane.get("expected") is not None and clock <= int(cfg["lane_until"]):
        got, want = lane["last_hits"], lane["expected"]
        delta = got - want
        rows.append((
            "pair", "CS", f"{got}/{want} {delta:+d}",
            GOOD if lane.get("on_track") else WARN,
        ))

    # Both-team reads GSI hands us that the game UI never surfaces.
    context = payload.get("context") or {}
    chips: list[tuple[str, str]] = []
    if context.get("enemy_smoke_recent"):
        chips.append(("SMOKE", URGENT))
    if context.get("enemy_scan_recent"):
        chips.append(("SCAN", SOON))
    if context.get("enemy_buyback_recent"):
        chips.append(("E-BB", SOON))
    swing = context.get("fight_net_kills") or 0
    if swing:
        chips.append((f"{swing:+d}", GOOD if swing > 0 else URGENT))
    if chips:
        rows.append(("chips", chips))

    if paused:
        rows.append(("pair", "PAUSED", "", DIM))
    return rows or [("pair", "ASSISTANT", "quiet", DIM)]


# --------------------------------------------------------------------------- #
# Window
# --------------------------------------------------------------------------- #

class Overlay:
    def __init__(self, cfg: dict, poller: Poller, setup: bool) -> None:
        self.cfg = cfg
        self.poller = poller
        self.setup = setup
        self.visible = True
        self.hwnd = None
        self._height = 0
        self._drag = (0, 0)

        self.root = tk.Tk()
        self.root.title("Dota Assistant overlay")
        self.root.overrideredirect(True)
        self.root.attributes("-topmost", True)

        bg = "#181a1d" if setup else COLORKEY
        self.root.configure(bg=bg)
        if not setup:
            try:
                self.root.attributes("-transparentcolor", COLORKEY)
                self.root.attributes("-alpha", float(cfg["opacity"]))
            except tk.TclError:  # not Windows — stay opaque but keep working
                self.root.configure(bg="#181a1d")
                bg = "#181a1d"

        self.font = tkfont.Font(family=cfg["font"], size=int(cfg["font_size"]), weight="bold")
        self.line_h = self.font.metrics("linespace") + 4
        self.pad = 8
        self.width = int(cfg["width"])

        self.canvas = tk.Canvas(
            self.root, bg=bg, highlightthickness=0, bd=0,
            width=self.width, height=self.line_h,
        )
        self.canvas.pack()

        self.place(self.line_h + 2 * self.pad)
        self.root.update_idletasks()

        if setup:
            self.canvas.bind("<Button-1>", self.on_press)
            self.canvas.bind("<B1-Motion>", self.on_drag)
            self.canvas.bind("<ButtonRelease-1>", self.on_drop)
        else:
            self.make_click_through()

        self.tick()
        if not setup:
            self.root.after(TOPMOST_REFRESH_MS, self.reassert_topmost)

    # --- Windows plumbing --------------------------------------------------

    def make_click_through(self) -> None:
        """Mouse events fall through to Dota, the window never takes focus, and
        it stays out of alt-tab. Without this the overlay eats the click that
        was meant for the shop."""
        try:
            import win32con
            import win32gui
        except ImportError:
            return
        self.hwnd = win32gui.GetParent(self.root.winfo_id()) or self.root.winfo_id()
        style = win32gui.GetWindowLong(self.hwnd, win32con.GWL_EXSTYLE)
        win32gui.SetWindowLong(
            self.hwnd, win32con.GWL_EXSTYLE,
            style | win32con.WS_EX_LAYERED | win32con.WS_EX_TRANSPARENT
            | win32con.WS_EX_NOACTIVATE | win32con.WS_EX_TOOLWINDOW,
        )

    def reassert_topmost(self) -> None:
        """Dota re-asserts its own z-order on alt-tab and on resolution changes,
        which can bury a window that was topmost a moment ago."""
        if self.hwnd and self.visible:
            try:
                import win32con
                import win32gui
                win32gui.SetWindowPos(
                    self.hwnd, win32con.HWND_TOPMOST, 0, 0, 0, 0,
                    win32con.SWP_NOMOVE | win32con.SWP_NOSIZE | win32con.SWP_NOACTIVATE,
                )
            except Exception:
                pass
        self.root.after(TOPMOST_REFRESH_MS, self.reassert_topmost)

    # --- geometry ----------------------------------------------------------

    def place(self, height: int) -> None:
        sw, sh = self.root.winfo_screenwidth(), self.root.winfo_screenheight()
        ox, oy = int(self.cfg["offset_x"]), int(self.cfg["offset_y"])
        anchor = self.cfg["anchor"]
        x = ox if "left" in anchor else sw - self.width - ox
        if anchor == "top-center":
            x = (sw - self.width) // 2 + ox
        y = oy if anchor.startswith("top") else sh - height - oy
        self.root.geometry(f"{self.width}x{height}+{x}+{y}")

    def offsets_from(self, x: int, y: int, height: int) -> tuple[int, int]:
        """Absolute position back to anchor-relative offsets, so a dragged
        window lands in the same spot at the same resolution next launch."""
        sw, sh = self.root.winfo_screenwidth(), self.root.winfo_screenheight()
        anchor = self.cfg["anchor"]
        if anchor == "top-center":
            ox = x - (sw - self.width) // 2
        else:
            ox = x if "left" in anchor else sw - self.width - x
        oy = y if anchor.startswith("top") else sh - height - y
        return ox, oy

    # --- setup-mode dragging ------------------------------------------------

    def on_press(self, event) -> None:
        self._drag = (event.x_root - self.root.winfo_x(), event.y_root - self.root.winfo_y())

    def on_drag(self, event) -> None:
        self.root.geometry(f"+{event.x_root - self._drag[0]}+{event.y_root - self._drag[1]}")

    def on_drop(self, _event) -> None:
        ox, oy = self.offsets_from(self.root.winfo_x(), self.root.winfo_y(), self._height)
        settings.set_overlay({"offset_x": ox, "offset_y": oy})
        print(f"saved position: anchor={self.cfg['anchor']} offset_x={ox} offset_y={oy}",
              flush=True)

    # --- render -------------------------------------------------------------

    def toggle(self) -> None:
        self.visible = not self.visible
        if self.visible:
            self.root.deiconify()
        else:
            self.root.withdraw()

    def text(self, x: int, y: int, s: str, colour: str, anchor: str) -> None:
        """Every string gets a black copy one pixel down-right. Over a dark
        forest the shadow is invisible; over the fountain or a bright creep
        fight it's the only reason the text stays readable."""
        self.canvas.create_text(x + 1, y + 1, text=s, fill="#000000",
                                font=self.font, anchor=anchor)
        self.canvas.create_text(x, y, text=s, fill=colour, font=self.font, anchor=anchor)

    def tick(self) -> None:
        payload, fetched_at, online = self.poller.snapshot()
        age = (time.monotonic() - fetched_at) if fetched_at else 0.0
        rows = build_rows(payload, age, online, self.cfg)

        height = len(rows) * self.line_h + 2 * self.pad
        if height != self._height:
            self._height = height
            self.canvas.config(height=height)
            self.place(height)

        self.canvas.delete("all")
        y = self.pad
        for row in rows:
            if row[0] == "pair":
                _, left, right, colour = row
                self.text(self.pad, y, left, DIM, "nw")
                self.text(self.width - self.pad, y, right, colour, "ne")
            else:
                x = self.pad
                for chip, colour in row[1]:
                    self.text(x, y, chip, colour, "nw")
                    x += self.font.measure(chip + "  ")
            y += self.line_h

        self.root.after(REDRAW_MS, self.tick)


def start_hotkey(overlay: Overlay, name: str) -> None:
    """Show/hide without alt-tabbing. Same caveat as the server's F8: needs
    Input Monitoring permission on macOS, degrades to "always visible"."""
    try:
        from pynput import keyboard

        target = getattr(keyboard.Key, name.lower())

        def on_press(key):
            if key == target:
                overlay.root.after(0, overlay.toggle)  # tk is not thread-safe

        listener = keyboard.Listener(on_press=on_press)
        listener.daemon = True
        listener.start()
        print(f"Hotkeys active: {name.upper()} = show/hide overlay", flush=True)
    except Exception as exc:
        print(f"Overlay hotkey unavailable ({exc}); overlay stays visible", flush=True)


def main() -> None:
    setup = "--setup" in sys.argv
    cfg = load_config()

    poller = Poller()
    poller.start()

    overlay = Overlay(cfg, poller, setup)
    if setup:
        print("Setup mode: drag the box where you want it, then close this window.")
        print("Position is saved to settings.json on each drop; restart without --setup.")
    else:
        start_hotkey(overlay, str(cfg["hotkey"]))
        print(f"Overlay running ({cfg['anchor']}). Dota must be in Fullscreen Windowed.")
        print("Reposition with:  dota-assistant overlay --setup")

    try:
        overlay.root.mainloop()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
