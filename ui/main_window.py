import customtkinter as ctk
import ui.theme as theme
from ui.reservation_form import ReservationForm
from ui.log_panel import LogPanel
from engines.zeroworld_engine import ZeroWorldEngine
from engines.jigubyeol_engine import JigubyeolEngine
from PIL import Image
import os
import json
import winsound
import tkinter.messagebox as messagebox

def resource_path(relative_path):
    import sys
    import os
    try:
        base_path = sys._MEIPASS
    except Exception:
        base_path = os.path.abspath(".")
    return os.path.join(base_path, relative_path)

class SuccessDialog(ctk.CTkToplevel):
    def __init__(self, parent, title="예약 성공", message="축하합니다! 방탈출 예약에 성공하였습니다."):
        super().__init__(parent)
        self.title(title)
        self.geometry("380x180")
        self.resizable(False, False)
        self.configure(fg_color=theme.CANVAS_COLOR)
        
        # Enable OS window frame (not borderless) to ensure IME focus works correctly
        self.transient(parent)
        
        # Center on parent window
        parent.update_idletasks()
        parent_x = parent.winfo_x()
        parent_y = parent.winfo_y()
        parent_width = parent.winfo_width()
        parent_height = parent.winfo_height()
        
        x = parent_x + (parent_width - 380) // 2
        y = parent_y + (parent_height - 180) // 2
        self.geometry(f"+{x}+{y}")
        
        # Content frame
        content_frame = ctk.CTkFrame(self, fg_color="transparent")
        content_frame.pack(fill="both", expand=True, padx=20, pady=15)
        
        # Checkmark Icon / Emblem
        icon_label = ctk.CTkLabel(
            content_frame,
            text="✓",
            font=(theme.FONT_FAMILY, 32, "bold"),
            text_color=theme.ACCENT_GREEN,
            width=50,
            height=50,
            fg_color=theme.SURFACE_COLOR,
            corner_radius=25
        )
        icon_label.pack(pady=(5, 10))
        
        # Message Label
        msg_label = ctk.CTkLabel(
            content_frame,
            text=message,
            font=(theme.FONT_FAMILY, 12),
            text_color=theme.TEXT_PRIMARY,
            wraplength=340
        )
        msg_label.pack(pady=(0, 15))
        
        # OK Button
        self.ok_btn = ctk.CTkButton(
            content_frame,
            text="확인",
            font=(theme.FONT_FAMILY, 12, "bold"),
            text_color=theme.TEXT_DARK,
            fg_color=theme.ACCENT_WHITE,
            hover_color=theme.TEXT_BODY,
            corner_radius=theme.ROUNDED_MD,
            command=self._on_ok,
            height=30,
            width=100
        )
        self.ok_btn.pack()
        
        self.bind("<Return>", lambda e: self._on_ok())
        self.after(50, self._grab_focus)
        
    def _grab_focus(self):
        try:
            self.grab_set()
            self.focus_force()
        except Exception:
            pass
        
    def _on_ok(self):
        try:
            self.grab_release()
        except Exception:
            pass
        self.destroy()

class AddSiteDialog(ctk.CTkToplevel):
    def __init__(self, parent, success_callback):
        super().__init__(parent)
        self.parent = parent
        self.success_callback = success_callback
        
        self.title("커스텀 사이트 추가")
        self.geometry("380x250")
        self.resizable(False, False)
        self.configure(fg_color=theme.CANVAS_COLOR)
        
        # Enable OS window frame (not borderless) to ensure IME focus works correctly
        self.transient(parent)
        
        # Center on parent window
        parent.update_idletasks()
        parent_x = parent.winfo_x()
        parent_y = parent.winfo_y()
        parent_width = parent.winfo_width()
        parent_height = parent.winfo_height()
        
        x = parent_x + (parent_width - 380) // 2
        y = parent_y + (parent_height - 250) // 2
        self.geometry(f"+{x}+{y}")
        
        # Content frame
        self.content_frame = ctk.CTkFrame(self, fg_color="transparent")
        self.content_frame.pack(fill="both", expand=True, padx=20, pady=15)
        
        # Label & Entry for Site Name
        self.name_label = ctk.CTkLabel(
            self.content_frame,
            text="사이트 이름 (예: 지구별 홍대)",
            font=theme.FONT_BODY_SM,
            text_color=theme.TEXT_MUTE
        )
        self.name_label.pack(anchor="w", pady=(5, 1))
        
        self.name_entry = ctk.CTkEntry(
            self.content_frame,
            placeholder_text="사이트 이름을 입력하세요",
            fg_color=theme.ELEVATED_COLOR,
            border_color=theme.HAIRLINE_COLOR,
            text_color=theme.TEXT_PRIMARY,
            placeholder_text_color=theme.TEXT_DISABLED,
            corner_radius=theme.ROUNDED_MD,
            height=28
        )
        self.name_entry.pack(fill="x", pady=(0, 10))
        
        # Label & Entry for URL
        self.url_label = ctk.CTkLabel(
            self.content_frame,
            text="예약 페이지 URL",
            font=theme.FONT_BODY_SM,
            text_color=theme.TEXT_MUTE
        )
        self.url_label.pack(anchor="w", pady=(0, 1))
        
        self.url_entry = ctk.CTkEntry(
            self.content_frame,
            placeholder_text="https://example.com/reservation",
            fg_color=theme.ELEVATED_COLOR,
            border_color=theme.HAIRLINE_COLOR,
            text_color=theme.TEXT_PRIMARY,
            placeholder_text_color=theme.TEXT_DISABLED,
            corner_radius=theme.ROUNDED_MD,
            height=28
        )
        self.url_entry.pack(fill="x", pady=(0, 15))
        
        # Action Buttons frame
        self.btn_frame = ctk.CTkFrame(self.content_frame, fg_color="transparent")
        self.btn_frame.pack(fill="x", side="bottom")
        
        self.cancel_btn = ctk.CTkButton(
            self.btn_frame,
            text="취소",
            font=(theme.FONT_FAMILY, 12, "bold"),
            text_color=theme.TEXT_BODY,
            fg_color=theme.SURFACE_COLOR,
            hover_color=theme.CARD_COLOR,
            corner_radius=theme.ROUNDED_MD,
            command=self._on_cancel,
            height=30,
            width=100
        )
        self.cancel_btn.pack(side="left")
        
        self.add_btn = ctk.CTkButton(
            self.btn_frame,
            text="등록",
            font=(theme.FONT_FAMILY, 12, "bold"),
            text_color=theme.TEXT_DARK,
            fg_color=theme.ACCENT_WHITE,
            hover_color=theme.TEXT_BODY,
            corner_radius=theme.ROUNDED_MD,
            command=self._on_add,
            height=30,
            width=100
        )
        self.add_btn.pack(side="right")
        
        # Status/Loading indicator
        self.status_label = ctk.CTkLabel(
            self.content_frame,
            text="",
            font=theme.FONT_BODY_SM,
            text_color=theme.ACCENT_YELLOW
        )
        self.status_label.pack(side="bottom", pady=(0, 10))
        
        self.after(50, self._grab_focus)
        
    def _grab_focus(self):
        try:
            self.grab_set()
            self.focus_force()
        except Exception:
            pass
            
    def _on_cancel(self):
        try:
            self.grab_release()
        except Exception:
            pass
        self.destroy()
        
    def _on_add(self):
        site_name = self.name_entry.get().strip()
        url = self.url_entry.get().strip()
        
        if not site_name:
            self.status_label.configure(text="⚠️ 사이트 이름을 입력해주세요.", text_color=theme.ACCENT_RED)
            return
        if not url:
            self.status_label.configure(text="⚠️ 예약 페이지 URL을 입력해주세요.", text_color=theme.ACCENT_RED)
            return
            
        self.status_label.configure(text="🔄 사이트 구조 분석 중...", text_color=theme.ACCENT_YELLOW)
        self.add_btn.configure(state="disabled")
        self.cancel_btn.configure(state="disabled")
        
        # Parse in a background thread to prevent UI freezing
        import threading
        def parse_thread():
            from engines.site_parser import parse_booking_site
            try:
                result = parse_booking_site(url, site_name)
                self.parent.after(0, lambda: self._on_parse_success(result))
            except Exception as e:
                self.parent.after(0, lambda: self._on_parse_error(str(e)))
                
        t = threading.Thread(target=parse_thread, name="SiteParserThread")
        t.daemon = True
        t.start()
        
    def _on_parse_success(self, result):
        self.status_label.configure(text="✓ 분석 완료! 저장 중...", text_color=theme.ACCENT_GREEN)
        self.success_callback(result)
        self._on_cancel()
        
    def _on_parse_error(self, error_msg):
        self.status_label.configure(text=f"⚠️ {error_msg}", text_color=theme.ACCENT_RED)
        self.add_btn.configure(state="normal")
        self.cancel_btn.configure(state="normal")

class MainWindow(ctk.CTk):
    def __init__(self):
        super().__init__()

        # Window Config
        self.title("방탈출 집중예약")
        
        # Center the window on startup
        width = 480
        height = 860
        screen_width = self.winfo_screenwidth()
        screen_height = self.winfo_screenheight()
        x = (screen_width - width) // 2
        y = (screen_height - height) // 2
        self.geometry(f"{width}x{height}+{x}+{y}")
        
        self.resizable(False, False)
        self.configure(fg_color=theme.CANVAS_COLOR)

        # Make Window Borderless
        self.overrideredirect(True)
        self.after(10, self.set_appwindow)
        self.protocol("WM_DELETE_WINDOW", self._on_close)
        
        # Bind restore event to detect window mapping
        self.bind("<Map>", self._on_restore)
        self._minimized = False
        self._is_maximized = False

        # Active engine tracking
        self.active_engine = None
        self.is_pinned = False

        # Attempt counter & status tracking
        self.attempt_count = 0
        self.current_status = "idle"

        # Log buffering
        import threading
        self.log_queue = []
        self.log_queue_lock = threading.Lock()
        self.is_flusher_running = False

        # Dragging variables
        self.drag_x = 0
        self.drag_y = 0

        # Set Window Icon if exists
        icon_path = resource_path("icon.ico")
        if os.path.exists(icon_path):
            try:
                self.iconbitmap(icon_path)
            except Exception:
                pass

        # -------------------------------------------------------------
        # 1. Draggable Custom Title Bar (Mac Style)
        # -------------------------------------------------------------
        self.title_bar = ctk.CTkFrame(self, fg_color=theme.SURFACE_COLOR, height=36, corner_radius=0)
        self.title_bar.pack(fill="x", side="top")
        self.title_bar.pack_propagate(False)

        # Drag bindings for Title Bar
        self.title_bar.bind("<Button-1>", self.start_drag)
        self.title_bar.bind("<B1-Motion>", self.drag)

        # Title Label in the Center
        self.title_label = ctk.CTkLabel(
            self.title_bar,
            text="방탈출 집중예약",
            font=(theme.FONT_FAMILY, 11, "bold"),
            text_color=theme.TEXT_BODY
        )
        self.title_label.place(relx=0.5, rely=0.5, anchor="center")
        self.title_label.bind("<Button-1>", self.start_drag)
        self.title_label.bind("<B1-Motion>", self.drag)

        # macOS Traffic Light Buttons Container on the Right
        dots_frame = ctk.CTkFrame(self.title_bar, fg_color="transparent")
        dots_frame.pack(side="right", padx=12, pady=8)

        # Minimize Button (Yellow)
        self.min_btn = ctk.CTkButton(
            dots_frame,
            text="",
            width=14,
            height=14,
            corner_radius=7,
            fg_color="#ffbd2e",
            hover_color="#e0a624",
            command=self._on_minimize
        )
        self.min_btn.pack(side="left", padx=4)

        # Maximize Button (Green)
        self.max_btn = ctk.CTkButton(
            dots_frame,
            text="",
            width=14,
            height=14,
            corner_radius=7,
            fg_color="#27c93f",
            hover_color="#1fa232",
            command=self._on_maximize
        )
        self.max_btn.pack(side="left", padx=4)

        # Close Button (Red)
        self.close_btn = ctk.CTkButton(
            dots_frame,
            text="",
            width=14,
            height=14,
            corner_radius=7,
            fg_color="#ff5f56",
            hover_color="#e04f47",
            command=self._on_close
        )
        self.close_btn.pack(side="left", padx=4)

        # Pin (Always on Top) Button on the Left
        self.pin_btn = ctk.CTkButton(
            self.title_bar,
            text="📌",
            width=26,
            height=22,
            corner_radius=6,
            fg_color="transparent",
            hover_color=theme.CARD_COLOR,
            font=(theme.FONT_FAMILY, 11),
            text_color=theme.TEXT_MUTE,
            command=self._toggle_pin
        )
        self.pin_btn.pack(side="left", padx=(10, 0))

        # Titlebar Bottom Hairline Border
        title_divider = ctk.CTkFrame(self, height=1, fg_color=theme.HAIRLINE_COLOR)
        title_divider.pack(fill="x", side="top")

        # -------------------------------------------------------------
        # 2. Main Title Header Block (Tighter Padding)
        # -------------------------------------------------------------
        header_frame = ctk.CTkFrame(self, fg_color="transparent")
        header_frame.pack(fill="x", padx=20, pady=(15, 6))

        # Big Title
        self.big_title_label = ctk.CTkLabel(
            header_frame,
            text="방탈출 집중예약",
            font=(theme.FONT_FAMILY, 20, "bold"),
            text_color=theme.TEXT_PRIMARY
        )
        self.big_title_label.pack(anchor="center")

        # Subtitle
        self.subtitle_label = ctk.CTkLabel(
            header_frame,
            text="제로월드 & 지구별방탈출 통합 매크로",
            font=theme.FONT_BODY_SM,
            text_color=theme.TEXT_MUTE
        )
        self.subtitle_label.pack(anchor="center", pady=(1, 6))

        # Status Pill Badge
        self.status_badge = ctk.CTkLabel(
            header_frame,
            text="● 대기 중",
            font=(theme.FONT_FAMILY, 11, "bold"),
            text_color=theme.TEXT_PRIMARY,
            fg_color=theme.ELEVATED_COLOR,
            corner_radius=11,
            padx=12,
            pady=3,
            height=22
        )
        self.status_badge.pack(anchor="center")

        # Divider
        divider = ctk.CTkFrame(self, height=1, fg_color=theme.HAIRLINE_COLOR)
        divider.pack(fill="x", padx=20, pady=(3, 8))

        # -------------------------------------------------------------
        # 3. Site Selection OptionMenu & Add Button
        # -------------------------------------------------------------
        self.site_select_frame = ctk.CTkFrame(self, fg_color="transparent")
        self.site_select_frame.pack(fill="x", padx=20, pady=(0, 6))
        self.site_select_frame.columnconfigure(0, weight=1)
        self.site_select_frame.columnconfigure(1, weight=0)
        self.site_select_frame.columnconfigure(2, weight=0)

        # Load custom sites
        self.custom_sites = {}
        if os.path.exists("custom_sites.json"):
            try:
                with open("custom_sites.json", "r", encoding="utf-8") as f:
                    self.custom_sites = json.load(f)
            except Exception:
                pass

        # Load saved site config if exists, default to "제로월드 강남"
        saved_site = "제로월드 강남"
        if os.path.exists("config.json"):
            try:
                with open("config.json", "r", encoding="utf-8") as f:
                    config = json.load(f)
                    saved_site = config.get("site", "제로월드 강남")
            except Exception:
                pass

        self.site_var = ctk.StringVar(value=saved_site)
        
        # Build options list
        self.default_site_names = ["제로월드 강남", "제로월드 홍대", "지구별방탈출"]
        site_options = self.default_site_names + list(self.custom_sites.keys())
        
        # Fallback if saved site is no longer in options
        if saved_site not in site_options:
            saved_site = "제로월드 강남"
            self.site_var.set(saved_site)

        self.site_dropdown = ctk.CTkOptionMenu(
            self.site_select_frame,
            variable=self.site_var,
            values=site_options,
            command=self._on_site_change,
            fg_color=theme.SURFACE_COLOR,
            button_color=theme.SURFACE_COLOR,
            button_hover_color=theme.CARD_COLOR,
            dropdown_fg_color=theme.SURFACE_COLOR,
            dropdown_text_color=theme.TEXT_PRIMARY,
            dropdown_hover_color=theme.CARD_COLOR,
            text_color=theme.TEXT_PRIMARY,
            font=(theme.FONT_FAMILY, 11, "bold"),
            dropdown_font=(theme.FONT_FAMILY, 11, "bold"),
            corner_radius=theme.ROUNDED_MD,
            height=30,
            anchor="w"
        )
        self.site_dropdown.grid(row=0, column=0, sticky="ew", padx=(0, 6))

        self.add_site_btn = ctk.CTkButton(
            self.site_select_frame,
            text="+",
            width=30,
            height=30,
            font=(theme.FONT_FAMILY, 14, "bold"),
            text_color=theme.TEXT_PRIMARY,
            fg_color=theme.SURFACE_COLOR,
            hover_color=theme.CARD_COLOR,
            corner_radius=theme.ROUNDED_MD,
            command=self._open_add_site_dialog
        )
        self.add_site_btn.grid(row=0, column=1, sticky="e", padx=(0, 6))

        self.delete_site_btn = ctk.CTkButton(
            self.site_select_frame,
            text="-",
            width=30,
            height=30,
            font=(theme.FONT_FAMILY, 14, "bold"),
            text_color=theme.TEXT_PRIMARY,
            fg_color=theme.SURFACE_COLOR,
            hover_color=theme.CARD_COLOR,
            corner_radius=theme.ROUNDED_MD,
            command=self._delete_current_site,
            state="disabled"
        )
        self.delete_site_btn.grid(row=0, column=2, sticky="e")

        # -------------------------------------------------------------
        # 4. Form Card Component
        # -------------------------------------------------------------
        self.form = ReservationForm(
            self,
            start_callback=self._start_booking,
            stop_callback=self._stop_booking
        )
        self.form.custom_sites = self.custom_sites
        self.form.pack(fill="x", padx=20, pady=(0, 6))
        self.form.set_site(saved_site)
        self.form.load_config()

        # -------------------------------------------------------------
        # 5. Full Width Primary CTA Button
        # -------------------------------------------------------------
        self.cta_btn = ctk.CTkButton(
            self,
            text="예약 시작",
            font=(theme.FONT_FAMILY, 14, "bold"),
            text_color=theme.TEXT_DARK,
            fg_color=theme.ACCENT_WHITE,
            hover_color=theme.TEXT_BODY,
            corner_radius=theme.ROUNDED_MD,
            command=self._toggle_cta,
            height=38
        )
        self.cta_btn.pack(fill="x", padx=20, pady=(0, 8))

        # -------------------------------------------------------------
        # 6. Terminal Logs Card Component
        # -------------------------------------------------------------
        self.log_panel = LogPanel(self)
        self.log_panel.pack(fill="both", expand=True, padx=20, pady=(0, 10))

        # Welcome message
        self.log_panel.append_log("프로그램이 준비되었습니다.", "info")

        # Start background theme parser for Jigubyeol to fetch any new themes automatically
        self._start_jigubyeol_theme_fetcher()
        self._update_delete_button_state(saved_site)

    # -------------------------------------------------------------
    # Dragging Functionality for Borderless Window
    # -------------------------------------------------------------
    def start_drag(self, event):
        self.drag_x = event.x
        self.drag_y = event.y

    def drag(self, event):
        x = self.winfo_x() + (event.x - self.drag_x)
        y = self.winfo_y() + (event.y - self.drag_y)
        self.geometry(f"+{x}+{y}")
        self.update_idletasks()  # Force coordinate system refresh for child popup menus

    def set_appwindow(self):
        self._apply_appwindow_style()
        try:
            self.withdraw()
            self.after(10, self.deiconify)
            self.after(20, self.update_idletasks)  # Refresh coordinates on startup
        except Exception:
            pass

    def _apply_appwindow_style(self):
        try:
            import ctypes
            GWL_EXSTYLE = -20
            WS_EX_APPWINDOW = 0x00040000
            WS_EX_TOOLWINDOW = 0x00000080
            
            hwnd = ctypes.windll.user32.GetParent(self.winfo_id())
            style = ctypes.windll.user32.GetWindowLongW(hwnd, GWL_EXSTYLE)
            style = style & ~WS_EX_TOOLWINDOW
            style = style | WS_EX_APPWINDOW
            ctypes.windll.user32.SetWindowLongW(hwnd, GWL_EXSTYLE, style)
            
            # Apply style change immediately via SetWindowPos
            # SWP_NOMOVE = 0x0002, SWP_NOSIZE = 0x0001, SWP_NOZORDER = 0x0004, SWP_FRAMECHANGED = 0x0020
            ctypes.windll.user32.SetWindowPos(hwnd, 0, 0, 0, 0, 0, 0x0002 | 0x0001 | 0x0004 | 0x0020)
        except Exception:
            pass

    def _on_minimize(self):
        self._minimized = True
        self.overrideredirect(False)
        self.iconify()

    def _on_restore(self, event):
        if event.widget == self and getattr(self, "_minimized", False):
            if self.state() == "iconic":
                return
            self._minimized = False
            # 약간의 딜레이를 주어 창 상태 복원이 완료된 후 borderless를 씌운다.
            self.after(100, self._restore_borderless)

    def _restore_borderless(self):
        self.overrideredirect(True)
        self._apply_appwindow_style()
        self.deiconify()
        self.focus_force()

    def _on_maximize(self):
        if getattr(self, "_is_maximized", False):
            self._is_maximized = False
            self.state("normal")
            self.overrideredirect(True)
            
            # Restore to original size and center it
            width = 480
            height = 860
            screen_width = self.winfo_screenwidth()
            screen_height = self.winfo_screenheight()
            x = (screen_width - width) // 2
            y = (screen_height - height) // 2
            self.geometry(f"{width}x{height}+{x}+{y}")
            self._apply_appwindow_style()
        else:
            self._is_maximized = True
            self.state("zoomed")

    def _on_site_change(self, site_name):
        self.form.set_site(site_name)
        self.form.save_config(site_name)
        self.log_panel.append_log(f"사이트가 '{site_name}'으로 변경되었습니다.", "info")
        self._update_delete_button_state(site_name)

    def _update_delete_button_state(self, site_name):
        if site_name in self.custom_sites:
            self.delete_site_btn.configure(state="normal", text_color=theme.ACCENT_RED)
        else:
            self.delete_site_btn.configure(state="disabled", text_color=theme.TEXT_PRIMARY)

    def _delete_current_site(self):
        current_site = self.site_var.get()
        if current_site in self.custom_sites:
            if messagebox.askyesno("사이트 삭제", f"정말로 '{current_site}' 사이트를 삭제하시겠습니까?"):
                # Remove from dict
                del self.custom_sites[current_site]
                
                # Save to JSON
                try:
                    with open("custom_sites.json", "w", encoding="utf-8") as f:
                        json.dump(self.custom_sites, f, ensure_ascii=False, indent=2)
                except Exception as e:
                    self.log_panel.append_log(f"설정 저장 중 오류: {e}", "error")
                
                # Refresh dropdown values
                site_options = self.default_site_names + list(self.custom_sites.keys())
                self.site_dropdown.configure(values=site_options)
                
                # Switch back to default
                self.site_var.set("제로월드 강남")
                self._on_site_change("제로월드 강남")
                self.log_panel.append_log(f"커스텀 사이트 '{current_site}'이(가) 삭제되었습니다.", "info")

    def _open_add_site_dialog(self):
        AddSiteDialog(self, self._on_custom_site_added)

    def _on_custom_site_added(self, site_data):
        site_name = site_data["name"]
        self.custom_sites[site_name] = site_data
        
        # Save to custom_sites.json
        try:
            with open("custom_sites.json", "w", encoding="utf-8") as f:
                json.dump(self.custom_sites, f, ensure_ascii=False, indent=2)
        except Exception as e:
            self.log_panel.append_log(f"설정 저장 중 오류: {e}", "error")
            
        # Refresh dropdown options
        site_options = self.default_site_names + list(self.custom_sites.keys())
        self.site_dropdown.configure(values=site_options)
        
        # Select newly added site
        self.site_var.set(site_name)
        self._on_site_change(site_name)
        
        self.log_panel.append_log(f"커스텀 사이트 '{site_name}'이(가) 등록되었습니다. (엔진 유형: {site_data['style']})", "success")

    def _on_close(self):
        try:
            site = self.site_var.get()
            self.form.save_config(site)
        except Exception:
            pass
        self.destroy()

    def _toggle_pin(self):
        self.is_pinned = not self.is_pinned
        self.attributes("-topmost", self.is_pinned)
        if self.is_pinned:
            self.pin_btn.configure(fg_color=theme.ELEVATED_COLOR, text_color=theme.ACCENT_YELLOW)
        else:
            self.pin_btn.configure(fg_color="transparent", text_color=theme.TEXT_MUTE)

    def _toggle_cta(self):
        if self.active_engine and self.active_engine.is_running:
            self._stop_booking()
        else:
            res_data, error_msg, threads = self.form.get_reservation_data()
            if error_msg:
                self.log_panel.append_log(f"입력 오류: {error_msg}", "error")
                self.current_status = "error"
                self.status_badge.configure(
                    text="● 에러 발생",
                    text_color=theme.TEXT_PRIMARY,
                    fg_color=theme.ACCENT_RED
                )
                return
            self._start_booking(res_data, threads)

    def _start_booking(self, reservation_data, threads):
        selected_site = self.site_var.get()
        self.form.save_config(selected_site)
        self.log_panel.clear_log()
        self.attempt_count = 0
        self.current_status = "running"
        self.log_panel.append_log(f"[{selected_site}] 예약을 시작합니다...", "info")

        self.cta_btn.configure(
            text="예약 중지",
            text_color=theme.TEXT_PRIMARY,
            fg_color=theme.ACCENT_RED,
            hover_color="#d64b4b"
        )
        
        self.status_badge.configure(
            text="● 동작 중",
            text_color=theme.TEXT_DARK,
            fg_color=theme.ACCENT_YELLOW
        )

        if selected_site in self.custom_sites:
            site_info = self.custom_sites[selected_site]
            if site_info["style"] == "jigubyeol":
                self.active_engine = JigubyeolEngine(
                    log_callback=self._on_engine_log,
                    success_callback=self._on_booking_success,
                    site_url=site_info["base_url"]
                )
            else:
                self.active_engine = ZeroWorldEngine(
                    site_url=site_info["url"],
                    log_callback=self._on_engine_log,
                    success_callback=self._on_booking_success
                )
        elif selected_site == "지구별방탈출":
            self.active_engine = JigubyeolEngine(
                log_callback=self._on_engine_log,
                success_callback=self._on_booking_success
            )
        else:
            url = "https://zerogangnam.com/reservation" if "강남" in selected_site else "https://www.zerohongdae.com/reservation"
            self.active_engine = ZeroWorldEngine(
                site_url=url,
                log_callback=self._on_engine_log,
                success_callback=self._on_booking_success
            )

        # Clear any leftover logs in the queue before starting
        with self.log_queue_lock:
            self.log_queue.clear()

        self.active_engine.start_reservation(reservation_data, threads)

    def _stop_booking(self):
        if self.active_engine and self.active_engine.is_running:
            self.current_status = "idle"
            self.active_engine.stop_reservation()
            self._reset_cta_state()
        else:
            self.log_panel.append_log("실행 중인 예약 작업이 없습니다.", "warning")

    def _reset_cta_state(self):
        self.cta_btn.configure(
            text="예약 시작",
            text_color=theme.TEXT_DARK,
            fg_color=theme.ACCENT_WHITE,
            hover_color=theme.TEXT_BODY
        )
        
        if self.current_status == "error":
            self.status_badge.configure(
                text="● 에러 발생",
                text_color=theme.TEXT_PRIMARY,
                fg_color=theme.ACCENT_RED
            )
        else:
            self.current_status = "idle"
            self.status_badge.configure(
                text="● 대기 중",
                text_color=theme.TEXT_PRIMARY,
                fg_color=theme.ELEVATED_COLOR
            )

    def _on_engine_log(self, message, log_type):
        if log_type == "error" or log_type == "success":
            self.attempt_count += 1
            
        with self.log_queue_lock:
            self.log_queue.append((message, log_type))
            
        if not self.is_flusher_running:
            self.is_flusher_running = True
            self.after(0, self._flush_logs)

    def _flush_logs(self):
        batch = []
        with self.log_queue_lock:
            if self.log_queue:
                batch = self.log_queue[:]
                self.log_queue.clear()
        
        if batch:
            self.log_panel.append_logs_batch(batch)
            
            # If there's an error log, set status to "에러 발생"
            has_error = any(log_type == "error" for _, log_type in batch)
            if has_error:
                self.current_status = "error"
                self.status_badge.configure(
                    text="● 에러 발생",
                    text_color=theme.TEXT_PRIMARY,
                    fg_color=theme.ACCENT_RED
                )
        
        self._check_engine_finished()
        
        if self.active_engine and self.active_engine.is_running:
            self.after(200, self._flush_logs)
        else:
            self.is_flusher_running = False
            # Check one last time if any new logs arrived between lock release and this check
            with self.log_queue_lock:
                if self.log_queue:
                    self.is_flusher_running = True
                    self.after(50, self._flush_logs)

    def _check_engine_finished(self):
        if self.active_engine and not self.active_engine.is_running:
            self._reset_cta_state()

    def _on_booking_success(self):
        self.current_status = "idle"
        self.after(0, self._trigger_success_notification)

    def _trigger_success_notification(self):
        self._reset_cta_state()
        try:
            winsound.PlaySound("SystemAsterisk", winsound.SND_ALIAS)
        except Exception:
            try:
                winsound.MessageBeep(winsound.MB_ICONASTERISK)
            except Exception:
                pass
        dialog = SuccessDialog(self, title="예약 성공", message="축하합니다! 방탈출 예약에 성공하였습니다. 웹사이트 또는 예약 내역을 확인해주세요.")

    def _start_jigubyeol_theme_fetcher(self):
        def fetch():
            import requests
            from bs4 import BeautifulSoup
            import urllib.parse
            
            url = "https://www.xn--2e0b040a4xj.com/theme"
            try:
                response = requests.get(url, timeout=10)
                if response.status_code == 200:
                    soup = BeautifulSoup(response.text, 'html.parser')
                    theme_sections = soup.find_all('section', class_='theme-item')
                    
                    new_themes = {}
                    for section in theme_sections:
                        btn_div = section.find('div', class_='theme-item-btn')
                        if not btn_div:
                            continue
                        a_tag = btn_div.find('a')
                        if not a_tag or 'href' not in a_tag.attrs:
                            continue
                            
                        href = a_tag['href']
                        parsed_url = urllib.parse.urlparse(href)
                        query_params = urllib.parse.parse_qs(parsed_url.query)
                        
                        branch_id = query_params.get('branch', [''])[0]
                        theme_pk = query_params.get('theme', [''])[0]
                        
                        if not branch_id or not theme_pk:
                            continue
                            
                        info_div = section.find('div', class_='eveThemeInfo')
                        if not info_div:
                            continue
                        h2_tag = info_div.find('h2')
                        if not h2_tag:
                            continue
                        name_span = h2_tag.find('span', class_='ff-bhs')
                        if not name_span:
                            continue
                        theme_name = name_span.text.strip()
                        
                        if branch_id not in new_themes:
                            new_themes[branch_id] = {}
                        new_themes[branch_id][theme_name] = theme_pk
                    
                    if new_themes:
                        from data.themes import JIGUBYEOL_THEMES
                        for b_id, themes in new_themes.items():
                            if b_id not in JIGUBYEOL_THEMES:
                                JIGUBYEOL_THEMES[b_id] = {}
                            JIGUBYEOL_THEMES[b_id].update(themes)
                        
                        self.after(0, self._refresh_themes_ui)
            except Exception:
                pass

        import threading
        t = threading.Thread(target=fetch, name="ThemeFetcher")
        t.daemon = True
        t.start()

    def _refresh_themes_ui(self):
        try:
            if self.site_var.get() == "지구별방탈출":
                self.form._update_theme_options()
        except Exception:
            pass
