#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
네이버페이 가상 보안 키패드 인식 및 클릭 시뮬레이터 (NPay Keypad Tester)
- 펭크로와 동일한 다크 테마 UI
- 클립보드(Ctrl+V) 또는 파일에서 네이버페이 키패드 이미지 로드
- Lucide Eye(눈 모양) ON/OFF 비밀번호 표시/숨김 토글
- 키패드 0~9 번호 자동 인식 및 6자리 클릭 순서 시각적 오버레이 시뮬레이션
"""

from __future__ import annotations

import base64
import io
import os
import sys
import threading
import time
from pathlib import Path
from tkinter import Canvas, filedialog, messagebox
from typing import Any

# Ensure project root is in sys.path
_PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

import customtkinter as ctk
from PIL import Image, ImageDraw, ImageFont, ImageGrab, ImageTk

from engines.npay_keypad_recognizer import KeypadCell, NpayKeypadRecognizer

# Sample base64 image (user's real Naver Pay keypad screenshot)
_SAMPLE_FILE = _PROJECT_ROOT / "data" / "npay_sample.b64"


def _create_lucide_eye_icon(size: tuple[int, int] = (18, 18), color: str = "#9ca3af") -> ctk.CTkImage:
    scale = 8
    canvas_dim = 24
    canvas_size = canvas_dim * scale
    img = Image.new("RGBA", (canvas_size, canvas_size), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)
    sw = int(1.8 * scale)

    # Eye contours (24x24 viewBox)
    draw.arc([2 * scale, 4 * scale, 22 * scale, 20 * scale], start=200, end=340, fill=color, width=sw)
    draw.arc([2 * scale, 4 * scale, 22 * scale, 20 * scale], start=20, end=160, fill=color, width=sw)

    # Pupil
    r = int(3.3 * scale)
    cx, cy = 12 * scale, 12 * scale
    draw.ellipse([cx - r, cy - r, cx + r, cy + r], outline=color, width=sw)

    res = img.resize(size, Image.Resampling.LANCZOS)
    return ctk.CTkImage(light_image=res, dark_image=res, size=size)


def _create_lucide_eye_off_icon(size: tuple[int, int] = (18, 18), color: str = "#9ca3af") -> ctk.CTkImage:
    scale = 8
    canvas_dim = 24
    canvas_size = canvas_dim * scale
    img = Image.new("RGBA", (canvas_size, canvas_size), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)
    sw = int(1.8 * scale)

    # Lower eye curve
    draw.arc([2 * scale, 4 * scale, 22 * scale, 20 * scale], start=20, end=150, fill=color, width=sw)
    # Upper eye arcs
    draw.arc([2 * scale, 4 * scale, 22 * scale, 20 * scale], start=200, end=240, fill=color, width=sw)
    draw.arc([2 * scale, 4 * scale, 22 * scale, 20 * scale], start=290, end=340, fill=color, width=sw)
    # Pupil arc
    r = int(3.3 * scale)
    cx, cy = 12 * scale, 12 * scale
    draw.arc([cx - r, cy - r, cx + r, cy + r], start=60, end=210, fill=color, width=sw)
    # Diagonal slash
    draw.line([3 * scale, 3 * scale, 21 * scale, 21 * scale], fill=color, width=sw)

    res = img.resize(size, Image.Resampling.LANCZOS)
    return ctk.CTkImage(light_image=res, dark_image=res, size=size)


class NpayKeypadTesterApp(ctk.CTk):
    """Standalone interactive verification tool for Naver Pay keypad recognition and simulated clicking."""

    THEME_BG = "#121316"
    THEME_CARD = "#1c1e22"
    THEME_ELEVATED = "#252830"
    THEME_ACCENT_GREEN = "#03c75a"
    THEME_ACCENT_BLUE = "#3875f6"
    THEME_TEXT_PRIMARY = "#ffffff"
    THEME_TEXT_SECONDARY = "#9ca3af"
    THEME_TEXT_MUTED = "#6b7280"
    THEME_BORDER = "#2e323b"

    def __init__(self) -> None:
        super().__init__()
        ctk.set_appearance_mode("dark")
        ctk.set_default_color_theme("blue")

        self.title("네이버페이 키패드 인식 & 클릭 시뮬레이터 (NPay Keypad Tester)")
        self.geometry("1100x820")
        self.minsize(980, 720)
        self.configure(fg_color=self.THEME_BG)

        self.current_image: Image.Image | None = None
        self.recognized_cells: dict[str, KeypadCell] = {}
        self.is_simulating = False
        self.simulation_stop = threading.Event()
        self.eye_visible = False

        self.icon_eye = _create_lucide_eye_icon((18, 18), color="#9ca3af")
        self.icon_eye_off = _create_lucide_eye_off_icon((18, 18), color="#9ca3af")

        self._build_ui()
        self._load_default_sample()

        # Keyboard shortcuts
        self.bind("<Control-v>", lambda e: self._paste_from_clipboard())
        self.bind("<Control-V>", lambda e: self._paste_from_clipboard())

    def _build_ui(self) -> None:
        # Header Bar
        header = ctk.CTkFrame(self, fg_color=self.THEME_CARD, height=54, corner_radius=0)
        header.pack(fill="x", side="top")
        header.pack_propagate(False)

        title_box = ctk.CTkFrame(header, fg_color="transparent")
        title_box.pack(side="left", padx=16, pady=8)

        ctk.CTkLabel(
            title_box,
            text="네이버페이 키패드 시뮬레이터",
            font=ctk.CTkFont(family="Pretendard, Malgun Gothic", size=16, weight="bold"),
            text_color=self.THEME_TEXT_PRIMARY,
        ).pack(side="left", padx=(0, 10))

        badge = ctk.CTkLabel(
            title_box,
            text="v1.0 독립 검증 도구",
            font=ctk.CTkFont(family="Pretendard, Malgun Gothic", size=11),
            text_color=self.THEME_ACCENT_GREEN,
            fg_color="#0d2b1d",
            corner_radius=6,
            padx=8,
            pady=2,
        )
        badge.pack(side="left")

        # Main Body (Split into Left Controls / Right Visualizer)
        body = ctk.CTkFrame(self, fg_color="transparent")
        body.pack(fill="both", expand=True, padx=16, pady=16)
        body.columnconfigure(0, weight=0, minsize=380)
        body.columnconfigure(1, weight=1)
        body.rowconfigure(0, weight=1)

        # Left Control Panel
        left_panel = ctk.CTkScrollableFrame(
            body,
            fg_color=self.THEME_CARD,
            border_color=self.THEME_BORDER,
            border_width=1,
            corner_radius=10,
            width=380,
        )
        left_panel.grid(row=0, column=0, sticky="nsew", padx=(0, 12))

        # Card 1: 비밀번호 설정
        self._build_card_password(left_panel)

        # Card 2: 이미지 불러오기
        self._build_card_image_source(left_panel)

        # Card 3: 시뮬레이션 제어
        self._build_card_simulation(left_panel)

        # Card 4: 실시간 인식 & 시뮬레이션 로그
        self._build_card_logs(left_panel)

        # Right Panel: Canvas Visualizer
        right_panel = ctk.CTkFrame(
            body,
            fg_color=self.THEME_CARD,
            border_color=self.THEME_BORDER,
            border_width=1,
            corner_radius=10,
        )
        right_panel.grid(row=0, column=1, sticky="nsew")
        self._build_canvas_panel(right_panel)

    def _build_card_password(self, parent: ctk.CTkFrame) -> None:
        card = ctk.CTkFrame(parent, fg_color=self.THEME_ELEVATED, corner_radius=8)
        card.pack(fill="x", pady=(0, 12))

        ctk.CTkLabel(
            card,
            text="🔐 네이버페이 결제 비밀번호 (6자리)",
            font=ctk.CTkFont(family="Pretendard, Malgun Gothic", size=13, weight="bold"),
            text_color=self.THEME_TEXT_PRIMARY,
        ).pack(anchor="w", padx=12, pady=(10, 6))

        input_row = ctk.CTkFrame(card, fg_color="transparent")
        input_row.pack(fill="x", padx=12, pady=(0, 10))
        input_row.columnconfigure(0, weight=1)

        self.password_var = ctk.StringVar(value="123456")
        self.password_entry = ctk.CTkEntry(
            input_row,
            textvariable=self.password_var,
            placeholder_text="6자리 숫자 입력",
            show="•",
            font=ctk.CTkFont(family="Pretendard, Consolas", size=14),
            height=36,
            fg_color=self.THEME_CARD,
            border_color=self.THEME_BORDER,
            text_color=self.THEME_TEXT_PRIMARY,
        )
        self.password_entry.grid(row=0, column=0, sticky="ew", padx=(0, 6))

        self.eye_button = ctk.CTkButton(
            input_row,
            image=self.icon_eye,
            text="",
            width=36,
            height=36,
            fg_color=self.THEME_CARD,
            hover_color="#333842",
            border_color=self.THEME_BORDER,
            border_width=1,
            command=self._toggle_eye,
        )
        self.eye_button.grid(row=0, column=1, sticky="e")

    def _build_card_image_source(self, parent: ctk.CTkFrame) -> None:
        card = ctk.CTkFrame(parent, fg_color=self.THEME_ELEVATED, corner_radius=8)
        card.pack(fill="x", pady=(0, 12))

        ctk.CTkLabel(
            card,
            text="🖼️ 키패드 이미지 입력",
            font=ctk.CTkFont(family="Pretendard, Malgun Gothic", size=13, weight="bold"),
            text_color=self.THEME_TEXT_PRIMARY,
        ).pack(anchor="w", padx=12, pady=(10, 6))

        btn_row = ctk.CTkFrame(card, fg_color="transparent")
        btn_row.pack(fill="x", padx=12, pady=(0, 6))
        btn_row.columnconfigure((0, 1), weight=1)

        ctk.CTkButton(
            btn_row,
            text="📋 클립보드 붙여넣기",
            font=ctk.CTkFont(size=12),
            height=32,
            fg_color="#1e3a8a",
            hover_color="#2563eb",
            command=self._paste_from_clipboard,
        ).grid(row=0, column=0, sticky="ew", padx=(0, 4))

        ctk.CTkButton(
            btn_row,
            text="📁 파일 불러오기",
            font=ctk.CTkFont(size=12),
            height=32,
            fg_color=self.THEME_CARD,
            hover_color="#333842",
            border_color=self.THEME_BORDER,
            border_width=1,
            command=self._open_file,
        ).grid(row=0, column=1, sticky="ew", padx=(4, 0))

        ctk.CTkButton(
            card,
            text="⚡ 기본 샘플 이미지 다시 로드",
            font=ctk.CTkFont(size=11),
            height=28,
            fg_color="transparent",
            text_color=self.THEME_TEXT_SECONDARY,
            hover_color=self.THEME_CARD,
            command=self._load_default_sample,
        ).pack(fill="x", padx=12, pady=(0, 8))

    def _build_card_simulation(self, parent: ctk.CTkFrame) -> None:
        card = ctk.CTkFrame(parent, fg_color=self.THEME_ELEVATED, corner_radius=8)
        card.pack(fill="x", pady=(0, 12))

        ctk.CTkLabel(
            card,
            text="⚡ 시뮬레이션 제어",
            font=ctk.CTkFont(family="Pretendard, Malgun Gothic", size=13, weight="bold"),
            text_color=self.THEME_TEXT_PRIMARY,
        ).pack(anchor="w", padx=12, pady=(10, 6))

        self.start_btn = ctk.CTkButton(
            card,
            text="▶️ 분석 및 클릭 시뮬레이션 시작",
            font=ctk.CTkFont(family="Pretendard, Malgun Gothic", size=13, weight="bold"),
            height=38,
            fg_color=self.THEME_ACCENT_GREEN,
            hover_color="#02a348",
            text_color="#000000",
            command=self._start_simulation,
        )
        self.start_btn.pack(fill="x", padx=12, pady=(0, 8))

        speed_row = ctk.CTkFrame(card, fg_color="transparent")
        speed_row.pack(fill="x", padx=12, pady=(0, 10))
        speed_row.columnconfigure(1, weight=1)

        ctk.CTkLabel(
            speed_row,
            text="클릭 딜레이:",
            font=ctk.CTkFont(size=11),
            text_color=self.THEME_TEXT_SECONDARY,
        ).grid(row=0, column=0, sticky="w", padx=(0, 8))

        self.speed_slider = ctk.CTkSlider(
            speed_row,
            from_=100,
            to=1000,
            number_of_steps=18,
            height=14,
            command=self._on_speed_change,
        )
        self.speed_slider.set(400)
        self.speed_slider.grid(row=0, column=1, sticky="ew")

        self.speed_label = ctk.CTkLabel(
            speed_row,
            text="400ms",
            font=ctk.CTkFont(size=11),
            text_color=self.THEME_TEXT_MUTED,
            width=45,
        )
        self.speed_label.grid(row=0, column=2, sticky="e", padx=(6, 0))

    def _build_card_logs(self, parent: ctk.CTkFrame) -> None:
        card = ctk.CTkFrame(parent, fg_color=self.THEME_ELEVATED, corner_radius=8)
        card.pack(fill="both", expand=True, pady=(0, 0))

        header_row = ctk.CTkFrame(card, fg_color="transparent")
        header_row.pack(fill="x", padx=12, pady=(8, 4))
        header_row.columnconfigure(0, weight=1)

        ctk.CTkLabel(
            header_row,
            text="📋 실시간 분석 및 시뮬레이션 로그",
            font=ctk.CTkFont(family="Pretendard, Malgun Gothic", size=12, weight="bold"),
            text_color=self.THEME_TEXT_PRIMARY,
        ).grid(row=0, column=0, sticky="w")

        ctk.CTkButton(
            header_row,
            text="지우기",
            width=45,
            height=20,
            font=ctk.CTkFont(size=10),
            fg_color="transparent",
            text_color=self.THEME_TEXT_MUTED,
            hover_color=self.THEME_CARD,
            command=self._clear_logs,
        ).grid(row=0, column=1, sticky="e")

        self.log_box = ctk.CTkTextbox(
            card,
            fg_color=self.THEME_CARD,
            border_color=self.THEME_BORDER,
            border_width=1,
            font=ctk.CTkFont(family="Consolas, Malgun Gothic", size=11),
            text_color="#e5e7eb",
            height=200,
        )
        self.log_box.pack(fill="both", expand=True, padx=12, pady=(0, 10))

    def _build_canvas_panel(self, parent: ctk.CTkFrame) -> None:
        parent.rowconfigure(1, weight=1)
        parent.columnconfigure(0, weight=1)

        # Top Bar with PIN dots indicator & status
        top_bar = ctk.CTkFrame(parent, fg_color=self.THEME_ELEVATED, height=48, corner_radius=8)
        top_bar.grid(row=0, column=0, sticky="ew", padx=12, pady=12)
        top_bar.pack_propagate(False)

        self.status_label = ctk.CTkLabel(
            top_bar,
            text="키패드 이미지를 불러오거나 시뮬레이션을 시작하세요.",
            font=ctk.CTkFont(family="Pretendard, Malgun Gothic", size=12),
            text_color=self.THEME_TEXT_SECONDARY,
        )
        self.status_label.pack(side="left", padx=16)

        # PIN Dots (6 circles)
        self.dots_frame = ctk.CTkFrame(top_bar, fg_color="transparent")
        self.dots_frame.pack(side="right", padx=16)

        self.dot_labels: list[ctk.CTkLabel] = []
        for i in range(6):
            dot = ctk.CTkLabel(
                self.dots_frame,
                text="○",
                font=ctk.CTkFont(size=18, weight="bold"),
                text_color=self.THEME_TEXT_MUTED,
                width=22,
            )
            dot.pack(side="left", padx=2)
            self.dot_labels.append(dot)

        # Canvas Area
        canvas_box = ctk.CTkFrame(parent, fg_color="#0a0b0d", corner_radius=8)
        canvas_box.grid(row=1, column=0, sticky="nsew", padx=12, pady=(0, 12))
        canvas_box.rowconfigure(0, weight=1)
        canvas_box.columnconfigure(0, weight=1)

        self.canvas = Canvas(
            canvas_box,
            bg="#0a0b0d",
            highlightthickness=0,
            bd=0,
        )
        self.canvas.grid(row=0, column=0, sticky="nsew")

        # Canvas Click to copy coords
        self.canvas.bind("<Button-1>", self._on_canvas_click)

    def _toggle_eye(self) -> None:
        self.eye_visible = not self.eye_visible
        if self.eye_visible:
            self.password_entry.configure(show="")
            self.eye_button.configure(image=self.icon_eye_off)
        else:
            self.password_entry.configure(show="•")
            self.eye_button.configure(image=self.icon_eye)

    def _on_speed_change(self, value: float) -> None:
        ms = int(value)
        self.speed_label.configure(text=f"{ms}ms")

    def _log(self, text: str, level: str = "info") -> None:
        ts = time.strftime("%H:%M:%S")
        prefix = {
            "info": "ℹ️ ",
            "success": "✅ ",
            "warning": "⚠️ ",
            "error": "❌ ",
            "click": "👉 ",
        }.get(level, "• ")
        self.log_box.insert("end", f"[{ts}] {prefix}{text}\n")
        self.log_box.see("end")

    def _clear_logs(self) -> None:
        self.log_box.delete("1.0", "end")

    def _update_dots(self, filled_count: int) -> None:
        for i, dot in enumerate(self.dot_labels):
            if i < filled_count:
                dot.configure(text="●", text_color=self.THEME_ACCENT_GREEN)
            else:
                dot.configure(text="○", text_color=self.THEME_TEXT_MUTED)

    def _load_default_sample(self) -> None:
        sample_path = _PROJECT_ROOT / "scratch" / "sample_b64.txt"
        if sample_path.exists():
            try:
                b64_str = sample_path.read_text(encoding="utf-8").strip()
                raw_bytes = base64.b64decode(b64_str)
                img = Image.open(io.BytesIO(raw_bytes))
                self._set_image(img, "기본 샘플 (네이버페이 가상 키패드)")
                return
            except Exception as exc:
                self._log(f"샘플 로드 실패: {exc}", "error")

        # Fallback to direct user uploaded image if available
        user_media = Path(r"C:\Users\Administrator\.gemini\antigravity\brain\6f859b83-e5c9-4d39-9883-805d23fcb21b\.user_uploaded\media_1787031981896.png")
        if user_media.exists():
            try:
                img = Image.open(user_media)
                self._set_image(img, "사용자 업로드 네이버페이 화면")
                return
            except Exception:
                pass

        self._log("기본 샘플 이미지를 찾지 못했습니다. 클립보드(Ctrl+V)나 파일 열기를 이용해주세요.", "warning")

    def _paste_from_clipboard(self) -> None:
        try:
            grabbed = ImageGrab.grabclipboard()
            if isinstance(grabbed, Image.Image):
                self._set_image(grabbed, "클립보드에서 붙여넣은 이미지")
            elif isinstance(grabbed, list) and grabbed:
                # File path in clipboard
                file_path = grabbed[0]
                img = Image.open(file_path)
                self._set_image(img, f"클립보드 파일: {Path(file_path).name}")
            else:
                messagebox.showinfo("알림", "클립보드에 복사된 이미지가 없습니다.\nWin+Shift+S 로 화면을 캡처한 후 다시 시도해보세요.")
        except Exception as exc:
            self._log(f"클립보드 읽기 오류: {exc}", "error")

    def _open_file(self) -> None:
        path = filedialog.askopenfilename(
            title="키패드 이미지 선택",
            filetypes=[("Image files", "*.png;*.jpg;*.jpeg;*.webp;*.bmp"), ("All files", "*.*")],
        )
        if path:
            try:
                img = Image.open(path)
                self._set_image(img, f"파일: {Path(path).name}")
            except Exception as exc:
                self._log(f"파일 열기 실패: {exc}", "error")

    def _set_image(self, img: Image.Image, source_name: str) -> None:
        self.current_image = img.convert("RGBA")
        self._log(f"이미지 로드 완료 · {source_name} ({img.width}x{img.height})", "success")
        self.status_label.configure(text=f"{source_name} 로드됨 ({img.width}x{img.height})")
        self._update_dots(0)
        self._analyze_image()

    def _analyze_image(self) -> None:
        if self.current_image is None:
            return

        self._log("키패드 영역 및 숫자(0~9) 분석 시작...", "info")
        t0 = time.perf_counter()
        try:
            cells = NpayKeypadRecognizer.recognize_keypad_image(self.current_image)
            elapsed_ms = (time.perf_counter() - t0) * 1000.0
            self.recognized_cells = cells

            digits_found = [k for k in sorted(cells.keys()) if k.isdigit()]
            self._log(
                f"분석 완료! {len(digits_found)}개 숫자 및 기능키 감지 ({elapsed_ms:.1f}ms)",
                "success",
            )

            for d in digits_found:
                cell = cells[d]
                self._log(f"  • 숫자 '{d}' -> [{cell.row}행 {cell.col}열] (중심={cell.center}, 신뢰도={cell.confidence*100:.1f}%)")

            self._draw_overlay()
        except Exception as exc:
            self._log(f"키패드 분석 중 오류 발생: {exc}", "error")

    def _draw_overlay(self, active_step: int | None = None, active_digit: str | None = None) -> None:
        if self.current_image is None:
            return

        canvas_w = self.canvas.winfo_width()
        canvas_h = self.canvas.winfo_height()

        if canvas_w < 50 or canvas_h < 50:
            self.after(50, lambda: self._draw_overlay(active_step, active_digit))
            return

        # Scale image to fit canvas while preserving aspect ratio
        img_w, img_h = self.current_image.size
        scale = min((canvas_w - 40) / float(img_w), (canvas_h - 40) / float(img_h), 1.0)
        scale = max(0.2, scale)

        render_w = int(img_w * scale)
        render_h = int(img_h * scale)

        # Create overlay image
        overlay = self.current_image.copy().resize((render_w, render_h), Image.Resampling.BILINEAR)
        draw = ImageDraw.Draw(overlay)

        # Draw bounding boxes for recognized cells
        pw = self.password_var.get().strip()
        step_order_for_digit: dict[str, list[int]] = {}
        for step_idx, char in enumerate(pw, start=1):
            step_order_for_digit.setdefault(char, []).append(step_idx)

        for key, cell in self.recognized_cells.items():
            x1 = int(cell.bbox[0] * scale)
            y1 = int(cell.bbox[1] * scale)
            x2 = int(cell.bbox[2] * scale)
            y2 = int(cell.bbox[3] * scale)

            is_active = (active_digit is not None and key == active_digit)

            # Border color
            if is_active:
                border_color = "#facc15"  # Vibrant Yellow glow
                fill_color = (250, 204, 21, 60)
                draw.rectangle([x1, y1, x2, y2], outline=border_color, width=4)
                draw.rectangle([x1, y1, x2, y2], fill=fill_color)
            elif cell.digit is not None:
                border_color = "#3b82f6"  # Blue
                draw.rectangle([x1, y1, x2, y2], outline=border_color, width=2)
            else:
                border_color = "#6b7280"
                draw.rectangle([x1, y1, x2, y2], outline=border_color, width=1)

            # Draw step badge if this key is part of password
            if cell.digit in step_order_for_digit:
                steps = step_order_for_digit[cell.digit]
                badge_text = " ".join(f"[{s}]" for s in steps)
                bx = x1 + 6
                by = y1 + 6
                draw.rectangle([bx - 2, by - 2, bx + len(badge_text) * 7 + 4, by + 16], fill="#1e1b4b")
                draw.text((bx, by), badge_text, fill="#38bdf8")

        # Center on canvas
        self.canvas.delete("all")
        self._tk_img = ImageTk.PhotoImage(overlay)
        cx = canvas_w // 2
        cy = canvas_h // 2
        self.canvas.create_image(cx, cy, image=self._tk_img)

        # Store transform metadata for click translation
        self._canvas_meta = {
            "scale": scale,
            "offset_x": cx - render_w // 2,
            "offset_y": cy - render_h // 2,
        }

    def _on_canvas_click(self, event) -> None:
        if not hasattr(self, "_canvas_meta") or self.current_image is None:
            return
        meta = self._canvas_meta
        img_x = int((event.x - meta["offset_x"]) / meta["scale"])
        img_y = int((event.y - meta["offset_y"]) / meta["scale"])

        # Check if hit any cell
        for key, cell in self.recognized_cells.items():
            x1, y1, x2, y2 = cell.bbox
            if x1 <= img_x <= x2 and y1 <= img_y <= y2:
                self._log(f"🎯 캔버스 클릭: [{key}] 버튼 (좌표: x={img_x}, y={img_y}, 행={cell.row}, 열={cell.col})", "click")
                self._draw_overlay(active_digit=key)
                return

        self._log(f"캔버스 클릭 좌표: (x={img_x}, y={img_y})", "info")

    def _start_simulation(self) -> None:
        if self.is_simulating:
            self.simulation_stop.set()
            self.is_simulating = False
            self.start_btn.configure(text="▶️ 분석 및 클릭 시뮬레이션 시작", fg_color=self.THEME_ACCENT_GREEN)
            self._log("시뮬레이션이 사용자에 의해 중지되었습니다.", "warning")
            return

        pw = self.password_var.get().strip()
        if not pw:
            messagebox.showwarning("알림", "비밀번호를 입력해주세요.")
            return

        if len(pw) != 6 or not pw.isdigit():
            messagebox.showwarning("알림", "네이버페이 결제 비밀번호는 6자리 숫자여야 합니다.")
            return

        if not self.recognized_cells:
            messagebox.showwarning("알림", "키패드 이미지를 먼저 분석해주세요.")
            return

        # Check missing digits
        missing = [d for d in pw if d not in self.recognized_cells]
        if missing:
            messagebox.showerror(
                "인식 오류",
                f"비밀번호의 다음 숫자가 키패드에서 인식되지 않았습니다: {missing}\n이미지 해상도 또는 캡처 상태를 확인해주세요.",
            )
            return

        self.is_simulating = True
        self.simulation_stop.clear()
        self.start_btn.configure(text="⏹️ 시뮬레이션 중지", fg_color="#ef4444")
        self._update_dots(0)

        # Run sequential playback in thread
        delay_ms = int(self.speed_slider.get())
        threading.Thread(target=self._run_simulation_thread, args=(pw, delay_ms), daemon=True).start()

    def _run_simulation_thread(self, password: str, delay_ms: int) -> None:
        self._log(f"🚀 비밀번호 6자리 클릭 시뮬레이션 시작 (딜레이: {delay_ms}ms)...", "info")

        for idx, digit in enumerate(password, start=1):
            if self.simulation_stop.is_set():
                break

            cell = self.recognized_cells[digit]
            self.after(0, lambda s=idx, d=digit, c=cell: self._step_simulation_ui(s, d, c))
            time.sleep(delay_ms / 1000.0)

        if not self.simulation_stop.is_set():
            self.after(0, self._finish_simulation_ui)

    def _step_simulation_ui(self, step: int, digit: str, cell: KeypadCell) -> None:
        self._update_dots(step)
        self.status_label.configure(
            text=f"[{step}/6단계] 숫자 '{digit}' 클릭 중... (위치: {cell.row}행 {cell.col}열)",
            text_color=self.THEME_ACCENT_GREEN,
        )
        self._log(
            f"[{step}/6단계] 숫자 '{digit}' -> ({cell.row}행 {cell.col}열, 중심좌표: x={cell.center[0]}, y={cell.center[1]}) 클릭 시뮬레이션 완료",
            "click",
        )
        self._draw_overlay(active_step=step, active_digit=digit)

    def _finish_simulation_ui(self) -> None:
        self.is_simulating = False
        self.start_btn.configure(text="▶️ 분석 및 클릭 시뮬레이션 시작", fg_color=self.THEME_ACCENT_GREEN)
        self.status_label.configure(
            text="🎉 6자리 비밀번호 입력 시뮬레이션 성공! (결제 승인 상태)",
            text_color=self.THEME_ACCENT_GREEN,
        )
        self._log("🎉 [완료] 네이버페이 6자리 비밀번호 순차 클릭 시뮬레이션 성공!", "success")
        self._log("  * 모든 숫자가 정확한 순서와 위치로 매핑 및 클릭되었습니다.", "success")
        self._draw_overlay()


def main() -> None:
    app = NpayKeypadTesterApp()
    app.mainloop()


if __name__ == "__main__":
    main()
