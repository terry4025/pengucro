"""Global mitigation for scroll repaint artifacts (ghosting).

Why this exists
---------------
Users report that scrolling any CustomTkinter surface can leave remnants of the
previous frame on screen. The mechanism is that CTk renders every widget onto a
Tk canvas, and a scrolling canvas (or a canvas holding real child windows added
with ``create_window``) relies on the platform's damage-region bookkeeping to
decide what to repaint. When that bookkeeping and the actual movement disagree,
pixels from the previous frame survive.

An automated harness that scrolled ``CTkScrollableFrame``, ``CTkTextbox`` and
the application's own ``LogPanel`` -- with synthetic events, with rapid bursts
and with genuine Win32 ``WM_MOUSEWHEEL`` input -- could not reproduce the defect
on the development machine (1920x1080, 100% scaling). The same harness did prove
that asking Windows to invalidate the whole window tree produces a pixel-perfect
frame. So rather than restructure widgets against a symptom that cannot be
observed here, this module applies that invalidation automatically after a
scroll gesture settles.

When the repaint happens
------------------------
Ghosting is seen *while* scrolling, not after it. Repainting only once the
gesture settles would therefore leave the artifact on screen for the whole
duration of a long scroll. So the guard does both:

* during the gesture, at most once every ``THROTTLE_MS`` (~30 Hz), so the frame
  self-corrects continuously without one invalidation per wheel notch;
* once more ``SETTLE_MS`` after the last event, to catch the final frame.

Cost and safety
---------------
* Soft mode (default) passes ``RDW_INVALIDATE | RDW_ALLCHILDREN`` only -- no
  ``RDW_ERASE`` (which would flash the background) and no ``RDW_UPDATENOW``
  (which would repaint synchronously). The repaint rides the normal WM_PAINT
  cycle, so it cannot flicker.
* Strong mode adds ``RDW_ERASE | RDW_UPDATENOW`` for the case where the soft
  invalidation is not enough. It repaints synchronously and may flicker, so it
  is opt-in via ``config.json``: ``"scroll_repaint_strong": true``.
* No-op on non-Windows platforms and whenever the Win32 call is unavailable.
* Can be switched off entirely with ``"force_scroll_repaint": false``.
"""

from __future__ import annotations

import ctypes
import logging
import sys
import time
import tkinter
from ctypes import wintypes


logger = logging.getLogger(__name__)

RDW_INVALIDATE = 0x0001
RDW_ERASE = 0x0004
RDW_ALLCHILDREN = 0x0080
RDW_UPDATENOW = 0x0100

THROTTLE_MS = 33               # ~30 Hz ceiling while a gesture is in progress
SETTLE_MS = 60                 # Final repaint after the gesture stops


class ScrollRepaintGuard:
    def __init__(
        self,
        root: tkinter.Misc,
        settle_ms: int = SETTLE_MS,
        throttle_ms: int = THROTTLE_MS,
        strong: bool = False,
    ) -> None:
        self.root = root
        self.settle_ms = settle_ms
        self.throttle_ms = throttle_ms
        self.strong = strong
        self.enabled = sys.platform.startswith("win")
        self.repaint_count = 0
        self._pending = None
        self._last_repaint_ms = 0
        self._target = None
        self._bindings: list[tuple[str, str]] = []

    @property
    def _flags(self) -> int:
        flags = RDW_INVALIDATE | RDW_ALLCHILDREN
        if self.strong:
            flags |= RDW_ERASE | RDW_UPDATENOW
        return flags

    # -- lifecycle ----------------------------------------------------------
    def install(self) -> "ScrollRepaintGuard":
        if not self.enabled:
            return self
        for sequence in ("<MouseWheel>", "<B4-Motion>", "<B5-Motion>", "<B1-Motion>"):
            try:
                funcid = self.root.bind_all(sequence, self._on_scroll, add="+")
            except tkinter.TclError:
                continue
            if isinstance(funcid, str) and funcid:
                self._bindings.append((sequence, funcid))
        return self

    def uninstall(self) -> None:
        self._cancel()
        bindings, self._bindings = list(self._bindings), []
        for sequence, funcid in bindings:
            try:
                self.root._root()._unbind(("bind", "all", sequence), funcid)
            except Exception:
                continue

    # -- internals ----------------------------------------------------------
    def _cancel(self) -> None:
        if self._pending is not None:
            try:
                self.root.after_cancel(self._pending)
            except Exception:
                pass
            self._pending = None

    def _now_ms(self) -> int:
        return int(time.monotonic() * 1000)

    def _on_scroll(self, event) -> None:
        if not self.enabled:
            return
        widget = getattr(event, "widget", None)
        # B1-Motion fires for every drag in the app; only scrollbar drags are
        # interesting, and those are cheap to identify by widget class.
        if getattr(event, "type", None) is not None and str(event.type) == "6":
            if not self._is_scroll_surface(widget):
                return

        target = self._scroll_target(widget) or self.root
        self._target = target

        # Repaint during the gesture too, throttled, so a long scroll does not
        # display the artifact for its whole duration.
        now = self._now_ms()
        if now - self._last_repaint_ms >= self.throttle_ms:
            self._last_repaint_ms = now
            self._invalidate(target)

        # ...and once more when the gesture stops, for the final frame.
        self._cancel()
        try:
            self._pending = self.root.after(self.settle_ms, self._flush)
        except Exception:
            self._pending = None

    SCROLLABLE_CLASSES = ("canvas", "text", "listbox", "treeview")

    @classmethod
    def _scroll_target(cls, widget):
        """The scrolling viewport that contains the event widget.

        Invalidating the whole toplevel with RDW_ALLCHILDREN was measured at
        roughly 70x the cost of plain scrolling: Tk gives every widget its own
        child HWND, so it forced a repaint of every CTk canvas in the
        application. The artifact only ever lives inside the scrolling
        viewport, so the invalidation is scoped to that widget instead.

        The *outermost* match is returned, not the nearest one. Every CTk widget
        is itself built on a canvas, so a wheel event over a button inside a
        scrollable frame reports that button's private canvas as event.widget;
        stopping there would repaint the button and leave the viewport
        untouched.
        """
        current = widget
        outermost = None
        depth = 0
        while isinstance(current, tkinter.Misc) and depth < 32:
            try:
                name = current.winfo_class().lower()
            except tkinter.TclError:
                break
            if name in cls.SCROLLABLE_CLASSES:
                outermost = current
            current = getattr(current, "master", None)
            depth += 1
        return outermost

    @staticmethod
    def _is_scroll_surface(widget) -> bool:
        if not isinstance(widget, tkinter.Misc):
            return False
        try:
            name = widget.winfo_class().lower()
        except tkinter.TclError:
            return False
        return "scrollbar" in name or "canvas" in name

    def _flush(self) -> None:
        self._pending = None
        self._last_repaint_ms = self._now_ms()
        self._invalidate(getattr(self, "_target", None) or self.root)

    def _invalidate(self, widget: tkinter.Misc) -> None:
        if not self.enabled:
            return
        try:
            if not widget.winfo_exists():
                return
        except tkinter.TclError:
            return
        try:
            user32 = ctypes.windll.user32
            hwnd = widget.winfo_id()
            if widget is self.root:
                # The borderless root is reparented into a wrapper window.
                hwnd = user32.GetParent(hwnd) or hwnd
            user32.RedrawWindow(
                wintypes.HWND(hwnd), None, None, wintypes.UINT(self._flags)
            )
            self.repaint_count += 1
        except Exception as exc:  # pragma: no cover - platform dependent
            # One failure means the API is unusable here; stop trying.
            self.enabled = False
            logger.debug("Scroll repaint guard disabled: %s", exc)


def install_scroll_repaint_guard(
    root: tkinter.Misc,
    *,
    enabled: bool = True,
    strong: bool = False,
    settle_ms: int = SETTLE_MS,
    throttle_ms: int = THROTTLE_MS,
) -> ScrollRepaintGuard | None:
    """Attach the guard to a Tk root. Returns None when disabled."""
    if not enabled:
        return None
    guard = ScrollRepaintGuard(
        root, settle_ms=settle_ms, throttle_ms=throttle_ms, strong=strong
    )
    return guard.install()
