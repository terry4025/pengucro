"""Start-up splash overlay.

The previous implementation ran a 16 ms (~62 fps) callback that, on every single
frame, allocated a new 160x160 RGBA image, drew ten ellipses into it, applied
``GaussianBlur(8)``, rotated the logo with BICUBIC resampling, resized it with
LANCZOS and wrapped the result in a fresh ``ImageTk.PhotoImage``. All of that
happened on the Tk main thread, and the progress bar advanced on a fixed
schedule that took roughly two seconds regardless of how long start-up actually
needed. Launching the app therefore always cost about 2.2 seconds of blocked UI.

Here the expensive raster work is done once: a static glow and a handful of
pre-scaled logo frames are built at construction (a few milliseconds in total)
and the per-frame callback only moves cheap canvas items. The progress bar
tracks real start-up state -- it eases up to 90% and then waits for
:meth:`finish`, so the splash disappears as soon as the window is actually
ready instead of padding out a fixed animation.
"""

from __future__ import annotations

import math
import os
import random
import time

from PIL import Image, ImageDraw, ImageFilter, ImageFont, ImageTk

import customtkinter as ctk

import ui.theme as theme


FRAME_INTERVAL_MS = 33          # ~30 fps is plenty for drifting dots
LOGO_FRAMES = 8                 # Pre-rendered breathing steps
LOGO_BASE = 88
LOGO_PULSE = 6
CANVAS_SIZE = 176
PARTICLE_COUNT = 10
PROGRESS_WIDTH = 184
PROGRESS_HEIGHT = 3

# Minimum time the splash stays on screen. Construction of the main window
# finishes before the overlay is even created, so this is purely additive to
# start-up. The original fixed animation cost ~2.2 s; trimming it to ~0.3 s made
# the splash flash by too quickly to read, so it is paced to a deliberate but
# short interval instead.
MIN_VISIBLE_MS = 900
_EASE_CEILING = 0.92            # Progress ceiling before start-up reports ready
_HARD_CAP_MS = 2600             # Never hold the splash longer than this

# Vertical composition, as fractions of the overlay height. The logo group and
# the progress group were originally 130 px apart, which left the layout looking
# unbalanced with a large dead band in the middle.
LOGO_CENTRE_Y = 0.38
PROGRESS_CENTRE_Y = 0.62

# Shown under the wordmark as the bar fills, so the splash visibly progresses
# instead of just sitting there. (threshold, caption)
STAGES = (
    (0.00, "시작하는 중"),
    (0.30, "사이트 정보 준비 중"),
    (0.62, "예약 화면 구성 중"),
    (0.90, "거의 완료"),
)

RING_RADIUS = 74
ARC_EXTENT = 74                 # Degrees of the sweeping arc
ARC_SPEED = 5.0                 # Degrees per frame


def _app_version() -> str:
    try:
        from pengucro import __version__

        return __version__
    except Exception:
        return ""


def _resource_path(relative_path: str) -> str:
    import sys

    try:
        base_path = sys._MEIPASS
    except Exception:
        base_path = os.path.abspath(".")
    return os.path.join(base_path, relative_path)


class LoadingOverlay(ctk.CTkFrame):
    def __init__(self, parent, on_complete):
        super().__init__(parent, fg_color=theme.CANVAS_COLOR, corner_radius=0)
        self.parent = parent
        self.on_complete = on_complete

        self._alive = True
        self._completed = False
        self._ready = False
        self._tick = 0
        self._progress = 0.0
        self._shine = -0.25
        self._place_y = 0
        self._arc_angle = 90.0
        self._started_ms = self._now_ms()
        self._status_override = ""

        self.canvas = ctk.CTkCanvas(self, bg=theme.CANVAS_COLOR, highlightthickness=0)
        self.canvas.pack(fill="both", expand=True)

        self._logo_frames = self._build_logo_frames()
        self._particles = [self._new_particle(initial=True) for _ in range(PARTICLE_COUNT)]

        self.after(FRAME_INTERVAL_MS, self._render_frame)
        self.after(FRAME_INTERVAL_MS, self._advance_progress)
        self.after(_HARD_CAP_MS, self._force_ready)

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------
    @staticmethod
    def _now_ms() -> int:
        return int(time.monotonic() * 1000)

    def set_status(self, text: str) -> None:
        """Pin the caption under the logo, overriding the automatic stages."""
        self._status_override = text or ""

    @property
    def _status_text(self) -> str:
        if self._status_override:
            return self._status_override
        caption = STAGES[0][1]
        for threshold, text in STAGES:
            if self._progress >= threshold:
                caption = text
        return caption

    def finish(self) -> None:
        """Signal that start-up finished; the splash may complete its exit."""
        self._ready = True

    # ------------------------------------------------------------------
    # Pre-rendered artwork
    # ------------------------------------------------------------------
    def _load_logo(self) -> Image.Image:
        try:
            path = _resource_path("app_icon.png")
            if os.path.exists(path):
                return Image.open(path).convert("RGBA")
        except Exception:
            pass

        # Fall back to drawing the penguin glyph with the Windows colour emoji
        # font so the splash still has a focal point.
        fallback = Image.new("RGBA", (128, 128), (0, 0, 0, 0))
        draw = ImageDraw.Draw(fallback)
        try:
            font = ImageFont.truetype("seguiemj.ttf", 84)
        except Exception:
            font = ImageFont.load_default()
        draw.text((64, 64), "\U0001F427", fill="#FFFFFF", font=font, anchor="mm")
        return fallback

    def _build_glow(self) -> Image.Image:
        """One soft radial glow, blurred once instead of once per frame."""
        glow = Image.new("RGBA", (CANVAS_SIZE, CANVAS_SIZE), (0, 0, 0, 0))
        draw = ImageDraw.Draw(glow)
        centre = CANVAS_SIZE // 2
        outer = 62
        for radius in range(outer, 8, -4):
            alpha = int(46 * (1.0 - radius / outer))
            draw.ellipse(
                (centre - radius, centre - radius, centre + radius, centre + radius),
                fill=(74, 150, 235, alpha),
            )
        return glow.filter(ImageFilter.GaussianBlur(10))

    def _build_logo_frames(self) -> list[ImageTk.PhotoImage]:
        """Composite the glow with a few pre-scaled logo sizes, once."""
        try:
            source = self._load_logo()
            glow = self._build_glow()
        except Exception:
            return []

        frames: list[ImageTk.PhotoImage] = []
        for index in range(LOGO_FRAMES):
            # Half a sine period so cycling 0..n-1..0 breathes smoothly.
            phase = math.sin(math.pi * index / max(1, LOGO_FRAMES - 1))
            size = int(LOGO_BASE + LOGO_PULSE * phase)
            try:
                scaled = source.resize((size, size), Image.Resampling.LANCZOS)
                composite = glow.copy()
                offset = (CANVAS_SIZE - size) // 2
                composite.alpha_composite(scaled, (offset, offset))
                frames.append(ImageTk.PhotoImage(composite))
            except Exception:
                break
        return frames

    # ------------------------------------------------------------------
    # Particles
    # ------------------------------------------------------------------
    def _new_particle(self, initial: bool = False) -> dict:
        depth = random.uniform(0.35, 1.0)
        return {
            "x": random.uniform(0.06, 0.94),
            "y": random.uniform(0.05, 1.0) if initial else random.uniform(1.0, 1.15),
            "speed": random.uniform(0.0016, 0.0052) * depth,
            "drift": random.uniform(-0.0008, 0.0008),
            "radius": max(1.0, 2.6 * depth),
            "alpha": random.uniform(0.18, 0.55) * depth,
            "phase": random.uniform(0.0, math.tau),
        }

    @staticmethod
    def _dot_color(alpha: float) -> str:
        # Blend the accent tint down onto the near-black canvas.
        level = max(0.0, min(1.0, alpha))
        red = int(0x0A + (0x4A - 0x0A) * level)
        green = int(0x0A + (0x96 - 0x0A) * level)
        blue = int(0x0C + (0xEB - 0x0C) * level)
        return f"#{red:02x}{green:02x}{blue:02x}"

    # ------------------------------------------------------------------
    # Frame loop
    # ------------------------------------------------------------------
    def _render_frame(self) -> None:
        if not self._alive or not self.winfo_exists():
            return

        width = self.winfo_width()
        height = self.winfo_height()
        if width <= 1 or height <= 1:
            width, height = 480, 900

        self._tick += 1
        canvas = self.canvas
        canvas.delete("all")

        # Drifting dots -- pure canvas items, no raster work.
        for index, particle in enumerate(self._particles):
            particle["y"] -= particle["speed"]
            particle["x"] += particle["drift"] + 0.0004 * math.sin(
                self._tick * 0.06 + particle["phase"]
            )
            if particle["y"] < -0.05 or not 0.0 < particle["x"] < 1.0:
                self._particles[index] = self._new_particle()
                continue
            px = particle["x"] * width
            py = particle["y"] * height
            radius = particle["radius"]
            canvas.create_oval(
                px - radius,
                py - radius,
                px + radius,
                py + radius,
                fill=self._dot_color(particle["alpha"]),
                outline="",
            )

        centre_x = width / 2
        logo_y = height * LOGO_CENTRE_Y

        # Static hairline ring plus one sweeping arc. Two canvas items per frame,
        # and it reads as deliberate motion without the old wobbling logo.
        canvas.create_oval(
            centre_x - RING_RADIUS,
            logo_y - RING_RADIUS,
            centre_x + RING_RADIUS,
            logo_y + RING_RADIUS,
            outline=theme.HAIRLINE_COLOR,
            width=1,
        )
        self._arc_angle = (self._arc_angle - ARC_SPEED) % 360
        canvas.create_arc(
            centre_x - RING_RADIUS,
            logo_y - RING_RADIUS,
            centre_x + RING_RADIUS,
            logo_y + RING_RADIUS,
            start=self._arc_angle,
            extent=ARC_EXTENT,
            style="arc",
            outline=theme.ACCENT_BLUE,
            width=2,
        )

        if self._logo_frames:
            cycle = 2 * (len(self._logo_frames) - 1) or 1
            position = self._tick // 3 % cycle
            if position >= len(self._logo_frames):
                position = cycle - position
            canvas.create_image(centre_x, logo_y, image=self._logo_frames[position])

        # Wordmark
        canvas.create_text(
            centre_x,
            logo_y + RING_RADIUS + 26,
            text="방탈출 펭크로",
            font=(theme.FONT_FAMILY, 18, "bold"),
            fill=theme.TEXT_PRIMARY,
        )
        canvas.create_text(
            centre_x,
            logo_y + RING_RADIUS + 48,
            text=f"v{_app_version()}",
            font=(theme.FONT_FAMILY, 10),
            fill=theme.TEXT_TERTIARY,
        )

        bar_y = height * PROGRESS_CENTRE_Y
        canvas.create_text(
            centre_x,
            bar_y - 20,
            text=self._status_text,
            font=(theme.FONT_FAMILY, 11),
            fill=theme.TEXT_MUTE,
        )
        canvas.create_text(
            centre_x,
            bar_y + 18,
            text=f"{int(self._progress * 100)}%",
            font=(theme.FONT_MONO_FAMILY, 10, "bold"),
            fill=theme.TEXT_TERTIARY,
        )

        self._draw_progress(canvas, centre_x, bar_y)
        self.after(FRAME_INTERVAL_MS, self._render_frame)

    def _draw_progress(self, canvas, centre_x: float, bar_y: float) -> None:
        half = PROGRESS_WIDTH / 2
        left = centre_x - half
        right = centre_x + half
        top = bar_y - PROGRESS_HEIGHT / 2
        bottom = bar_y + PROGRESS_HEIGHT / 2

        canvas.create_rectangle(left, top, right, bottom, fill=theme.ELEVATED_COLOR, outline="")
        if self._progress <= 0.0:
            return

        filled = left + PROGRESS_WIDTH * self._progress
        canvas.create_rectangle(left, top, filled, bottom, fill=theme.ACCENT_BLUE, outline="")

        # A single soft highlight travelling along the filled part reads as
        # "working" without the five-line rainbow sweep of the old version.
        self._shine += 0.022
        if self._shine > 1.2:
            self._shine = -0.25
        shine_x = left + PROGRESS_WIDTH * self._shine
        for offset, color in ((-7, "#5AA9FF"), (0, "#DCEBFF"), (7, "#5AA9FF")):
            x = shine_x + offset
            if left <= x <= filled:
                canvas.create_line(x, top, x, bottom, fill=color, width=2)

    # ------------------------------------------------------------------
    # Progress / exit
    # ------------------------------------------------------------------
    def _advance_progress(self) -> None:
        if not self._alive or not self.winfo_exists():
            return

        elapsed = self._now_ms() - self._started_ms
        held_long_enough = elapsed >= MIN_VISIBLE_MS

        if self._ready and held_long_enough:
            target = 1.0
        else:
            # Paced against MIN_VISIBLE_MS so the bar fills at a readable rate
            # and stops just short of full until start-up actually reports in.
            target = min(_EASE_CEILING, elapsed / MIN_VISIBLE_MS * _EASE_CEILING)

        self._progress += (target - self._progress) * 0.34
        if target >= 1.0 and self._progress >= 0.99:
            self._progress = 1.0
            self.after(90, self._fade_out)
            return
        self.after(FRAME_INTERVAL_MS, self._advance_progress)

    def _force_ready(self) -> None:
        if self._alive and self.winfo_exists():
            self._ready = True

    def _to_logical(self, value: float) -> int:
        """Convert a physical pixel measurement into place()'s logical units.

        ``winfo_height`` / ``place_info`` report physical pixels, while
        ``place(y=...)`` multiplies its argument by the widget scaling factor.
        Mixing the two makes the curtain jump or overshoot on any display whose
        scaling is not 100%.
        """
        try:
            return int(self._reverse_widget_scaling(value))
        except Exception:
            return int(value)

    def _fade_out(self) -> None:
        if not self._alive or not self.winfo_exists():
            return

        height = self._to_logical(self.winfo_height() or 900)
        try:
            raw_y = self.place_info().get("y", 0)
            self._place_y = self._to_logical(float(raw_y or 0))
        except Exception:
            self._place_y = 0
        step = max(24, height // 12)
        self._slide(height, step)

    def _slide(self, height: int, step: int) -> None:
        if not self._alive or not self.winfo_exists():
            return
        self._place_y -= step
        if self._place_y > -height:
            self.place(y=self._place_y)
            self.after(12, self._slide, height, step)
        else:
            self._complete()

    def _complete(self) -> None:
        if self._completed:
            return
        self._completed = True
        self._alive = False
        self._logo_frames = []
        try:
            self.destroy()
        except Exception:
            pass
        if self.on_complete:
            self.on_complete()

    def destroy(self):
        self._alive = False
        self._logo_frames = []
        super().destroy()
