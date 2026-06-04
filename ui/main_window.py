import customtkinter as ctk
import ui.theme as theme
from ui.reservation_form import ReservationForm
from ui.log_panel import LogPanel
from engines.zeroworld_engine import ZeroWorldEngine
from engines.jigubyeol_engine import JigubyeolEngine
from engines.naver_engine import NaverEngine
from PIL import Image
import os
import json
import time
from datetime import datetime
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

def animate_click(btn, original_height, original_width=None, callback=None):
    try:
        btn.configure(height=original_height - 2)
        if original_width:
            btn.configure(width=original_width - 4)
    except Exception:
        pass
        
    def restore():
        try:
            btn.configure(height=original_height)
            if original_width:
                btn.configure(width=original_width)
        except Exception:
            pass
        if callback:
            callback()
            
    btn.after(80, restore)

class LoadingOverlay(ctk.CTkFrame):
    def __init__(self, parent, on_complete):
        super().__init__(parent, fg_color=theme.CANVAS_COLOR, corner_radius=0)
        self.parent = parent
        self.on_complete = on_complete
        
        # Load local animation helpers
        import random
        import math
        from PIL import Image, ImageTk, ImageDraw, ImageFont, ImageFilter
        
        # Background animation Canvas
        self.canvas = ctk.CTkCanvas(self, bg=theme.CANVAS_COLOR, highlightthickness=0)
        self.canvas.pack(fill="both", expand=True)
        
        # Load local assets
        self.orig_img = None
        try:
            img_path = resource_path("app_icon.png")
            if os.path.exists(img_path):
                self.orig_img = Image.open(img_path).convert("RGBA")
                self.orig_img = self.orig_img.resize((100, 100), Image.Resampling.LANCZOS)
        except Exception:
            pass
            
        if self.orig_img is None:
            # Fallback drawing of 🐧 emoji on a transparent background
            try:
                # seguiemj is Windows standard color emoji font
                font = ImageFont.truetype("seguiemj.ttf", 64)
            except Exception:
                font = ImageFont.load_default()
            
            fallback = Image.new("RGBA", (100, 100), (0, 0, 0, 0))
            draw = ImageDraw.Draw(fallback)
            draw.text((50, 50), "🐧", fill="#ffffff", font=font, anchor="mm")
            self.orig_img = fallback
            
        self.time_counter = 0.0
        
        # UI controls placed relative to frame over canvas
        self.title_label = ctk.CTkLabel(
            self,
            text="방탈출 펭크로",
            font=(theme.FONT_FAMILY, 20, "bold"),
            text_color=theme.TEXT_PRIMARY,
            fg_color="transparent"
        )
        self.title_label.place(relx=0.5, rely=0.6, anchor="center")
        
        self.subtitle_label = ctk.CTkLabel(
            self,
            text="예약 엔진 로딩 및 최적화 중...",
            font=theme.FONT_BODY_SM,
            text_color=theme.TEXT_MUTE,
            fg_color="transparent"
        )
        self.subtitle_label.place(relx=0.5, rely=0.64, anchor="center")
        
        # Flat Premium Progress Bar
        self.progress = ctk.CTkProgressBar(
            self,
            width=200,
            height=4,
            corner_radius=2,
            fg_color=theme.ELEVATED_COLOR,
            progress_color=theme.ACCENT_BLUE
        )
        self.progress.set(0)
        self.progress.place(relx=0.5, rely=0.7, anchor="center")
        
        # Generate 12 ambient particles
        self.particles = []
        for _ in range(12):
            self.particles.append(self._create_particle(random_y=True))
            
        # Start animations
        self.after(16, self._update_animation)
        self.after(100, self._animate_progress, 0)
        
    def _create_particle(self, random_y=False):
        import random
        import math
        glyphs = ["❄️", "✨", "•", "*"]
        glyph = random.choice(glyphs)
        
        x = random.randint(10, 470)
        if random_y:
            y = random.randint(50, 750)
        else:
            y = random.randint(750, 840)
            
        speed_y = random.uniform(0.6, 2.2)
        speed_x = random.uniform(-0.5, 0.5)
        size = random.randint(8, 14) if glyph in ("❄️", "✨") else random.randint(3, 6)
        alpha = random.uniform(0.3, 0.9)
        decay = random.uniform(0.005, 0.015)
        phase = random.uniform(0, math.pi * 2)
        
        return {
            'x': x,
            'y': y,
            'speed_y': speed_y,
            'speed_x': speed_x,
            'size': size,
            'alpha': alpha,
            'decay': decay,
            'glyph': glyph,
            'phase': phase
        }
        
    def _update_animation(self):
        if not self.winfo_exists():
            return
            
        import math
        import random
        from PIL import Image, ImageTk, ImageDraw, ImageFilter
        
        self.canvas.delete("all")
        w = self.winfo_width()
        h = self.winfo_height()
        if w <= 1 or h <= 1:
            w, h = 480, 860
            
        self.time_counter += 0.026
        
        # Update & Draw particles
        for i in range(len(self.particles)):
            p = self.particles[i]
            p['y'] -= p['speed_y'] * 0.52
            p['x'] += (p['speed_x'] + 0.3 * math.sin(self.time_counter + p['phase'])) * 0.52
            p['alpha'] -= p['decay'] * 0.52
            
            # Re-spawn
            if p['y'] < 40 or p['alpha'] <= 0 or p['x'] < 0 or p['x'] > w:
                self.particles[i] = self._create_particle(random_y=False)
                p = self.particles[i]
                
            # Render to canvas (blend with black bg)
            intensity = int(p['alpha'] * 255)
            intensity = max(0, min(255, intensity))
            
            if p['glyph'] in ("❄️", "✨"):
                color = f"#{intensity:02x}{intensity:02x}{intensity:02x}"
                self.canvas.create_text(
                    p['x'], p['y'],
                    text=p['glyph'],
                    font=("Segoe UI", p['size']),
                    fill=color
                )
            else:
                # Accent blue shade: (87, 193, 255)
                r_val = int(p['alpha'] * 87)
                g_val = int(p['alpha'] * 193)
                b_val = int(p['alpha'] * 255)
                color = f"#{r_val:02x}{g_val:02x}{b_val:02x}"
                
                radius = p['size'] / 2
                self.canvas.create_oval(
                    p['x'] - radius, p['y'] - radius,
                    p['x'] + radius, p['y'] + radius,
                    fill=color, outline=""
                )
                
        # Draw Soft Glow & Pinned/Tilted Logo
        center_x = w / 2
        center_y = h / 2 - 80
        
        if self.orig_img:
            pulse = math.sin(self.time_counter)
            glow_radius = int(55 + 15 * pulse)
            glow_alpha = int(45 + 20 * pulse)
            
            # Alpha compositing canvas
            glow_canvas = Image.new("RGBA", (160, 160), (0, 0, 0, 0))
            draw = ImageDraw.Draw(glow_canvas)
            
            # Draw fuzzy circular gradient
            for r in range(glow_radius, 10, -5):
                alpha = int(glow_alpha * (1.0 - r / glow_radius))
                draw.ellipse(
                    (80 - r, 80 - r, 80 + r, 80 + r),
                    fill=(87, 193, 255, alpha)
                )
            glow_canvas = glow_canvas.filter(ImageFilter.GaussianBlur(8))
            
            # Tilting (Rotation)
            angle = 8 * math.sin(self.time_counter * 0.7)
            rotated_penguin = self.orig_img.rotate(angle, resample=Image.Resampling.BICUBIC)
            
            # Scale penguin slightly
            peng_size = int(84 + 8 * pulse)
            scaled_penguin = rotated_penguin.resize((peng_size, peng_size), Image.Resampling.LANCZOS)
            
            offset = (160 - peng_size) // 2
            glow_canvas.alpha_composite(scaled_penguin, (offset, offset))
            
            # Render to canvas
            self.tk_logo_img = ImageTk.PhotoImage(glow_canvas)
            self.canvas.create_image(center_x, center_y, image=self.tk_logo_img)
            
        self.after(16, self._update_animation)
        
    def _animate_progress(self, val):
        if not self.winfo_exists():
            return
        if val < 1.0:
            val += 0.038 # 1.3x slower (was 0.05)
            self.progress.set(val)
            delay = int(30 + (val * 90))
            self.after(delay, self._animate_progress, val)
        else:
            self.after(100, self._fade_out)
            
    def _fade_out(self):
        if not self.winfo_exists():
            return
        # Smoothly slide the overlay up
        h = self.winfo_height()
        step = max(5, int(h / 12))
        
        def slide():
            if not self.winfo_exists():
                return
            y = self.winfo_y()
            if abs(y) < h:
                self.place(y=y - step)
                self.after(12, slide)
            else:
                self.destroy()
                self.on_complete()
        slide()

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

        # URL Validation based on Engine Mode
        current_mode = self.parent.form.engine_mode_btn.get()
        if current_mode == "네이버 (Playwright)":
            from engines.site_parser import normalize_naver_url
            normalized_url = normalize_naver_url(url)
            if not normalized_url:
                self.status_label.configure(text="⚠️ 올바른 네이버 예약 또는 지도 URL이 아닙니다.", text_color=theme.ACCENT_RED)
                return
            url = normalized_url
            self.url_entry.delete(0, "end")
            self.url_entry.insert(0, url)
        else:
            if any(p in url for p in ["booking.naver.com", "naver.me", "map.naver.com", "place.naver.com"]):
                self.status_label.configure(text="⚠️ 네이버 예약은 '네이버 (Playwright)' 모드에서 등록해주세요.", text_color=theme.ACCENT_RED)
                return
            
        def proceed():
            self.status_label.configure(text="🔄 사이트 구조 분석 중...", text_color=theme.ACCENT_YELLOW)
            self.add_btn.configure(state="disabled")
            self.cancel_btn.configure(state="disabled")
            
            # Start dots animation
            self._parsing_in_progress = True
            self.animate_dots(1)
            
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
            
        animate_click(self.add_btn, 30, 100, proceed)
        
    def animate_dots(self, count=1):
        if not getattr(self, "_parsing_in_progress", False):
            return
        dots = "." * count
        self.status_label.configure(text=f"🔄 사이트 구조 분석 중{dots}")
        next_count = 1 if count >= 3 else count + 1
        self.after(300, lambda: self.animate_dots(next_count))

    def _on_parse_success(self, result):
        self._parsing_in_progress = False
        self.status_label.configure(text="✓ 분석 완료! 저장 중...", text_color=theme.ACCENT_GREEN)
        self.success_callback(result)
        self._on_cancel()
        
    def _on_parse_error(self, error_msg):
        self._parsing_in_progress = False
        self.status_label.configure(text=f"⚠️ {error_msg}", text_color=theme.ACCENT_RED)
        self.add_btn.configure(state="normal")
        self.cancel_btn.configure(state="normal")

class MainWindow(ctk.CTk):
    def __init__(self):
        super().__init__()

        # Window Config
        self.title("방탈출 펭크로")
        
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
        
        self._is_maximized = False

        # Active engine tracking
        self.active_engine = None
        self.is_pinned = False
        
        # Naver Server Time synchronization state
        self.naver_time_offset = 0.0
        self.is_sync_running = False

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
            text="방탈출 펭크로",
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
            text="방탈출 펭크로",
            font=(theme.FONT_FAMILY, 20, "bold"),
            text_color=theme.TEXT_PRIMARY
        )
        self.big_title_label.pack(anchor="center", pady=(0, 6))

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

        # Naver Server Time Label (initially hidden)
        self.server_time_label = ctk.CTkLabel(
            header_frame,
            text="네이버 서버 시간: 동기화 중...",
            font=(theme.FONT_FAMILY, 12, "bold"),
            text_color=theme.ACCENT_YELLOW,
            fg_color="transparent"
        )

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
        self.last_logged_site = saved_site
        self.last_logged_mode = None
        self.last_standard_site = saved_site
        self.last_naver_site = None
        
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
            stop_callback=self._stop_booking,
            mode_callback=self._on_engine_mode_change
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

        # Show premium loading splash overlay
        self.loading_overlay = LoadingOverlay(self, self._on_loading_complete)
        self.loading_overlay.place(x=0, y=36, relwidth=1, relheight=1)

    def _on_loading_complete(self):
        # Refresh theme UI once loading is completely done
        self._refresh_themes_ui()

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
            self.after(50, self.bring_to_front)    # Force window to front/foreground on launch
        except Exception:
            pass

    def bring_to_front(self):
        try:
            import ctypes
            hwnd = ctypes.windll.user32.GetParent(self.winfo_id())
            
            # Attach current thread input to the active foreground window thread to bypass Foreground Lock policy
            fore_hwnd = ctypes.windll.user32.GetForegroundWindow()
            fore_thread = 0
            curr_thread = 0
            if fore_hwnd:
                fore_thread = ctypes.windll.user32.GetWindowThreadProcessId(fore_hwnd, 0)
                curr_thread = ctypes.windll.kernel32.GetCurrentThreadId()
                if fore_thread != curr_thread:
                    ctypes.windll.user32.AttachThreadInput(curr_thread, fore_thread, True)
                    
            # 1. Bring window to topmost layout (HWND_TOPMOST = -1)
            ctypes.windll.user32.SetWindowPos(hwnd, -1, 0, 0, 0, 0, 0x0001 | 0x0002 | 0x0040)
            
            # 2. Force focus activation
            ctypes.windll.user32.SetForegroundWindow(hwnd)
            ctypes.windll.user32.BringWindowToTop(hwnd)
            ctypes.windll.user32.SetFocus(hwnd)
            
            # 3. Detach thread input
            if fore_hwnd and fore_thread != curr_thread:
                ctypes.windll.user32.AttachThreadInput(curr_thread, fore_thread, False)
                
            # 4. Remove topmost status after a short 100ms delay to let DWM render it in front
            if not getattr(self, "is_pinned", False):
                def remove_topmost():
                    try:
                        if not getattr(self, "is_pinned", False):
                            # HWND_NOTOPMOST = -2
                            ctypes.windll.user32.SetWindowPos(hwnd, -2, 0, 0, 0, 0, 0x0001 | 0x0002 | 0x0040)
                    except Exception:
                        pass
                self.after(100, remove_topmost)
        except Exception:
            try:
                self.attributes("-topmost", True)
                if not getattr(self, "is_pinned", False):
                    self.after(100, lambda: self.attributes("-topmost", False))
                self.focus_force()
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
        try:
            import ctypes
            hwnd = ctypes.windll.user32.GetParent(self.winfo_id())
            # SW_MINIMIZE = 6
            ctypes.windll.user32.ShowWindow(hwnd, 6)
        except Exception:
            self.iconify()

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
            self.update_idletasks()
        else:
            self._is_maximized = True
            self.state("zoomed")
            self.update_idletasks()

    def _on_site_change(self, site_name):
        self.form.set_site(site_name)
        self.form.save_config(site_name)
        
        # Track active site history based on the selected mode
        current_mode = self.form.engine_mode_btn.get()
        if current_mode == "네이버 (Playwright)":
            self.last_naver_site = site_name
        else:
            self.last_standard_site = site_name
            
        if (
            getattr(self, "last_logged_site", None) != site_name
            and not getattr(self, "_suppress_site_log", False)
        ):
            if hasattr(self, "log_panel") and self.log_panel:
                self.log_panel.append_log(f"사이트가 '{site_name}'으로 변경되었습니다.", "info")
            self.last_logged_site = site_name
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
                
                # Refresh dropdown values with active filter
                current_mode = self.form.engine_mode_btn.get()
                if current_mode == "네이버 (Playwright)":
                    site_options = [k for k, v in self.custom_sites.items() if v.get("style") == "naver"]
                    fallback_site = site_options[0] if site_options else "(네이버 예약을 등록하세요)"
                else:
                    site_options = self.default_site_names + [k for k, v in self.custom_sites.items() if v.get("style") != "naver"]
                    fallback_site = "제로월드 강남"

                if not site_options:
                    site_options = [fallback_site]
                self.site_dropdown.configure(values=site_options)
                
                # Switch back to default
                self.site_var.set(fallback_site)
                self._on_site_change(fallback_site)
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
            
        # Refresh dropdown options with active mode filter
        current_mode = self.form.engine_mode_btn.get()
        if current_mode == "네이버 (Playwright)":
            site_options = [k for k, v in self.custom_sites.items() if v.get("style") == "naver"]
        else:
            site_options = self.default_site_names + [k for k, v in self.custom_sites.items() if v.get("style") != "naver"]
            
        self.site_dropdown.configure(values=site_options)
        
        # Select newly added site
        self.site_var.set(site_name)
        self._on_site_change(site_name)
        
        self.log_panel.append_log(f"커스텀 사이트 '{site_name}'이(가) 등록되었습니다. (엔진 유형: {site_data['style']})", "success")

    def _on_engine_mode_change(self, mode):
        # Log mode change if not redundant
        if getattr(self, "last_logged_mode", None) != mode:
            if hasattr(self, "log_panel") and self.log_panel:
                self.log_panel.append_log(f"예약 방식이 '{mode}'(으)로 변경되었습니다.", "info")
            self.last_logged_mode = mode

        # Suppress site-change logs while switching engine modes (the engine-mode
        # log above is sufficient context for the user).
        self._suppress_site_log = True
            
        # Filter site dropdown depending on active engine mode
        if mode == "네이버 (Playwright)":
            site_options = [k for k, v in self.custom_sites.items() if v.get("style") == "naver"]
            if not site_options:
                target_site = "(네이버 예약을 등록하세요)"
                site_options = [target_site]
            else:
                if getattr(self, "last_naver_site", None) in site_options:
                    target_site = self.last_naver_site
                else:
                    target_site = site_options[0]
                    
            self.site_var.set(target_site)
            self._on_site_change(target_site)
            self.site_dropdown.configure(values=site_options)
            
            # Show Naver server time label and start synchronization loop
            self.server_time_label.pack(anchor="center", pady=(5, 0))
            if not self.is_sync_running:
                self.is_sync_running = True
                import threading
                t = threading.Thread(target=self._sync_naver_server_time, name="NaverTimeSyncThread")
                t.daemon = True
                t.start()
                self._update_server_time_clock()
        else:
            site_options = self.default_site_names + [k for k, v in self.custom_sites.items() if v.get("style") != "naver"]
            if getattr(self, "last_standard_site", None) in site_options:
                target_site = self.last_standard_site
            else:
                target_site = "제로월드 강남"
                
            self.site_var.set(target_site)
            self._on_site_change(target_site)
            self.site_dropdown.configure(values=site_options)
            
            # Hide Naver server time and stop synchronization
            self.server_time_label.pack_forget()
            self.is_sync_running = False

        # Re-enable site-change logging for user-initiated site switches
        self._suppress_site_log = False

    def _sync_naver_server_time(self):
        import urllib.request
        import time
        from email.utils import parsedate_to_datetime
        
        while self.is_sync_running:
            try:
                req = urllib.request.Request("https://booking.naver.com", method="HEAD")
                start = time.perf_counter()
                with urllib.request.urlopen(req, timeout=3) as response:
                    latency = (time.perf_counter() - start) / 2
                    date_str = response.info().get("Date")
                    if date_str:
                        gmt_dt = parsedate_to_datetime(date_str)
                        server_time = gmt_dt.timestamp() + latency
                        self.naver_time_offset = server_time - time.time()
            except Exception:
                pass
            
            # Re-sync every 30 seconds
            for _ in range(30):
                if not self.is_sync_running:
                    break
                time.sleep(1)

    def _update_server_time_clock(self):
        if not self.is_sync_running:
            return
        
        now = time.time() + self.naver_time_offset
        now_dt = datetime.fromtimestamp(now)
        time_str = now_dt.strftime("네이버 서버 시간: %H:%M:%S.%f")[:-4] # keep milliseconds to 2 decimals
        self.server_time_label.configure(text=time_str)
        
        # Refresh every 100ms
        self.after(100, self._update_server_time_clock)

    def _on_close(self):
        self.is_sync_running = False
        try:
            if hasattr(self, 'active_engine') and self.active_engine and self.active_engine.is_running:
                self.active_engine.stop_reservation()
        except Exception:
            pass
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
        def proceed():
            if self.active_engine and self.active_engine.is_running:
                self._stop_booking()
            else:
                res_data, error_msg, threads, is_async = self.form.get_reservation_data()
                if error_msg:
                    self.log_panel.append_log(f"입력 오류: {error_msg}", "error")
                    self.current_status = "error"
                    self.status_badge.configure(
                        text="● 에러 발생",
                        text_color=theme.TEXT_PRIMARY,
                        fg_color=theme.ACCENT_RED
                    )
                    return
                self._start_booking(res_data, threads, is_async)
                
        animate_click(self.cta_btn, 38, callback=proceed)

    def _start_booking(self, reservation_data, threads, is_async):
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

        if self.form.engine_mode_btn.get() == "네이버 (Playwright)":
            self.active_engine = NaverEngine(
                log_callback=self._on_engine_log,
                success_callback=self._on_booking_success
            )
        elif selected_site in self.custom_sites:
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

        self.active_engine.status_callback = self._on_engine_status_update
        self.active_engine.log_batch_callback = self._on_engine_log_batch

        # Clear any leftover logs in the queue before starting
        with self.log_queue_lock:
            self.log_queue.clear()

        # Inject server time offset into reservation_data for NaverEngine
        if self.form.engine_mode_btn.get() == "네이버 (Playwright)":
            reservation_data['naver_time_offset'] = getattr(self, 'naver_time_offset', 0.0)

        self.active_engine.start_reservation(reservation_data, threads, is_async)

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

    def _on_engine_log_batch(self, batch):
        with self.log_queue_lock:
            self.log_queue.extend(batch)
            
        if not self.is_flusher_running:
            self.is_flusher_running = True
            self.after(0, self._flush_logs)

    def _on_engine_status_update(self, attempt_count, last_error):
        self.attempt_count = attempt_count
        if self.current_status == "running":
            self.after(0, lambda: self.status_badge.configure(
                text=f"● 동작 중 (시도: {attempt_count}회)",
                text_color=theme.TEXT_DARK,
                fg_color=theme.ACCENT_YELLOW
            ))

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
            self.after(30, self._flush_logs)
        else:
            self.is_flusher_running = False
            # Check one last time if any new logs arrived between lock release and this check
            with self.log_queue_lock:
                if self.log_queue:
                    self.is_flusher_running = True
                    self.after(30, self._flush_logs)

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
