import customtkinter as ctk
import ui.theme as theme
import re

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
        self.textbox.pack(fill="both", expand=True, padx=15, pady=(0, 15))

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
                    family = theme.FONT_MONO[0] if isinstance(theme.FONT_MONO, tuple) else "Segoe UI"
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

    def append_log(self, message, log_type="info"):
        self.append_logs_batch([(message, log_type)])

    def append_logs_batch(self, logs_list):
        self.textbox.configure(state="normal")
        # Match optional timestamp [HH:MM:SS] followed by optional category [CategoryName]
        log_pattern = re.compile(r"^(\[(\d{2}:\d{2}:\d{2})\])?\s*(\[([^\]]+)\])?(.*)$")
        
        for message, log_type in logs_list:
            match = log_pattern.match(message)
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
