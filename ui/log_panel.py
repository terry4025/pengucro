import customtkinter as ctk
import ui.theme as theme

class LogPanel(ctk.CTkFrame):
    def __init__(self, parent):
        super().__init__(
            parent,
            fg_color=theme.SURFACE_COLOR,
            border_color=theme.HAIRLINE_COLOR,
            border_width=1,
            corner_radius=theme.ROUNDED_LG
        )

        # Header Row
        header_frame = ctk.CTkFrame(self, fg_color="transparent")
        header_frame.pack(fill="x", padx=15, pady=(10, 5))

        # Title
        self.header_label = ctk.CTkLabel(
            header_frame,
            text="● TERMINAL LOGS",
            font=(theme.FONT_FAMILY, 11, "bold"),
            text_color=theme.TEXT_PRIMARY
        )
        self.header_label.pack(side="left")

        # Clear Button
        self.clear_btn = ctk.CTkButton(
            header_frame,
            text="Clear",
            font=theme.FONT_BODY_SM,
            text_color=theme.TEXT_MUTE,
            fg_color="transparent",
            hover_color=theme.CARD_COLOR,
            width=50,
            height=20,
            corner_radius=theme.ROUNDED_SM,
            command=self.clear_log
        )
        self.clear_btn.pack(side="right")

        # Copy Button
        self.copy_btn = ctk.CTkButton(
            header_frame,
            text="Copy",
            font=theme.FONT_BODY_SM,
            text_color=theme.TEXT_MUTE,
            fg_color="transparent",
            hover_color=theme.CARD_COLOR,
            width=50,
            height=20,
            corner_radius=theme.ROUNDED_SM,
            command=self.copy_log
        )
        self.copy_btn.pack(side="right", padx=(0, 4))

        # Scrollable Text Area
        self.textbox = ctk.CTkTextbox(
            self,
            fg_color=theme.CARD_COLOR,
            text_color=theme.TEXT_BODY,
            font=theme.FONT_MONO,
            border_color=theme.HAIRLINE_COLOR,
            border_width=1,
            corner_radius=theme.ROUNDED_MD
        )
        self.textbox.pack(fill="both", expand=True, padx=15, pady=(0, 15))

        # Configure tags for color logging
        self.textbox.configure(state="normal")
        self.textbox._textbox.tag_config("info", foreground=theme.TEXT_PRIMARY)
        self.textbox._textbox.tag_config("success", foreground=theme.ACCENT_GREEN)
        self.textbox._textbox.tag_config("error", foreground=theme.ACCENT_RED)
        self.textbox._textbox.tag_config("warning", foreground=theme.ACCENT_YELLOW)
        self.textbox.configure(state="disabled")

    def append_log(self, message, log_type="info"):
        self.append_logs_batch([(message, log_type)])

    def append_logs_batch(self, logs_list):
        self.textbox.configure(state="normal")
        for message, log_type in logs_list:
            self.textbox.insert("end", message + "\n", log_type)
            
        try:
            line_count = int(self.textbox._textbox.index('end-1c').split('.')[0])
            if line_count > 300:
                self.textbox.delete("1.0", f"{line_count - 300}.0")
        except Exception:
            pass
            
        self.textbox.see("end")
        self.textbox.configure(state="disabled")

    def clear_log(self):
        self.textbox.configure(state="normal")
        self.textbox.delete("1.0", "end")
        self.textbox.configure(state="disabled")

    def copy_log(self):
        content = self.textbox.get("1.0", "end").strip()
        if content:
            self.winfo_toplevel().clipboard_clear()
            self.winfo_toplevel().clipboard_append(content)
            # Brief visual feedback
            self.copy_btn.configure(text="Copied!", text_color=theme.ACCENT_GREEN)
            self.after(1500, lambda: self.copy_btn.configure(text="Copy", text_color=theme.TEXT_MUTE))
