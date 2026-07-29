import customtkinter as ctk
import ui.theme as theme
import re

# Compiled once at import instead of on every append. The old code rebuilt this
# pattern for each batch of log lines.
# Matches an optional [HH:MM:SS] timestamp followed by an optional [Category].
LOG_PATTERN = re.compile(r"^(\[(\d{2}:\d{2}:\d{2})\])?\s*(\[([^\]]+)\])?(.*)$")

MAX_LINES = 300
# Slack, in lines, allowed when deciding whether the view is "at the bottom".
# Tk's yview() fractions do not land exactly on 1.0 after see("end") -- the
# trailing phantom line and integer row heights leave a small remainder -- so a
# fixed tiny epsilon would latch auto-scroll off permanently after the first
# append.
BOTTOM_SLACK_LINES = 2.0


class LogPanel(ctk.CTkFrame):
    def __init__(self, parent):
        super().__init__(
            parent,
            fg_color=theme.SURFACE_COLOR,
            border_color=theme.HAIRLINE_COLOR,
            border_width=1,
            corner_radius=theme.ROUNDED_LG
        )

        # Number of committed lines, tracked directly instead of re-parsing
        # Tk's "end-1c" index string on every append.
        self._line_count = 0

        # Header Row
        header_frame = ctk.CTkFrame(self, fg_color="transparent")
        header_frame.pack(fill="x", padx=theme.SPACE_4, pady=(theme.SPACE_2, theme.SPACE_1))

        # Title
        self.header_label = ctk.CTkLabel(
            header_frame,
            text="● TERMINAL LOGS",
            font=(theme.FONT_FAMILY, 11, "bold"),
            text_color=theme.TEXT_PRIMARY
        )
        self.header_label.pack(side="left")

        ghost_button = {
            "font": theme.FONT_BODY_SM,
            "text_color": theme.TEXT_MUTE,
            "fg_color": "transparent",
            "hover_color": theme.CARD_COLOR,
            "width": 52,
            "height": 22,
            "corner_radius": theme.ROUNDED_SM,
        }

        # Clear Button
        self.clear_btn = ctk.CTkButton(
            header_frame, text="Clear", command=self.clear_log, **ghost_button
        )
        self.clear_btn.pack(side="right")

        # Copy Button
        self.copy_btn = ctk.CTkButton(
            header_frame, text="Copy", command=self.copy_log, **ghost_button
        )
        self.copy_btn.pack(side="right", padx=(0, theme.SPACE_1))

        # Shown only while the user has scrolled away from the bottom, so it is
        # obvious that new lines are still arriving.
        self.scroll_hint = ctk.CTkLabel(
            header_frame,
            text="",
            font=theme.FONT_CAPTION,
            text_color=theme.ACCENT_YELLOW,
            cursor="hand2",
        )
        self.scroll_hint.pack(side="right", padx=(0, theme.SPACE_2))
        self.scroll_hint.bind("<Button-1>", lambda _event: self.scroll_to_end())

        # Scrollable Text Area with high contrast terminal BG and Apple-style scrollbar
        self.textbox = ctk.CTkTextbox(
            self,
            fg_color="#050505",       # Deeper pitch black for high contrast
            text_color=theme.TEXT_BODY,
            font=theme.FONT_MONO,
            border_color=theme.HAIRLINE_COLOR,
            border_width=1,
            corner_radius=theme.ROUNDED_MD,
            scrollbar_button_color=theme.HAIRLINE_COLOR,
            scrollbar_button_hover_color=theme.CARD_COLOR
        )
        self.textbox.pack(
            fill="both", expand=True, padx=theme.SPACE_4, pady=(0, theme.SPACE_3)
        )

        # Configure tags for standard log content
        self.textbox.configure(state="normal")
        self.textbox._textbox.tag_config("info", foreground=theme.TEXT_PRIMARY)
        self.textbox._textbox.tag_config("success", foreground=theme.ACCENT_GREEN)
        self.textbox._textbox.tag_config("error", foreground=theme.ACCENT_RED)
        self.textbox._textbox.tag_config("warning", foreground=theme.ACCENT_YELLOW)

        # Configure tags for category brackets
        self.textbox._textbox.tag_config("cat_default", foreground=theme.TEXT_MUTE)
        self.textbox._textbox.tag_config("cat_captcha", foreground=theme.ACCENT_BLUE)
        self.textbox._textbox.tag_config("cat_warning", foreground=theme.ACCENT_YELLOW)
        self.textbox._textbox.tag_config("cat_error", foreground=theme.ACCENT_RED)
        self.textbox._textbox.tag_config("cat_success", foreground=theme.ACCENT_GREEN)
        self.textbox._textbox.tag_config("cat_device", foreground="#BF5AF2") # iOS system purple

        # Setup custom apply_font_scaling hook to keep tags and base font synchronized
        orig_apply_font_scaling = self.textbox._apply_font_scaling

        def custom_apply_font_scaling(font):
            scaled_font = orig_apply_font_scaling(font)

            if isinstance(scaled_font, (tuple, list)):
                family = scaled_font[0]
                size = scaled_font[1]
            else:
                try:
                    family = scaled_font.cget("family")
                    size = scaled_font.cget("size")
                except Exception:
                    family = theme.FONT_MONO[0] if isinstance(theme.FONT_MONO, tuple) else theme.FONT_MONO_FAMILY
                    size = theme.FONT_MONO[1] if isinstance(theme.FONT_MONO, tuple) else 11

            font_normal = (family, size, "normal")
            font_bold = (family, size, "bold")

            self.textbox._textbox.tag_config("info", font=font_normal)
            self.textbox._textbox.tag_config("success", font=font_normal)
            self.textbox._textbox.tag_config("error", font=font_normal)
            self.textbox._textbox.tag_config("warning", font=font_normal)

            self.textbox._textbox.tag_config("cat_default", font=font_normal)
            self.textbox._textbox.tag_config("cat_captcha", font=font_bold)
            self.textbox._textbox.tag_config("cat_warning", font=font_bold)
            self.textbox._textbox.tag_config("cat_error", font=font_bold)
            self.textbox._textbox.tag_config("cat_success", font=font_bold)
            self.textbox._textbox.tag_config("cat_device", font=font_bold)

            return scaled_font

        self.textbox._apply_font_scaling = custom_apply_font_scaling

        # Trigger initial configuration of tag fonts
        self.textbox._apply_font_scaling(self.textbox._font)

        self.textbox.configure(state="disabled")

    # ------------------------------------------------------------------
    # Scroll position handling
    # ------------------------------------------------------------------
    def _is_pinned_to_bottom(self) -> bool:
        """True when the view is at the end, so auto-scrolling is wanted.

        The previous implementation called see("end") on every batch, which
        yanked the view back down while the user was reading older lines.
        """
        try:
            _first, last = self.textbox._textbox.yview()
        except Exception:
            return True
        if last >= 1.0:
            return True
        # Express the tolerance in lines so it scales with the buffer length.
        slack = BOTTOM_SLACK_LINES / max(1, self._line_count)
        return last >= 1.0 - slack

    def _jump_to_end(self) -> None:
        # yview_moveto(1.0) rather than see("end"): see() only scrolls the
        # minimum needed to expose an index and, in a short viewport, was
        # observed to stop several lines short of the bottom, which then made
        # _is_pinned_to_bottom() report False and latched auto-scroll off.
        try:
            self.textbox._textbox.yview_moveto(1.0)
        except Exception:
            try:
                self.textbox.see("end")
            except Exception:
                pass

    def scroll_to_end(self):
        self._jump_to_end()
        self.scroll_hint.configure(text="")

    # ------------------------------------------------------------------
    # Appending
    # ------------------------------------------------------------------
    def append_log(self, message, log_type="info"):
        self.append_logs_batch([(message, log_type)])

    def append_logs_batch(self, logs_list):
        if not logs_list:
            return

        was_pinned = self._is_pinned_to_bottom()
        self.textbox.configure(state="normal")

        for message, log_type in logs_list:
            match = LOG_PATTERN.match(message)
            if match:
                ts_part = match.group(1)
                cat_part = match.group(3)
                cat_name = match.group(4)
                body = match.group(5)

                # 1. Insert timestamp in default muted style if present (append space to separate)
                if ts_part:
                    self.textbox.insert("end", ts_part + " ", "cat_default")

                # 2. Insert category bracket with highlighted styling if present
                if cat_part and cat_name:
                    tag = "cat_default"
                    if "YesCaptcha" in cat_name:
                        tag = "cat_captcha"
                    elif "경고" in cat_name:
                        tag = "cat_warning"
                    elif "에러" in cat_name or "실패" in cat_name:
                        tag = "cat_error"
                    elif "성공" in cat_name or "완료" in cat_name:
                        tag = "cat_success"
                    elif "기기" in cat_name:
                        tag = "cat_device"
                    self.textbox.insert("end", cat_part, tag)

                # 3. Insert main log message body
                self.textbox.insert("end", body + "\n", log_type)
            else:
                self.textbox.insert("end", message + "\n", log_type)

            self._line_count += 1

        self._trim_history()

        if was_pinned:
            self._jump_to_end()
            self.scroll_hint.configure(text="")
        else:
            self.scroll_hint.configure(text="↓ 새 로그")

        self.textbox.configure(state="disabled")

    def _trim_history(self):
        """Drop the oldest lines using the tracked counter."""
        excess = self._line_count - MAX_LINES
        if excess <= 0:
            return
        try:
            self.textbox.delete("1.0", f"{excess + 1}.0")
        except Exception:
            # Fall back to recounting if the index maths ever disagrees with Tk.
            try:
                self._line_count = int(self.textbox._textbox.index("end-1c").split(".")[0])
            except Exception:
                pass
            return
        self._line_count -= excess

    # ------------------------------------------------------------------
    def clear_log(self):
        self.textbox.configure(state="normal")
        self.textbox.delete("1.0", "end")
        self.textbox.configure(state="disabled")
        self._line_count = 0
        self.scroll_hint.configure(text="")

    def copy_log(self):
        content = self.textbox.get("1.0", "end").strip()
        if content:
            self.winfo_toplevel().clipboard_clear()
            self.winfo_toplevel().clipboard_append(content)
            # Brief visual feedback
            self.copy_btn.configure(text="Copied!", text_color=theme.ACCENT_GREEN)
            self.after(1500, lambda: self.copy_btn.configure(text="Copy", text_color=theme.TEXT_MUTE))
