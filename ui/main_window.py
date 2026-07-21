import customtkinter as ctk
import ui.theme as theme
from ui.reservation_form import ReservationForm
from ui.log_panel import LogPanel
from engines.registry import EngineRegistry
from engines.catalog_providers import (
    builtin_site_configs,
    catalog_to_site_config,
    default_providers,
    fallback_catalog,
    migrate_custom_sites,
)
from pengucro.catalog import CatalogService
from pengucro.models import LEGACY_MODE_MAP, NAVER_MODE, STANDARD_MODE, ReservationRequest
from pengucro.storage import load_json, save_json
from pengucro import __version__
from PIL import Image
import os
import queue
import threading
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
        # 3. Slide in animation start positions
        self.subtitle_y = 0.65
        
        self.subtitle_label = ctk.CTkLabel(
            self,
            text="로딩 중...",
            font=(theme.FONT_FAMILY, 14),
            text_color=theme.TEXT_MUTE,
            fg_color="transparent"
        )
        self.subtitle_label.place(relx=0.5, rely=self.subtitle_y, anchor="center")
        
        # 1. Custom progress bar state
        self.progress_val = 0.0
        self.shine_x = 0.0
        
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
            
        # 2. 3D Parallax Depth parameter
        depth = random.uniform(0.3, 1.0)
            
        speed_y = random.uniform(0.6, 2.2) * depth * 1.5
        speed_x = random.uniform(-0.5, 0.5) * depth
        size = random.randint(8, 14) if glyph in ("❄️", "✨") else random.randint(3, 6)
        size = max(2, int(size * depth))
        
        alpha = random.uniform(0.3, 0.9) * depth
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
            'phase': phase,
            'depth': depth
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
            w, h = 480, 960
            
        self.time_counter += 0.026
        
        # 3. Smooth slide-in interpolation for text labels
        self.subtitle_y = self.subtitle_y + (0.62 - self.subtitle_y) * 0.08
        self.subtitle_label.place(relx=0.5, rely=self.subtitle_y, anchor="center")
        
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
            
        # 1. Custom Progress Bar & Shine Sweep Drawing on Canvas
        progress_y = h * 0.7
        bar_start = center_x - 100
        bar_end = center_x + 100
        bar_width = 200
        
        # Draw background bar
        self.canvas.create_rectangle(
            bar_start, progress_y - 2,
            bar_end, progress_y + 2,
            fill=theme.ELEVATED_COLOR, outline=""
        )
        
        # Draw filled progress bar
        fill_end = bar_start + (bar_width * self.progress_val)
        if self.progress_val > 0.0:
            self.canvas.create_rectangle(
                bar_start, progress_y - 2,
                fill_end, progress_y + 2,
                fill=theme.ACCENT_BLUE, outline=""
            )
            
            # Update shine position sweep
            self.shine_x += 4.5
            if self.shine_x > bar_end + 50:
                self.shine_x = bar_start - 50
                
            # Draw glow shine overlay line
            for offset, color_val in [(-12, "#74c2ff"), (-6, "#bfe3ff"), (0, "#ffffff"), (6, "#bfe3ff"), (12, "#74c2ff")]:
                sx = self.shine_x + offset
                if bar_start <= sx <= fill_end:
                    self.canvas.create_line(
                        sx, progress_y - 2,
                        sx, progress_y + 2,
                        fill=color_val, width=2
                    )
            
        self.after(16, self._update_animation)
        
    def _animate_progress(self, val):
        if not self.winfo_exists():
            return
        if val < 1.0:
            val += 0.038 # 1.3x slower (was 0.05)
            self.progress_val = val
            delay = int(30 + (val * 90))
            self.after(delay, self._animate_progress, val)
        else:
            self.progress_val = 1.0
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
        self._parse_outcome = None
        
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
        if current_mode == NAVER_MODE:
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
                self.status_label.configure(text="⚠️ 네이버 예약은 '네이버 예약' 유형에서 등록해주세요.", text_color=theme.ACCENT_RED)
                return
            
        def proceed():
            self.status_label.configure(text="🔄 사이트 구조 분석 중...", text_color=theme.ACCENT_YELLOW)
            self.add_btn.configure(state="disabled")
            self.cancel_btn.configure(state="disabled")
            
            # Start dots animation
            self._parsing_in_progress = True
            self.animate_dots(1)
            
            # Parse in a background thread to prevent UI freezing
            def parse_thread():
                from engines.catalog_providers import analyze_booking_site
                try:
                    result = analyze_booking_site(url, site_name)
                    self._parse_outcome = ("success", result)
                except Exception as e:
                    self._parse_outcome = ("error", str(e))
                    
            t = threading.Thread(target=parse_thread, name="SiteParserThread")
            t.daemon = True
            t.start()
            self.after(50, self._poll_parse_result)
            
        animate_click(self.add_btn, 30, 100, proceed)

    def _poll_parse_result(self):
        if not self.winfo_exists():
            return
        if self._parse_outcome is None:
            self.after(50, self._poll_parse_result)
            return
        outcome, value = self._parse_outcome
        self._parse_outcome = None
        if outcome == "success":
            self._on_parse_success(value)
        else:
            self._on_parse_error(value)
        
    def animate_dots(self, count=1):
        if not getattr(self, "_parsing_in_progress", False):
            return
        dots = "." * count
        self.status_label.configure(text=f"🔄 사이트 구조 분석 중{dots}")
        next_count = 1 if count >= 3 else count + 1
        self.after(300, lambda: self.animate_dots(next_count))

    def _on_parse_success(self, result):
        self._parsing_in_progress = False
        branch_count = len(result.get("branches", {}))
        theme_count = sum(len(items) for items in result.get("themes", {}).values())
        engine_names = {
            "naver": "네이버 예약",
            "jigubyeol": "지구별 계열",
            "sinbiworld": "SinbiWeb 제로월드 계열",
            "doomescape": "둠이스케이프 계열",
            "keyescape": "키이스케이프 계열",
            "zeroworld_laravel": "Laravel 제로월드 계열",
        }
        engine_id = result.get("engine_id", "")
        style = engine_names.get(engine_id, engine_id or "알 수 없음")
        detection = result.get("detection", {})
        confidence = detection.get("confidence", 0)
        evidence = ", ".join(detection.get("evidence", [])[:3]) or "조회 호환성 검사"
        messagebox.showinfo(
            "호환 엔진 자동 선택",
            f"자동 선택 엔진: {style}\n신뢰도 참고 지표: {confidence}%\n근거: {evidence}\n"
            f"지점: {branch_count}개\n테마: {theme_count}개\n\n"
            "실제 카탈로그 조회 검증을 통과한 엔진으로 자동 등록합니다.",
            parent=self,
        )
        self.status_label.configure(text="✓ 분석 완료! 저장 중...", text_color=theme.ACCENT_GREEN)
        self.success_callback(result)
        self._on_cancel()
        
    def _on_parse_error(self, error_msg):
        self._parsing_in_progress = False
        self.status_label.configure(text=f"⚠️ {error_msg}", text_color=theme.ACCENT_RED)
        self.add_btn.configure(state="normal")
        self.cancel_btn.configure(state="normal")


class CatalogChangesDialog(ctk.CTkToplevel):
    def __init__(self, parent, site_name, changes, apply_callback):
        super().__init__(parent)
        self.apply_callback = apply_callback
        self.rows = []
        self.title("사이트 변경 검토")
        self.geometry("500x420")
        self.minsize(460, 340)
        self.configure(fg_color=theme.CANVAS_COLOR)
        self.transient(parent)

        ctk.CTkLabel(
            self,
            text=f"{site_name} · 확인 필요한 변경",
            font=(theme.FONT_FAMILY, 14, "bold"),
            text_color=theme.TEXT_PRIMARY,
        ).pack(anchor="w", padx=18, pady=(16, 4))
        ctk.CTkLabel(
            self,
            text="선택한 삭제 또는 ID 교체만 반영됩니다. 선택하지 않은 항목은 기존 설정을 유지합니다.",
            font=theme.FONT_BODY_SM,
            text_color=theme.TEXT_MUTE,
            wraplength=455,
            justify="left",
        ).pack(anchor="w", padx=18, pady=(0, 10))

        scroll = ctk.CTkScrollableFrame(
            self,
            fg_color=theme.SURFACE_COLOR,
            border_color=theme.HAIRLINE_COLOR,
            border_width=1,
            corner_radius=theme.ROUNDED_MD,
        )
        scroll.pack(fill="both", expand=True, padx=18, pady=(0, 10))
        for change in changes:
            variable = ctk.BooleanVar(value=False)
            action = "삭제" if change.kind == "removed" else "ID 교체"
            entity = "지점" if change.entity == "branch" else "테마"
            if change.kind == "id_changed":
                detail = f"{entity} {change.old_name}: {change.old_id} → {change.new_id}"
            else:
                detail = f"{entity} {change.old_name} ({change.old_id})"
            checkbox = ctk.CTkCheckBox(
                scroll,
                text=f"{action} · {detail}",
                variable=variable,
                font=theme.FONT_BODY_SM,
                text_color=theme.TEXT_BODY,
                checkbox_width=16,
                checkbox_height=16,
            )
            checkbox.pack(fill="x", anchor="w", padx=10, pady=7)
            self.rows.append((variable, change))

        buttons = ctk.CTkFrame(self, fg_color="transparent")
        buttons.pack(fill="x", padx=18, pady=(0, 14))
        ctk.CTkButton(
            buttons,
            text="보류 유지",
            width=105,
            fg_color=theme.SURFACE_COLOR,
            hover_color=theme.CARD_COLOR,
            command=self.destroy,
        ).pack(side="left")
        ctk.CTkButton(
            buttons,
            text="선택 항목 반영",
            width=125,
            text_color=theme.TEXT_DARK,
            fg_color=theme.ACCENT_WHITE,
            hover_color=theme.TEXT_BODY,
            command=self._apply,
        ).pack(side="right")

    def _apply(self):
        selected = [change for variable, change in self.rows if variable.get()]
        if selected:
            self.apply_callback(selected)
        self.destroy()

class MainWindow(ctk.CTk):
    def __init__(self):
        super().__init__()

        # Window Config
        self.title(f"방탈출 펭크로 v{__version__}")
        
        # Center the window on startup
        width = 520
        height = 900
        screen_width = self.winfo_screenwidth()
        screen_height = self.winfo_screenheight()
        x = (screen_width - width) // 2
        y = (screen_height - height) // 2
        self.geometry(f"{width}x{height}+{x}+{y}")
        
        self.minsize(480, 720)
        self.resizable(True, True)
        self.configure(fg_color=theme.CANVAS_COLOR)

        # Make Window Borderless
        self.overrideredirect(True)
        self.after(10, self.set_appwindow)
        self.protocol("WM_DELETE_WINDOW", self._on_close)
        
        self._is_maximized = False

        # Active engine tracking
        self.active_engine = None
        self._engine_completion_handled = True
        self.is_pinned = False
        
        # Naver Server Time synchronization state
        self.naver_time_offset = 0.0
        self.is_sync_running = False
        self._server_sync_generation = 0
        self._server_sync_thread = None

        # Attempt counter & status tracking
        self.attempt_count = 0
        self.current_status = "idle"
        self.booking_started_at = None
        self._status_timer_id = None

        # Worker threads only write to this queue. Tk widgets are touched by
        # the main-thread polling loop below.
        self.engine_event_queue = queue.Queue()
        self._ui_polling = True

        # Dragging variables
        self.drag_x = 0
        self.drag_y = 0

        # Set Window Icon if exists
        self._icon_path = resource_path("icon.ico")
        self._native_icon_handles = []
        if os.path.exists(self._icon_path):
            try:
                self.iconbitmap(self._icon_path)
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
            text=f"방탈출 펭크로 v{__version__}",
            font=(theme.FONT_FAMILY, 11, "bold"),
            text_color=theme.TEXT_BODY
        )
        self.title_label.place(relx=0.5, rely=0.5, anchor="center")
        self.title_label.bind("<Button-1>", self.start_drag)
        self.title_label.bind("<B1-Motion>", self.drag)

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

        # Accessible window controls
        dots_frame = ctk.CTkFrame(self.title_bar, fg_color="transparent")
        dots_frame.pack(side="right", padx=8, pady=4)

        self.min_btn = ctk.CTkButton(
            dots_frame,
            text="—",
            width=30,
            height=26,
            corner_radius=6,
            fg_color="transparent",
            hover_color=theme.CARD_COLOR,
            text_color=theme.TEXT_BODY,
            command=self._on_minimize,
        )
        self.min_btn.pack(side="left", padx=2)

        self.max_btn = ctk.CTkButton(
            dots_frame,
            text="□",
            width=30,
            height=26,
            corner_radius=6,
            fg_color="transparent",
            hover_color=theme.CARD_COLOR,
            text_color=theme.TEXT_BODY,
            command=self._on_maximize,
        )
        self.max_btn.pack(side="left", padx=2)

        # Close Button (Red)
        self.close_btn = ctk.CTkButton(
            dots_frame,
            text="×",
            width=30,
            height=26,
            corner_radius=6,
            fg_color="transparent",
            hover_color=theme.ACCENT_RED_HOVER,
            text_color=theme.TEXT_BODY,
            command=self._on_close
        )
        self.close_btn.pack(side="left", padx=2)

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
            text=f"방탈출 펭크로 v{__version__}",
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
        self.custom_sites = load_json("custom_sites.json", {})
        if not isinstance(self.custom_sites, dict):
            self.custom_sites = {}
        self.custom_sites, custom_sites_migrated = migrate_custom_sites(self.custom_sites)
        if custom_sites_migrated:
            save_json("custom_sites.json", self.custom_sites)

        self.catalog_service = CatalogService(default_providers())
        self._catalog_refresh_running = False
        self._catalog_applied_counts = {}
        self.catalog_configs = builtin_site_configs()
        for site_name, site_config in self.custom_sites.items():
            if isinstance(site_config, dict):
                site_config["name"] = site_name
                self.catalog_configs[site_name] = site_config
        for site_name, site_config in self.catalog_configs.items():
            try:
                self.catalog_service.seed_fallback(fallback_catalog(site_name, site_config))
            except Exception:
                continue
        for site_name, site_config in self.catalog_configs.items():
            cached = self.catalog_service.catalogs.get(site_config.get("catalog_key", ""))
            if cached:
                self._apply_catalog_to_runtime(site_name, cached, persist=False)

        # Load saved site config if exists, default to "제로월드 강남"
        saved_config = load_json("config.json", {})
        saved_site = saved_config.get("site", "제로월드") if isinstance(saved_config, dict) else "제로월드"
        if saved_site in {"제로월드(신)", "제로월드(구)", "제로월드 강남", "제로월드 홍대"}:
            saved_site = "제로월드"

        self.site_var = ctk.StringVar(value=saved_site)
        self.last_logged_site = saved_site
        self.last_logged_mode = None
        self.last_standard_site = saved_site
        self.last_naver_site = None
        
        # Build options list
        self.default_site_names = ["제로월드", "지구별방탈출", "키이스케이프", "둠이스케이프"]
        site_options = self.default_site_names + list(self.custom_sites.keys())
        
        # Fallback if saved site is no longer in options
        if saved_site not in site_options:
            saved_site = "제로월드"
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
        self.form._is_initializing = True
        self.form.set_site(saved_site)
        self.form.load_config()
        self._update_server_time_sync_state()

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

        self.resize_grip = ctk.CTkLabel(
            self,
            text="◢",
            width=20,
            height=20,
            text_color=theme.TEXT_DISABLED,
            cursor="size_nw_se",
        )
        self.resize_grip.place(relx=1, rely=1, anchor="se")
        self.resize_grip.bind("<Button-1>", self._start_resize)
        self.resize_grip.bind("<B1-Motion>", self._resize_window)

        self.bind("<Control-Return>", lambda _event: self._toggle_cta())
        self.bind("<Control-l>", lambda _event: self.log_panel.clear_log())
        self.bind("<Escape>", lambda _event: self._stop_booking() if self.current_status == "running" else None)
        self.after(30, self._drain_engine_events)

        # Welcome message
        self.log_panel.append_log("프로그램이 준비되었습니다.", "info")

        # Refresh all stale site catalogs after the UI is ready.
        self.after(200, self._start_catalog_auto_refresh)
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

    def _start_resize(self, event):
        self._resize_origin = (event.x_root, event.y_root, self.winfo_width(), self.winfo_height())

    def _resize_window(self, event):
        if self._is_maximized or not hasattr(self, "_resize_origin"):
            return
        start_x, start_y, width, height = self._resize_origin
        new_width = max(480, width + event.x_root - start_x)
        new_height = max(720, height + event.y_root - start_y)
        self.geometry(f"{new_width}x{new_height}")

    def set_appwindow(self):
        self._apply_appwindow_style()
        self._apply_native_window_icon()
        try:
            self.withdraw()
            self.after(10, self.deiconify)
            self.after(20, self._apply_native_window_icon)
            self.after(20, self.update_idletasks)  # Refresh coordinates on startup
            self.after(50, self.bring_to_front)    # Force window to front/foreground on launch
        except Exception:
            pass

    def _apply_native_window_icon(self):
        """Apply the ICO to the real Win32 taskbar window used by this borderless UI."""
        icon_path = getattr(self, "_icon_path", "")
        if not icon_path or not os.path.exists(icon_path):
            return

        try:
            import ctypes
            from ctypes import wintypes

            user32 = ctypes.windll.user32
            user32.GetParent.argtypes = [wintypes.HWND]
            user32.GetParent.restype = wintypes.HWND
            user32.LoadImageW.argtypes = [
                wintypes.HINSTANCE,
                wintypes.LPCWSTR,
                wintypes.UINT,
                ctypes.c_int,
                ctypes.c_int,
                wintypes.UINT,
            ]
            user32.LoadImageW.restype = wintypes.HANDLE
            user32.SendMessageW.argtypes = [
                wintypes.HWND,
                wintypes.UINT,
                wintypes.WPARAM,
                wintypes.LPARAM,
            ]
            user32.SendMessageW.restype = ctypes.c_ssize_t

            hwnd = user32.GetParent(self.winfo_id())
            if not hwnd:
                return

            IMAGE_ICON = 1
            LR_LOADFROMFILE = 0x0010
            WM_SETICON = 0x0080
            ICON_SMALL = 0
            ICON_BIG = 1
            SM_CXICON, SM_CYICON = 11, 12
            SM_CXSMICON, SM_CYSMICON = 49, 50

            big_icon = user32.LoadImageW(
                None,
                icon_path,
                IMAGE_ICON,
                user32.GetSystemMetrics(SM_CXICON),
                user32.GetSystemMetrics(SM_CYICON),
                LR_LOADFROMFILE,
            )
            small_icon = user32.LoadImageW(
                None,
                icon_path,
                IMAGE_ICON,
                user32.GetSystemMetrics(SM_CXSMICON),
                user32.GetSystemMetrics(SM_CYSMICON),
                LR_LOADFROMFILE,
            )

            if big_icon:
                user32.SendMessageW(hwnd, WM_SETICON, ICON_BIG, big_icon)
            if small_icon:
                user32.SendMessageW(hwnd, WM_SETICON, ICON_SMALL, small_icon)

            new_handles = [handle for handle in (big_icon, small_icon) if handle]
            old_handles = getattr(self, "_native_icon_handles", [])
            self._native_icon_handles = new_handles
            for handle in old_handles:
                if handle not in new_handles:
                    user32.DestroyIcon(handle)
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
            width = 520
            height = 900
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
        self._keyescape_time_verified = False
        
        # Track active site history based on the selected mode
        current_mode = self.form.engine_mode_btn.get()
        if current_mode == NAVER_MODE:
            self.last_naver_site = site_name
        else:
            self.last_standard_site = site_name
            
        # Manage server time visibility: toggle server time thread & label
        self._update_server_time_sync_state()

        if (
            getattr(self, "last_logged_site", None) != site_name
            and not getattr(self, "_suppress_site_log", False)
        ):
            if hasattr(self, "log_panel") and self.log_panel:
                self.log_panel.append_log(f"사이트가 '{site_name}'으로 변경되었습니다.", "info")
            self.last_logged_site = site_name
        self._update_delete_button_state(site_name)
        self._update_catalog_status(site_name)

    def _update_catalog_status(self, site_name):
        config = self.catalog_configs.get(site_name)
        if not config or not hasattr(self.form, "catalog_refresh_status"):
            return
        catalog = self.catalog_service.catalogs.get(config.get("catalog_key", ""))
        pending = len(self.catalog_service.pending_changes(config.get("catalog_key", "")))
        changed = self._catalog_applied_counts.get(site_name, 0)
        if catalog and catalog.last_success_at:
            stamp = catalog.last_success_at.replace("T", " ")[:16]
            text = f"최근 {stamp}"
        else:
            text = "갱신 기록 없음"
        self.form.set_catalog_refresh_state(
            text,
            pending_count=pending,
            changed_count=changed,
            busy=self._catalog_refresh_running,
        )

    def _update_server_time_sync_state(self):
        current_mode = self.form.engine_mode_btn.get()
        site_name = self.site_var.get()
        
        show_server_time = False
        if hasattr(self, "form") and hasattr(self.form, "show_server_time_checkbox"):
            show_server_time = (self.form.show_server_time_checkbox.get() == 1)
            
        is_supported_site = (current_mode == NAVER_MODE and site_name != "(네이버 예약을 등록하세요)") or (site_name == "키이스케이프")
        
        self._server_sync_generation += 1
        generation = self._server_sync_generation
        if show_server_time and is_supported_site:
            self.is_sync_running = True
            import threading
            self._server_sync_thread = threading.Thread(
                target=self._sync_server_time,
                args=(generation, site_name),
                name="ServerTimeSyncThread",
                daemon=True,
            )
            self._server_sync_thread.start()
            self._update_server_time_clock()
        else:
            self.is_sync_running = False
            self.server_time_label.pack_forget()
            self.server_time_label.configure(text="")

    def _update_delete_button_state(self, site_name):
        if site_name in self.custom_sites:
            self.delete_site_btn.configure(state="normal", text_color=theme.ACCENT_RED)
        else:
            self.delete_site_btn.configure(state="disabled", text_color=theme.TEXT_PRIMARY)

    def _delete_current_site(self):
        current_site = self.site_var.get()
        if current_site in self.custom_sites:
            if messagebox.askyesno("사이트 삭제", f"정말로 '{current_site}' 사이트를 삭제하시겠습니까?"):
                catalog_key = self.custom_sites[current_site].get("catalog_key", "")
                # Remove from dict
                del self.custom_sites[current_site]
                self.catalog_configs.pop(current_site, None)
                if catalog_key:
                    self.catalog_service.catalogs.pop(catalog_key, None)
                    self.catalog_service.store.save(self.catalog_service.catalogs)
                
                # Save to JSON
                try:
                    save_json("custom_sites.json", self.custom_sites)
                except Exception as e:
                    self.log_panel.append_log(f"설정 저장 중 오류: {e}", "error")
                
                # Refresh dropdown values with active filter
                current_mode = self.form.engine_mode_btn.get()
                if current_mode == NAVER_MODE:
                    site_options = [k for k, v in self.custom_sites.items() if v.get("style") == "naver"]
                    fallback_site = site_options[0] if site_options else "(네이버 예약을 등록하세요)"
                else:
                    site_options = self.default_site_names + [k for k, v in self.custom_sites.items() if v.get("style") != "naver"]
                    fallback_site = "제로월드"

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
        discovered_catalog = site_data.pop("catalog", None)
        site_name = site_data["name"]
        self.custom_sites[site_name] = site_data
        self.catalog_configs[site_name] = site_data
        if discovered_catalog is not None:
            self.catalog_service.catalogs[discovered_catalog.site_key] = discovered_catalog
            self.catalog_service.store.save(self.catalog_service.catalogs)
            self._apply_catalog_to_runtime(site_name, discovered_catalog, persist=False)
        
        # Save to custom_sites.json
        try:
            save_json("custom_sites.json", self.custom_sites)
        except Exception as e:
            self.log_panel.append_log(f"설정 저장 중 오류: {e}", "error")
            
        # Refresh dropdown options with active mode filter
        current_mode = self.form.engine_mode_btn.get()
        if current_mode == NAVER_MODE:
            site_options = [k for k, v in self.custom_sites.items() if v.get("style") == "naver"]
        else:
            site_options = self.default_site_names + [k for k, v in self.custom_sites.items() if v.get("style") != "naver"]
            
        self.site_dropdown.configure(values=site_options)
        
        # Select newly added site
        self.site_var.set(site_name)
        self._on_site_change(site_name)
        
        self.log_panel.append_log(f"커스텀 사이트 '{site_name}'이(가) 등록되었습니다. (엔진 유형: {site_data['style']})", "success")

    def _apply_catalog_to_runtime(self, site_name, catalog, persist=True):
        from data.themes import (
            DOOMESCAPE_THEMES,
            JIGUBYEOL_THEMES,
            KEYESCAPE_THEMES,
            SITES_CONFIG,
            ZEROWORLD_THEMES,
        )

        projected = catalog_to_site_config(catalog, rich_keyescape=site_name == "키이스케이프")
        if site_name in SITES_CONFIG:
            SITES_CONFIG[site_name].update(projected)
            SITES_CONFIG[site_name]["engine_id"] = catalog.engine_id
            SITES_CONFIG[site_name]["engine_options"] = catalog.metadata.get("engine_options", {})
            target = {
                "제로월드": ZEROWORLD_THEMES,
                "지구별방탈출": JIGUBYEOL_THEMES,
                "키이스케이프": KEYESCAPE_THEMES,
                "둠이스케이프": DOOMESCAPE_THEMES,
            }.get(site_name)
            if target is not None:
                target.clear()
                target.update(projected["themes"])
            if site_name in self.catalog_configs:
                self.catalog_configs[site_name].update(projected)
                self.catalog_configs[site_name]["engine_options"] = catalog.metadata.get("engine_options", {})
        elif site_name in self.custom_sites:
            self.custom_sites[site_name].update(projected)
            self.custom_sites[site_name]["engine_id"] = catalog.engine_id
            self.custom_sites[site_name]["engine_options"] = catalog.metadata.get("engine_options", {})
            self.catalog_configs[site_name] = self.custom_sites[site_name]
            if persist:
                save_json("custom_sites.json", self.custom_sites)

    def _on_engine_mode_change(self, mode):
        # Log mode change if not redundant
        if getattr(self, "last_logged_mode", None) != mode:
            if hasattr(self, "log_panel") and self.log_panel:
                self.log_panel.append_log(f"예약 방식이 '{mode}'(으)로 변경되었습니다.", "info")
            self.last_logged_mode = mode

        self._suppress_site_log = True

        if mode == NAVER_MODE:
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
        else:
            site_options = self.default_site_names + [k for k, v in self.custom_sites.items() if v.get("style") != "naver"]
            if getattr(self, "last_standard_site", None) in site_options:
                target_site = self.last_standard_site
            else:
                target_site = "제로월드"
                
            self.site_var.set(target_site)
            self._on_site_change(target_site)
            self.site_dropdown.configure(values=site_options)

        self._suppress_site_log = False

    def _sync_server_time(self, generation, site_name):
        import urllib.request
        import time
        from email.utils import parsedate_to_datetime
        
        while self.is_sync_running and generation == self._server_sync_generation:
            try:
                target_url = "https://www.keyescape.com/reservation.php" if site_name == "키이스케이프" else "https://booking.naver.com"
                req = urllib.request.Request(target_url, method="HEAD")
                start = time.perf_counter()
                with urllib.request.urlopen(req, timeout=3) as response:
                    latency = (time.perf_counter() - start) / 2
                    date_str = response.info().get("Date")
                    if date_str:
                        gmt_dt = parsedate_to_datetime(date_str)
                        server_time = gmt_dt.timestamp() + latency
                        offset = server_time - time.time()
                        self.engine_event_queue.put(
                            ("server_sync", generation, site_name, offset, latency, None)
                        )
            except Exception as e:
                self.engine_event_queue.put(
                    ("server_sync", generation, site_name, 0.0, 0.0, str(e))
                )

            # Re-sync every 30 seconds
            for _ in range(30):
                if not self.is_sync_running or generation != self._server_sync_generation:
                    break
                time.sleep(1)

    def _apply_server_sync_result(self, generation, site_name, offset, latency, error):
        if generation != self._server_sync_generation or not self.is_sync_running:
            return
        if error:
            if site_name == "키이스케이프":
                self.log_panel.append_log(f"[에러] 키이스케이프 서버 시간 동기화 실패: {error}", "error")
            return
        self.naver_time_offset = offset
        if site_name == "키이스케이프" and not getattr(self, "_keyescape_time_verified", False):
            self._keyescape_time_verified = True
            if abs(offset) > 5:
                self.log_panel.append_log(
                    f"[경고] 키이스케이프 서버 시간과 로컬 PC 시간의 차이가 큽니다 ({abs(offset):.2f}초).",
                    "warning",
                )
            else:
                self.log_panel.append_log(
                    f"[정보] 키이스케이프 서버 시간 동기화 완료 (응답 오차: {latency * 1000:.1f}ms)",
                    "success",
                )

    def _update_server_time_clock(self):
        if not self.is_sync_running:
            return
            
        if not self.server_time_label.winfo_ismapped():
            self.server_time_label.pack(anchor="center", pady=(5, 0))
        
        now = time.time() + self.naver_time_offset
        now_dt = datetime.fromtimestamp(now)
        current_site = self.site_var.get()
        prefix = "키이스케이프 서버 시간" if current_site == "키이스케이프" else "네이버 서버 시간"
        time_str = now_dt.strftime(f"{prefix}: %H:%M:%S.%f")[:-4] # keep milliseconds to 2 decimals
        self.server_time_label.configure(text=time_str)
        
        # Refresh every 100ms
        self.after(100, self._update_server_time_clock)

    def _on_close(self):
        self._ui_polling = False
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
                if not messagebox.askyesno(
                    "예약 정보 확인",
                    f"{res_data.summary()}\n\n이 정보로 예약 감시를 시작할까요?",
                    parent=self,
                ):
                    return
                self._start_booking(res_data, threads, is_async)
                
        animate_click(self.cta_btn, 38, callback=proceed)

    def _start_booking(self, reservation_data: ReservationRequest, threads, is_async):
        if self._catalog_refresh_running:
            messagebox.showinfo("예약 시작", "사이트 정보 갱신이 끝난 뒤 예약을 시작해주세요.", parent=self)
            return
        selected_site = self.site_var.get()
        self.form.save_config(selected_site)
        self.log_panel.clear_log()
        self.attempt_count = 0
        self.current_status = "running"
        self.booking_started_at = time.monotonic()
        self.log_panel.append_log(f"[{selected_site}] 예약을 시작합니다...", "info")
        self.form.set_running_state(True)
        self.site_dropdown.configure(state="disabled")
        self.add_site_btn.configure(state="disabled")
        self.delete_site_btn.configure(state="disabled")

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

        # Clear any stale engine events before starting a new run.
        while True:
            try:
                self.engine_event_queue.get_nowait()
            except queue.Empty:
                break

        payload = reservation_data.to_engine_payload()
        if self.form.engine_mode_btn.get() == NAVER_MODE:
            payload["naver_time_offset"] = getattr(self, "naver_time_offset", 0.0)
        try:
            self.active_engine = EngineRegistry.create(
                site_name=selected_site,
                mode=self.form.engine_mode_btn.get(),
                payload=payload,
                custom_sites=self.custom_sites,
                log_callback=self._on_engine_log,
                success_callback=self._on_booking_success,
                status_callback=self._on_engine_status_update,
                log_batch_callback=self._on_engine_log_batch,
            )
            self._engine_completion_handled = False
            self.active_engine.start_reservation(payload, threads, is_async)
            self._update_booking_status()
        except Exception as exc:
            self._engine_completion_handled = True
            self.active_engine = None
            self.current_status = "error"
            self.log_panel.append_log(f"예약 엔진 시작 실패: {exc}", "error")
            self._reset_cta_state()

    def _stop_booking(self):
        if self.active_engine and self.active_engine.is_running:
            self.current_status = "stopping"
            self.active_engine.stop_reservation()
            self.cta_btn.configure(text="중지 중...", state="disabled")
            self.status_badge.configure(text="● 중지 중", text_color=theme.TEXT_PRIMARY, fg_color=theme.CARD_COLOR)
        else:
            self.log_panel.append_log("실행 중인 예약 작업이 없습니다.", "warning")

    def _reset_cta_state(self):
        if self._status_timer_id is not None:
            try:
                self.after_cancel(self._status_timer_id)
            except Exception:
                pass
            self._status_timer_id = None
        self.cta_btn.configure(
            text="예약 시작",
            state="normal",
            text_color=theme.TEXT_DARK,
            fg_color=theme.ACCENT_WHITE,
            hover_color=theme.TEXT_BODY
        )
        self.form.set_running_state(False)
        if self._catalog_refresh_running:
            self.form.catalog_refresh_btn.configure(state="disabled")
        self.site_dropdown.configure(state="normal")
        self.add_site_btn.configure(state="normal")
        self._update_delete_button_state(self.site_var.get())
        self.booking_started_at = None
        
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
        self.engine_event_queue.put(("log", message, log_type))

    def _on_engine_log_batch(self, batch):
        self.engine_event_queue.put(("log_batch", list(batch)))

    def _on_engine_status_update(self, attempt_count, last_error):
        self.engine_event_queue.put(("status", attempt_count, last_error))

    def _update_booking_status(self):
        if self.current_status not in {"running", "stopping"}:
            self._status_timer_id = None
            return
        if self.booking_started_at and self.current_status == "running":
            elapsed = int(time.monotonic() - self.booking_started_at)
            self.status_badge.configure(text=f"● 동작 중 · {self.attempt_count:,}회 · {elapsed // 60:02d}:{elapsed % 60:02d}")
        self._status_timer_id = self.after(500, self._update_booking_status)

    def _drain_engine_events(self):
        logs = []
        success = False
        while True:
            try:
                event = self.engine_event_queue.get_nowait()
            except queue.Empty:
                break

            kind = event[0]
            if kind == "log":
                logs.append((event[1], event[2]))
            elif kind == "log_batch":
                logs.extend(event[1])
            elif kind == "status":
                self.attempt_count = event[1]
            elif kind == "success":
                success = True
            elif kind == "refresh_themes":
                self._refresh_themes_ui()
            elif kind == "catalog_result":
                self._handle_catalog_result(*event[1:])
            elif kind == "catalog_done":
                self._catalog_refresh_running = False
                if self.current_status not in {"running", "stopping"}:
                    self.form.catalog_refresh_btn.configure(state="normal")
            elif kind == "server_sync":
                self._apply_server_sync_result(*event[1:])

        if logs:
            self.log_panel.append_logs_batch(logs)
            if any(log_type == "error" for _, log_type in logs):
                self.current_status = "error"
                self.status_badge.configure(
                    text="● 에러 발생",
                    text_color=theme.TEXT_PRIMARY,
                    fg_color=theme.ACCENT_RED,
                )
        if success:
            self.current_status = "idle"
            self._trigger_success_notification()
        else:
            self._check_engine_finished()

        if self._ui_polling:
            self.after(30, self._drain_engine_events)

    def _check_engine_finished(self):
        engine = self.active_engine
        if not engine or engine.is_running:
            return
        if not self._engine_completion_handled:
            self._engine_completion_handled = True
            self._reset_cta_state()
        self.active_engine = None

    def _on_booking_success(self):
        self.engine_event_queue.put(("success",))

    def _start_catalog_auto_refresh(self):
        if not self.form.catalog_auto_refresh_var.get():
            return
        if self.current_status in {"running", "stopping"}:
            self.after(5000, self._start_catalog_auto_refresh)
            return
        stale_sites = [
            name
            for name, config in self.catalog_configs.items()
            if self.catalog_service.is_stale(config.get("catalog_key", ""))
        ]
        if stale_sites:
            self._start_catalog_refresh(stale_sites, manual=False)

    def _refresh_current_catalog(self):
        site_name = self.site_var.get()
        if self.current_status in {"running", "stopping"}:
            messagebox.showinfo("사이트 정보 갱신", "예약 실행 중에는 사이트 정보를 갱신할 수 없습니다.", parent=self)
            return
        if site_name not in self.catalog_configs:
            messagebox.showwarning("사이트 정보 갱신", "갱신할 수 있는 사이트가 아닙니다.", parent=self)
            return
        self._start_catalog_refresh([site_name], manual=True)

    def _start_catalog_refresh(self, site_names, manual=False):
        if self._catalog_refresh_running:
            if manual:
                messagebox.showinfo("사이트 정보 갱신", "이미 사이트 정보를 갱신하고 있습니다.", parent=self)
            return
        self._catalog_refresh_running = True
        self.form.set_catalog_refresh_state("갱신 중...", busy=True)
        target_date = self._catalog_target_date()

        def worker():
            for site_name in site_names:
                config = self.catalog_configs.get(site_name)
                if not config:
                    continue
                result = self.catalog_service.refresh(config, target_date, force=manual)
                self.engine_event_queue.put(("catalog_result", site_name, result, manual))
            self.engine_event_queue.put(("catalog_done", manual))

        threading.Thread(target=worker, name="CatalogRefreshThread", daemon=True).start()

    def _catalog_target_date(self):
        today = datetime.now().date()
        raw_value = self.form.date_entry.get().strip()
        try:
            selected = datetime.strptime(raw_value, "%Y-%m-%d").date()
        except ValueError:
            selected = today
        return max(today, selected).isoformat()

    def _handle_catalog_result(self, site_name, result, manual):
        catalog = self.catalog_service.catalogs.get(result.site_key)
        selection = None
        if site_name == self.site_var.get():
            selection = (
                self.form._selected_branch_id(),
                self.form._selected_theme_id(),
            )
        if catalog and result.status in {"changed", "unchanged"}:
            self._apply_catalog_to_runtime(site_name, catalog)
            self._refresh_current_catalog_ui(site_name, selection)

        pending_count = len(self.catalog_service.pending_changes(result.site_key))
        if result.status in {"changed", "unchanged"}:
            self._catalog_applied_counts[site_name] = len(result.applied_changes)
        changed_count = self._catalog_applied_counts.get(site_name, 0)
        if site_name == self.site_var.get():
            if result.status in {"changed", "unchanged"}:
                stamp = (catalog.last_success_at if catalog else result.checked_at).replace("T", " ")[:16]
                self.form.set_catalog_refresh_state(
                    f"최근 {stamp}",
                    pending_count=pending_count,
                    changed_count=changed_count,
                )
            elif result.status == "deferred":
                self.form.set_catalog_refresh_state(
                    "갱신 보류 · 기존 정보 사용",
                    pending_count=pending_count,
                    changed_count=self._catalog_applied_counts.get(site_name, 0),
                )
            else:
                self.form.set_catalog_refresh_state(
                    "갱신 실패",
                    pending_count=pending_count,
                    changed_count=self._catalog_applied_counts.get(site_name, 0),
                )

        if result.error:
            if result.status == "deferred":
                refresh_kind = "수동" if manual else "자동"
                self.log_panel.append_log(
                    f"[{site_name}] {refresh_kind} 갱신 보류: {result.error}", "warning"
                )
            else:
                self.log_panel.append_log(f"[{site_name}] 사이트 정보 갱신 실패: {result.error}", "warning")
            if manual:
                messagebox.showwarning("사이트 정보 갱신", result.error, parent=self)
            return

        added = sum(change.kind == "added" for change in result.applied_changes)
        renamed = sum(change.kind == "renamed" for change in result.applied_changes)
        if result.changed:
            self.log_panel.append_log(
                f"[{site_name}] 카탈로그 갱신 · 신규 {added} · 이름 변경 {renamed} · 확인 필요 {pending_count}",
                "success" if not pending_count else "warning",
            )
        elif manual:
            self.log_panel.append_log(f"[{site_name}] 사이트 정보가 이미 최신입니다.", "info")

        if manual:
            if pending_count:
                self._show_catalog_pending(site_name)
            else:
                messagebox.showinfo(
                    "사이트 정보 갱신",
                    f"{site_name} 갱신 완료\n\n신규: {added}개\n이름 변경: {renamed}개\n확인 필요: 0개",
                    parent=self,
                )

    def _refresh_current_catalog_ui(self, site_name, selection=None):
        if site_name != self.site_var.get():
            return
        if selection is None:
            selection = (
                self.form._selected_branch_id(),
                self.form._selected_theme_id(),
            )
        old_branch_id, old_theme_id = selection
        self.form.custom_sites = self.custom_sites
        self.form.set_site(site_name)
        branch_name = next(
            (
                name
                for name, branch_id in self.form.config.get("branch_ids", {}).items()
                if str(branch_id) == str(old_branch_id)
            ),
            next(
                (
                    name
                    for name, branch_id in self.form.config.get("branches", {}).items()
                    if str(branch_id) == str(old_branch_id)
                ),
                "",
            ),
        )
        if branch_name:
            self.form.branch_var.set(branch_name)
            self.form._update_theme_options()
        if old_theme_id:
            booking_branch_id = self.form.config.get("branches", {}).get(branch_name, "")
            theme_name = next(
                (
                    name
                    for name in self.form.theme_dropdown.cget("values")
                    if self.form._theme_id_for_name(booking_branch_id, name) == str(old_theme_id)
                ),
                "",
            )
            if theme_name:
                self.form.theme_var.set(theme_name)

    def _show_catalog_pending(self, site_name=None):
        site_name = site_name or self.site_var.get()
        config = self.catalog_configs.get(site_name)
        if not config:
            return
        site_key = config.get("catalog_key", "")
        changes = self.catalog_service.pending_changes(site_key)
        if not changes:
            changed_count = self._catalog_applied_counts.get(site_name, 0)
            if changed_count:
                messagebox.showinfo(
                    "사이트 변경 내역",
                    f"안전 규칙에 따라 자동 반영된 변경이 {changed_count}개 있습니다.\n"
                    "자세한 내용은 터미널 로그에서 확인할 수 있습니다.",
                    parent=self,
                )
                self._catalog_applied_counts[site_name] = 0
                self._update_catalog_status(site_name)
            else:
                messagebox.showinfo("사이트 변경 검토", "확인할 변경사항이 없습니다.", parent=self)
            return

        def apply_selected(selected):
            selection = None
            if site_name == self.site_var.get():
                selection = (
                    self.form._selected_branch_id(),
                    self.form._selected_theme_id(),
                )
            catalog = self.catalog_service.apply_pending(site_key, selected)
            if catalog:
                self._apply_catalog_to_runtime(site_name, catalog)
                self._refresh_current_catalog_ui(site_name, selection)
                remaining = len(self.catalog_service.pending_changes(site_key))
                if site_name == self.site_var.get():
                    self.form.set_catalog_refresh_state("검토 반영 완료", pending_count=remaining)
                self.log_panel.append_log(
                    f"[{site_name}] 검토한 사이트 변경 {len(selected)}개를 반영했습니다.", "success"
                )

        CatalogChangesDialog(self, site_name, changes, apply_selected)

    def _trigger_success_notification(self):
        self._engine_completion_handled = True
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
                        
                        self.engine_event_queue.put(("refresh_themes",))
            except Exception:
                pass

        import threading
        t = threading.Thread(target=fetch, name="ThemeFetcher")
        t.daemon = True
        t.start()

    def _start_zeroworld_theme_fetcher(self):
        reservation_date = self.form.date_entry.get().strip()

        def fetch():
            from data.themes import ZEROWORLD_THEMES
            from engines.zeroworld_catalog import fetch_themes

            changed = False
            for branch_id in ("1", "2", "4", "5"):
                try:
                    themes = fetch_themes(branch_id, reservation_date)
                except Exception:
                    continue
                if themes and themes != ZEROWORLD_THEMES.get(branch_id):
                    ZEROWORLD_THEMES[branch_id] = themes
                    changed = True
            if changed:
                self.engine_event_queue.put(("refresh_themes",))

        import threading

        thread = threading.Thread(target=fetch, name="ZeroWorldThemeFetcher", daemon=True)
        thread.start()

    def _refresh_themes_ui(self):
        try:
            if self.site_var.get() in {"지구별방탈출", "제로월드"}:
                self.form._update_theme_options()
        except Exception:
            pass
