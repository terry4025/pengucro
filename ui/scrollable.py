"""A CTkScrollableFrame that cleans up after itself and repaints reliably.

Two defects in CustomTkinter 5.2.2 (``windows/widgets/ctk_scrollable_frame.py``)
are worked around here.

1. Leaked global bindings.
   ``__init__`` registers ``<MouseWheel>`` and the Shift press/release handlers
   with ``bind_all(..., add="+")``. Those live on the root window's ``all``
   bindtag, so ``destroy()`` -- which only tears down the Frame, appearance and
   scaling base classes -- never removes them. Every dialog that contains a
   scrollable frame therefore leaves a handler behind that still references a
   destroyed canvas, and each one runs on every wheel event anywhere in the
   application.

2. Repaint artifacts while scrolling.
   ``_set_scroll_increments`` sets ``yscrollincrement=1`` on Windows and the
   wheel handler then issues ``yview("scroll", -delta/6, "units")``. The frame
   inside the canvas is a real child window created with ``create_window``, and
   every CTk widget inside it is another real child window. Tk cannot blit
   embedded windows, so it moves them and invalidates the damaged region; when
   the invalidated region does not match the actual movement, pixels from the
   previous frame stay on screen. Scrolling with ``yview_moveto`` and no scroll
   increment repaints the whole viewport instead.

Only widgets that genuinely need scrolling should use this. A short list is
better served by a plain frame.
"""

from __future__ import annotations

import tkinter

import customtkinter as ctk

# Windows reports +/-120 per wheel notch. Upstream translated that into 20px,
# which makes lists feel stuck; three text rows is a more natural step.
SCROLL_PIXELS_PER_NOTCH = 54


class SafeScrollableFrame(ctk.CTkScrollableFrame):
    def __init__(self, master, **kwargs):
        # Recorded before super().__init__ so the upstream constructor's
        # bind_all calls are captured. Assigning an instance attribute shadows
        # the inherited method for the duration of the constructor.
        self._global_bindings: list[tuple[str, str]] = []
        self.bind_all = self._recording_bind_all
        try:
            super().__init__(master, **kwargs)
        finally:
            try:
                del self.bind_all
            except AttributeError:
                pass

        # Disable pixel-granular incremental scrolling (see module docstring).
        try:
            self._parent_canvas.configure(xscrollincrement=0, yscrollincrement=0)
        except tkinter.TclError:
            pass

    # -- binding bookkeeping ------------------------------------------------
    def _recording_bind_all(self, sequence=None, func=None, add=None):
        funcid = tkinter.Misc.bind_all(self, sequence, func, add)
        if func is not None and isinstance(funcid, str) and funcid:
            self._global_bindings.append((sequence, funcid))
        return funcid

    def _release_global_bindings(self) -> None:
        bindings, self._global_bindings = list(self._global_bindings), []
        root = None
        try:
            root = self._root()
        except Exception:
            return
        for sequence, funcid in bindings:
            # tkinter's private _unbind removes a single funcid and deletes the
            # Tcl command. unbind_all would drop every handler for the
            # sequence, including those of other live scrollable frames.
            try:
                root._unbind(("bind", "all", sequence), funcid)
            except Exception:
                try:
                    root.deletecommand(funcid)
                except Exception:
                    pass

    def destroy(self):
        self._release_global_bindings()
        super().destroy()

    # -- scrolling ----------------------------------------------------------
    def _set_scroll_increments(self):
        # Called from the upstream constructor before our own configure runs.
        # Overriding it keeps the increments at Tk's default of 0.
        return

    def _mouse_wheel_all(self, event):
        if not self._event_targets_this_frame(getattr(event, "widget", None)):
            return
        delta = getattr(event, "delta", 0)
        if not delta:
            return
        pixels = -int(delta / 120 * SCROLL_PIXELS_PER_NOTCH) or (-1 if delta > 0 else 1)
        axis = "x" if self._shift_pressed else "y"
        if self._orientation == "horizontal":
            axis = "x"
        elif self._orientation == "vertical" and axis == "x":
            return
        self._scroll_pixels(axis, pixels)

    def _scroll_pixels(self, axis: str, pixels: int) -> None:
        canvas = getattr(self, "_parent_canvas", None)
        if canvas is None:
            return
        try:
            if not canvas.winfo_exists():
                return
            first, last = canvas.xview() if axis == "x" else canvas.yview()
            if (first, last) == (0.0, 1.0):
                return
            box = canvas.bbox("all")
            if not box:
                return
            total = (box[2] - box[0]) if axis == "x" else (box[3] - box[1])
            if total <= 0:
                return
            visible = max(0.0, last - first)
            target = min(max(first + pixels / total, 0.0), max(0.0, 1.0 - visible))
            if axis == "x":
                canvas.xview_moveto(target)
            else:
                canvas.yview_moveto(target)
        except tkinter.TclError:
            # Canvas went away between the event and this call.
            return

    def _event_targets_this_frame(self, widget) -> bool:
        canvas = getattr(self, "_parent_canvas", None)
        if canvas is None or not isinstance(widget, tkinter.Misc):
            return False
        try:
            if not canvas.winfo_exists():
                return False
        except tkinter.TclError:
            return False
        seen = 0
        current = widget
        while current is not None and seen < 64:
            if current is canvas:
                return True
            current = getattr(current, "master", None)
            seen += 1
        return False
