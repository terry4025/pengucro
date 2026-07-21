import customtkinter as ctk
import ui.theme as theme
from data.themes import (
    ZEROWORLD_THEMES,
    JIGUBYEOL_THEMES, PHOBIADUNGEON_THEMES, SITES_CONFIG, JIGUBYEOL_THEME_ALIASES,
    KEYESCAPE_THEMES, DOOMESCAPE_THEMES
)
from pengucro.models import LEGACY_MODE_MAP, NAVER_MODE, STANDARD_MODE, ReservationRequest
from pengucro.storage import SecretStore, load_json, save_json
from datetime import datetime, timedelta
import calendar


class DatePickerDialog(ctk.CTkToplevel):
    def __init__(self, parent, initial_date, on_select):
        super().__init__(parent)
        self.on_select = on_select
        try:
            selected = datetime.strptime(initial_date, "%Y-%m-%d")
        except ValueError:
            selected = datetime.now() + timedelta(days=1)
        self.year = selected.year
        self.month = selected.month
        self.title("예약 날짜 선택")
        self.geometry("340x360")
        self.resizable(False, False)
        self.configure(fg_color=theme.CANVAS_COLOR)
        self.transient(parent.winfo_toplevel())
        self.grab_set()

        header = ctk.CTkFrame(self, fg_color="transparent")
        header.pack(fill="x", padx=16, pady=(14, 8))
        ctk.CTkButton(header, text="‹", width=36, command=lambda: self._move(-1)).pack(side="left")
        self.month_label = ctk.CTkLabel(header, font=theme.FONT_HEADING)
        self.month_label.pack(side="left", expand=True)
        ctk.CTkButton(header, text="›", width=36, command=lambda: self._move(1)).pack(side="right")

        self.days_frame = ctk.CTkFrame(self, fg_color="transparent")
        self.days_frame.pack(fill="both", expand=True, padx=14, pady=(0, 14))
        self._render()

    def _move(self, delta):
        month = self.month + delta
        if month < 1:
            self.year -= 1
            month = 12
        elif month > 12:
            self.year += 1
            month = 1
        self.month = month
        self._render()

    def _render(self):
        for child in self.days_frame.winfo_children():
            child.destroy()
        self.month_label.configure(text=f"{self.year}년 {self.month}월")
        for column, label in enumerate(("월", "화", "수", "목", "금", "토", "일")):
            ctk.CTkLabel(self.days_frame, text=label, text_color=theme.TEXT_MUTE).grid(
                row=0, column=column, padx=2, pady=2, sticky="nsew"
            )
            self.days_frame.columnconfigure(column, weight=1)
        today = datetime.now().date()
        for row, week in enumerate(calendar.monthcalendar(self.year, self.month), start=1):
            for column, day in enumerate(week):
                if not day:
                    continue
                value = datetime(self.year, self.month, day).date()
                button = ctk.CTkButton(
                    self.days_frame,
                    text=str(day),
                    width=36,
                    height=32,
                    fg_color=theme.ELEVATED_COLOR,
                    hover_color=theme.ACCENT_BLUE,
                    command=lambda chosen=value: self._choose(chosen),
                )
                if value < today:
                    button.configure(state="disabled", text_color=theme.TEXT_DISABLED)
                button.grid(row=row, column=column, padx=2, pady=2, sticky="nsew")

    def _choose(self, value):
        self.on_select(value.isoformat())
        self.destroy()


class TimePickerDialog(ctk.CTkToplevel):
    def __init__(self, parent, loader, on_select):
        super().__init__(parent)
        self.loader = loader
        self.on_select = on_select
        self._load_result = None
        self.title("예약 시간 조회")
        self.geometry("360x420")
        self.resizable(False, False)
        self.configure(fg_color=theme.CANVAS_COLOR)
        self.transient(parent.winfo_toplevel())
        self.grab_set()
        self.status = ctk.CTkLabel(self, text="예약 가능한 시간을 조회하고 있습니다...", text_color=theme.TEXT_MUTE)
        self.status.pack(padx=16, pady=16)
        self.list_frame = ctk.CTkScrollableFrame(self, fg_color=theme.SURFACE_COLOR)
        self.list_frame.pack(fill="both", expand=True, padx=16, pady=(0, 16))

        import threading

        threading.Thread(target=self._load, name="TimeSlotFetcher", daemon=True).start()
        self.after(50, self._poll_result)

    def _load(self):
        try:
            slots = self.loader()
            self._load_result = (slots, None)
        except Exception as exc:
            self._load_result = ([], str(exc))

    def _poll_result(self):
        if not self.winfo_exists():
            return
        if self._load_result is None:
            self.after(50, self._poll_result)
            return
        slots, error = self._load_result
        self._load_result = None
        self._render(slots, error)

    def _render(self, slots, error):
        if error:
            self.status.configure(text=f"시간 조회 실패: {error}", text_color=theme.ACCENT_RED)
            return
        available = [slot for slot in slots if slot.available]
        if not slots:
            self.status.configure(
                text="사이트가 아직 시간 버튼을 제공하지 않았습니다.",
                text_color=theme.ACCENT_YELLOW,
            )
            return
        self.status.configure(
            text=f"전체 {len(slots)}개 · 예약 가능 {len(available)}개 · 마감/미오픈 {len(slots) - len(available)}개",
            text_color=theme.ACCENT_GREEN if available else theme.ACCENT_YELLOW,
        )
        for slot in slots:
            button = ctk.CTkButton(
                self.list_frame,
                text=f"{slot.time}  {'예약 가능' if slot.available else '마감'}",
                state="normal" if slot.available else "disabled",
                fg_color=theme.ACCENT_BLUE if slot.available else theme.ELEVATED_COLOR,
                command=lambda value=slot.time: self._choose(value),
                height=34,
            )
            button.pack(fill="x", padx=4, pady=3)

    def _choose(self, value):
        self.on_select(value)
        self.destroy()


class ReservationForm(ctk.CTkFrame):
    def __init__(self, parent, start_callback, stop_callback, mode_callback=None):
        super().__init__(
            parent,
            fg_color=theme.SURFACE_COLOR,
            border_color=theme.HAIRLINE_COLOR,
            border_width=1,
            corner_radius=theme.ROUNDED_LG
        )
        self.start_callback = start_callback
        self.stop_callback = stop_callback
        self.mode_callback = mode_callback
        self.current_site = "제로월드"
        self.custom_sites = {}
        self.config = SITES_CONFIG[self.current_site]
        self._is_initializing = True
        self._save_after_id = None
        self.secret_store = SecretStore()
        
        # Thread memory states
        self.standard_threads = 30
        self.naver_threads = 5
        self.last_mode = STANDARD_MODE

        # Grid configuration for 2 columns
        self.columnconfigure((0, 1), weight=1, uniform="equal")

        # -------------------------------------------------------------
        # Row 0: Branch Selection / Day Type Selection (Dynamic)
        # -------------------------------------------------------------
        self.branch_frame = ctk.CTkFrame(self, fg_color="transparent")
        self.branch_label = ctk.CTkLabel(self.branch_frame, text="지점", font=theme.FONT_BODY_SM, text_color=theme.TEXT_MUTE)
        self.branch_label.pack(anchor="w", pady=(0, 1))
        
        self.branch_var = ctk.StringVar()
        self.branch_dropdown = ctk.CTkOptionMenu(
            self.branch_frame,
            variable=self.branch_var,
            command=self._on_branch_change,
            fg_color=theme.ELEVATED_COLOR,
            button_color=theme.ELEVATED_COLOR,
            button_hover_color=theme.CARD_COLOR,
            dropdown_fg_color=theme.SURFACE_COLOR,
            dropdown_text_color=theme.TEXT_PRIMARY,
            dropdown_hover_color=theme.CARD_COLOR,
            text_color=theme.TEXT_PRIMARY,
            font=theme.FONT_BODY_MD,
            dropdown_font=theme.FONT_BODY_MD,
            corner_radius=theme.ROUNDED_MD,
            height=28,
            anchor="w"
        )
        self.branch_dropdown.pack(fill="x")

        self.day_type_frame = ctk.CTkFrame(self, fg_color="transparent")
        self.day_type_label = ctk.CTkLabel(self.day_type_frame, text="요일 구분", font=theme.FONT_BODY_SM, text_color=theme.TEXT_MUTE)
        self.day_type_label.pack(anchor="w", pady=(0, 1))
        self.day_type_var = ctk.StringVar(value="평일")
        self.day_type_segmented = ctk.CTkSegmentedButton(
            self.day_type_frame,
            values=["평일", "주말"],
            variable=self.day_type_var,
            command=self._on_day_type_change,
            fg_color=theme.ELEVATED_COLOR,
            selected_color=theme.CARD_COLOR,
            unselected_color=theme.ELEVATED_COLOR,
            text_color=theme.TEXT_PRIMARY,
            corner_radius=theme.ROUNDED_MD,
            height=28
        )
        self.day_type_segmented.pack(fill="x")

        # -------------------------------------------------------------
        # Row 1: Theme Selection (Full Width OptionMenu)
        # -------------------------------------------------------------
        self.theme_frame = ctk.CTkFrame(self, fg_color="transparent")
        self.theme_frame.grid(row=1, column=0, columnspan=2, padx=12, pady=4, sticky="ew")
        
        self.theme_label = ctk.CTkLabel(self.theme_frame, text="테마 선택", font=theme.FONT_BODY_SM, text_color=theme.TEXT_MUTE)
        self.theme_label.pack(anchor="w", pady=(0, 1))
        
        self.theme_var = ctk.StringVar()
        self.theme_dropdown = ctk.CTkOptionMenu(
            self.theme_frame,
            variable=self.theme_var,
            command=self._on_theme_change,
            fg_color=theme.ELEVATED_COLOR,
            button_color=theme.ELEVATED_COLOR,
            button_hover_color=theme.CARD_COLOR,
            dropdown_fg_color=theme.SURFACE_COLOR,
            dropdown_text_color=theme.TEXT_PRIMARY,
            dropdown_hover_color=theme.CARD_COLOR,
            text_color=theme.TEXT_PRIMARY,
            font=theme.FONT_BODY_MD,
            dropdown_font=theme.FONT_BODY_MD,
            corner_radius=theme.ROUNDED_MD,
            height=28,
            anchor="w"
        )
        self.theme_dropdown.pack(fill="x")

        # -------------------------------------------------------------
        # Row 2: Custom Theme Entry (Full Width)
        # -------------------------------------------------------------
        self.custom_theme_frame = ctk.CTkFrame(self, fg_color="transparent")
        self.custom_theme_frame.grid(row=2, column=0, columnspan=2, padx=12, pady=4, sticky="ew")
        
        # Container to align checkboxes horizontally
        self.checkbox_container = ctk.CTkFrame(self.custom_theme_frame, fg_color="transparent")
        self.checkbox_container.pack(fill="x", anchor="w", pady=(0, 1))

        self.custom_theme_checkbox = ctk.CTkCheckBox(
            self.checkbox_container,
            text="테마 PK 직접 입력",
            font=theme.FONT_BODY_SM,
            text_color=theme.TEXT_MUTE,
            checkbox_width=14,
            checkbox_height=14,
            corner_radius=theme.ROUNDED_SM,
            fg_color=theme.ELEVATED_COLOR,
            checkmark_color=theme.ACCENT_GREEN,
            border_color=theme.HAIRLINE_COLOR,
            command=self._toggle_custom_theme
        )
        self.custom_theme_checkbox.pack(side="left", padx=(0, 15))

        self.show_server_time_checkbox = ctk.CTkCheckBox(
            self.checkbox_container,
            text="서버 시간 표시",
            font=theme.FONT_BODY_SM,
            text_color=theme.TEXT_MUTE,
            checkbox_width=14,
            checkbox_height=14,
            corner_radius=theme.ROUNDED_SM,
            fg_color=theme.ELEVATED_COLOR,
            checkmark_color=theme.ACCENT_GREEN,
            border_color=theme.HAIRLINE_COLOR,
            command=self._toggle_server_time
        )
        self.show_server_time_checkbox.pack(side="left")
        
        self.theme_pk_entry = ctk.CTkEntry(
            self.custom_theme_frame,
            placeholder_text="테마 PK 코드 (예: 27)",
            fg_color=theme.ELEVATED_COLOR,
            border_color=theme.HAIRLINE_COLOR,
            text_color=theme.TEXT_PRIMARY,
            placeholder_text_color=theme.TEXT_DISABLED,
            corner_radius=theme.ROUNDED_MD,
            height=26
        )
        self.theme_pk_entry.pack(fill="x")
        self.theme_pk_entry.pack_forget()

        # -------------------------------------------------------------
        # Row 3: Date & Time (Split row)
        # -------------------------------------------------------------
        self.date_frame = ctk.CTkFrame(self, fg_color="transparent")
        self.date_frame.grid(row=3, column=0, padx=(12, 4), pady=4, sticky="ew")
        self.date_label = ctk.CTkLabel(self.date_frame, text="날짜 (YYYY-MM-DD)", font=theme.FONT_BODY_SM, text_color=theme.TEXT_MUTE)
        self.date_label.pack(anchor="w", pady=(0, 1))
        
        tomorrow = (datetime.now() + timedelta(days=1)).strftime("%Y-%m-%d")
        self.date_entry = ctk.CTkEntry(
            self.date_frame,
            placeholder_text="예: 2026-06-01",
            fg_color=theme.ELEVATED_COLOR,
            border_color=theme.HAIRLINE_COLOR,
            text_color=theme.TEXT_PRIMARY,
            placeholder_text_color=theme.TEXT_DISABLED,
            corner_radius=theme.ROUNDED_MD,
            height=28
        )
        self.date_entry.insert(0, tomorrow)
        self.date_picker_btn = ctk.CTkButton(
            self.date_frame,
            text="📅",
            width=34,
            height=28,
            fg_color=theme.ELEVATED_COLOR,
            hover_color=theme.CARD_COLOR,
            command=self._open_date_picker,
        )
        self.date_picker_btn.pack(side="right", padx=(4, 0))
        self.date_entry.pack(side="left", fill="x", expand=True)
        self.date_entry.bind("<KeyRelease>", self._format_date)
        self.date_entry.bind("<FocusOut>", self._on_date_change)

        self.time_frame = ctk.CTkFrame(self, fg_color="transparent")
        self.time_frame.grid(row=3, column=1, padx=(4, 12), pady=4, sticky="ew")
        self.time_label = ctk.CTkLabel(self.time_frame, text="시간 (HH:MM)", font=theme.FONT_BODY_SM, text_color=theme.TEXT_MUTE)
        self.time_label.pack(anchor="w", pady=(0, 1))
        
        self.time_entry = ctk.CTkEntry(
            self.time_frame,
            placeholder_text="예: 14:00",
            fg_color=theme.ELEVATED_COLOR,
            border_color=theme.HAIRLINE_COLOR,
            text_color=theme.TEXT_PRIMARY,
            placeholder_text_color=theme.TEXT_DISABLED,
            corner_radius=theme.ROUNDED_MD,
            height=28
        )
        self.time_picker_btn = ctk.CTkButton(
            self.time_frame,
            text="조회",
            width=42,
            height=28,
            fg_color=theme.ELEVATED_COLOR,
            hover_color=theme.CARD_COLOR,
            command=self._open_time_picker,
        )
        self.time_picker_btn.pack(side="right", padx=(4, 0))
        self.time_entry.pack(side="left", fill="x", expand=True)
        self.time_entry.bind("<KeyRelease>", self._format_time)

        # -------------------------------------------------------------
        # Row 4: Name & People (Split row)
        # -------------------------------------------------------------
        self.name_frame = ctk.CTkFrame(self, fg_color="transparent")
        self.name_frame.grid(row=4, column=0, padx=(12, 4), pady=4, sticky="ew")
        self.name_label = ctk.CTkLabel(self.name_frame, text="이름", font=theme.FONT_BODY_SM, text_color=theme.TEXT_MUTE)
        self.name_label.pack(anchor="w", pady=(0, 1))
        
        self.name_entry = ctk.CTkEntry(
            self.name_frame,
            placeholder_text="예약자명",
            fg_color=theme.ELEVATED_COLOR,
            border_color=theme.HAIRLINE_COLOR,
            text_color=theme.TEXT_PRIMARY,
            placeholder_text_color=theme.TEXT_DISABLED,
            corner_radius=theme.ROUNDED_MD,
            height=28
        )
        self.name_entry.pack(fill="x")

        self.people_frame = ctk.CTkFrame(self, fg_color="transparent")
        self.people_frame.grid(row=4, column=1, padx=(4, 12), pady=4, sticky="ew")
        self.people_label = ctk.CTkLabel(self.people_frame, text="인원 수", font=theme.FONT_BODY_SM, text_color=theme.TEXT_MUTE)
        self.people_label.pack(anchor="w", pady=(0, 1))
        
        self.people_entry = ctk.CTkEntry(
            self.people_frame,
            placeholder_text="2",
            fg_color=theme.ELEVATED_COLOR,
            border_color=theme.HAIRLINE_COLOR,
            text_color=theme.TEXT_PRIMARY,
            placeholder_text_color=theme.TEXT_DISABLED,
            corner_radius=theme.ROUNDED_MD,
            height=28
        )
        self.people_entry.insert(0, "2")
        self.people_entry.pack(fill="x")

        # -------------------------------------------------------------
        # Row 5: Phone Number (Full Width)
        # -------------------------------------------------------------
        self.phone_frame = ctk.CTkFrame(self, fg_color="transparent")
        self.phone_frame.grid(row=5, column=0, columnspan=2, padx=12, pady=4, sticky="ew")
        self.phone_label = ctk.CTkLabel(self.phone_frame, text="전화번호", font=theme.FONT_BODY_SM, text_color=theme.TEXT_MUTE)
        self.phone_label.pack(anchor="w", pady=(0, 1))
        
        self.phone_entry = ctk.CTkEntry(
            self.phone_frame,
            placeholder_text="예: 010-1234-5678",
            fg_color=theme.ELEVATED_COLOR,
            border_color=theme.HAIRLINE_COLOR,
            text_color=theme.TEXT_PRIMARY,
            placeholder_text_color=theme.TEXT_DISABLED,
            corner_radius=theme.ROUNDED_MD,
            height=28
        )
        self.phone_entry.pack(fill="x")
        self.phone_entry.bind("<KeyRelease>", self._format_phone)

        # -------------------------------------------------------------
        # Advanced: concurrent attempts (shown below the advanced toggle)
        # -------------------------------------------------------------
        self.threads_frame = ctk.CTkFrame(self, fg_color="transparent")
        self.threads_frame.grid(row=8, column=0, columnspan=2, padx=12, pady=(4, 8), sticky="ew")

        # Labels container (to pack title and value side-by-side)
        self.threads_label_frame = ctk.CTkFrame(self.threads_frame, fg_color="transparent")
        self.threads_label_frame.pack(side="left")

        self.threads_title_label = ctk.CTkLabel(
            self.threads_label_frame,
            text="동시 시도 수",
            font=theme.FONT_BODY_SM,
            text_color=theme.TEXT_MUTE
        )
        self.threads_title_label.pack(side="left")

        # Dynamic value badge
        self.threads_badge = ctk.CTkFrame(
            self.threads_label_frame,
            fg_color=theme.ELEVATED_COLOR,
            border_color=theme.HAIRLINE_COLOR,
            border_width=1,
            corner_radius=theme.ROUNDED_SM
        )
        self.threads_badge.pack(side="left", padx=(8, 0))

        self.threads_value_label = ctk.CTkLabel(
            self.threads_badge,
            text="30",
            font=(theme.FONT_FAMILY, 10, "bold"),
            text_color=theme.ACCENT_BLUE,
            height=16
        )
        self.threads_value_label.pack(padx=6, pady=1)

        self.threads_slider = ctk.CTkSlider(
            self.threads_frame,
            from_=1,
            to=50,
            number_of_steps=49,
            fg_color=theme.ELEVATED_COLOR,
            progress_color=theme.ACCENT_BLUE,
            button_color=theme.ACCENT_WHITE,
            button_hover_color=theme.TEXT_BODY,
            command=self._on_threads_slider_move,
            height=8,
            corner_radius=4,
            button_length=14,
            button_corner_radius=7
        )
        self.threads_slider.set(30)
        self.threads_slider.pack(side="right", fill="x", expand=True, padx=(12, 0))

        # -------------------------------------------------------------
        # Row 6: Booking Method
        # -------------------------------------------------------------
        self.engine_mode_frame = ctk.CTkFrame(self, fg_color="transparent")
        self.engine_mode_frame.grid(row=6, column=0, columnspan=2, padx=12, pady=(4, 10), sticky="ew")

        self.engine_mode_label = ctk.CTkLabel(
            self.engine_mode_frame,
            text="사이트 유형",
            font=theme.FONT_BODY_SM,
            text_color=theme.TEXT_MUTE
        )
        self.engine_mode_label.pack(side="left", anchor="w")

        self.engine_mode_btn = ctk.CTkSegmentedButton(
            self.engine_mode_frame,
            values=[STANDARD_MODE, NAVER_MODE],
            font=theme.FONT_BODY_SM,
            fg_color=theme.ELEVATED_COLOR,
            selected_color=theme.ACCENT_BLUE,
            selected_hover_color=theme.ACCENT_BLUE,
            text_color=theme.TEXT_PRIMARY,
            corner_radius=theme.ROUNDED_MD,
            height=28,
            command=self._on_mode_change
        )
        self.engine_mode_btn.set(STANDARD_MODE)
        self.engine_mode_btn.pack(side="right", fill="x", expand=False)

        # -------------------------------------------------------------
        # Row 7: Advanced settings toggle
        # -------------------------------------------------------------
        self.advanced_toggle_btn = ctk.CTkButton(
            self,
            text="고급 설정  ▾",
            font=theme.FONT_BODY_SM,
            text_color=theme.TEXT_MUTE,
            fg_color="transparent",
            hover_color=theme.ELEVATED_COLOR,
            anchor="w",
            height=26,
            command=self._toggle_advanced,
        )
        self.advanced_toggle_btn.grid(row=7, column=0, columnspan=2, padx=12, pady=(0, 4), sticky="ew")

        self.advanced_frame = ctk.CTkFrame(self, fg_color="transparent")
        self.advanced_frame.columnconfigure(0, weight=1)

        self.remember_personal_var = ctk.BooleanVar(value=True)
        self.remember_personal_checkbox = ctk.CTkCheckBox(
            self.advanced_frame,
            text="이름과 전화번호를 이 PC에 기억",
            variable=self.remember_personal_var,
            font=theme.FONT_BODY_SM,
            text_color=theme.TEXT_MUTE,
            checkbox_width=14,
            checkbox_height=14,
            command=self.auto_save,
        )
        self.remember_personal_checkbox.grid(row=0, column=0, sticky="w", pady=(0, 6))

        self.api_key_entry = ctk.CTkEntry(
            self.advanced_frame,
            placeholder_text="YesCaptcha API 키 (선택 사항)",
            show="•",
            fg_color=theme.ELEVATED_COLOR,
            border_color=theme.HAIRLINE_COLOR,
            text_color=theme.TEXT_PRIMARY,
            placeholder_text_color=theme.TEXT_DISABLED,
            corner_radius=theme.ROUNDED_MD,
            height=28,
        )
        self.api_key_entry.grid(row=1, column=0, sticky="ew")
        self.api_key_entry.bind("<FocusOut>", self._save_secret_settings)

        self.catalog_auto_refresh_var = ctk.BooleanVar(value=True)
        self.catalog_auto_refresh_checkbox = ctk.CTkCheckBox(
            self.advanced_frame,
            text="시작 시 사이트 정보 자동 갱신",
            variable=self.catalog_auto_refresh_var,
            font=theme.FONT_BODY_SM,
            text_color=theme.TEXT_MUTE,
            checkbox_width=14,
            checkbox_height=14,
            command=self.auto_save,
        )
        self.catalog_auto_refresh_checkbox.grid(row=2, column=0, sticky="w", pady=(8, 6))

        self.catalog_refresh_frame = ctk.CTkFrame(self.advanced_frame, fg_color="transparent")
        self.catalog_refresh_frame.grid(row=3, column=0, sticky="ew")
        self.catalog_refresh_frame.columnconfigure(1, weight=1)
        self.catalog_refresh_btn = ctk.CTkButton(
            self.catalog_refresh_frame,
            text="현재 사이트 갱신",
            width=118,
            height=28,
            font=theme.FONT_BODY_SM,
            fg_color=theme.ELEVATED_COLOR,
            hover_color=theme.CARD_COLOR,
            command=self._request_catalog_refresh,
        )
        self.catalog_refresh_btn.grid(row=0, column=0, sticky="w")
        self.catalog_refresh_status = ctk.CTkLabel(
            self.catalog_refresh_frame,
            text="갱신 기록 없음",
            font=theme.FONT_BODY_SM,
            text_color=theme.TEXT_DISABLED,
            anchor="e",
        )
        self.catalog_refresh_status.grid(row=0, column=1, sticky="e", padx=(8, 0))
        self.catalog_change_badge = ctk.CTkLabel(
            self.catalog_refresh_frame,
            text="",
            width=0,
            font=(theme.FONT_FAMILY, 10, "bold"),
            text_color=theme.ACCENT_YELLOW,
        )
        self.catalog_change_badge.grid(row=0, column=2, sticky="e", padx=(6, 0))
        self.catalog_change_badge.bind("<Button-1>", lambda _event: self._show_catalog_pending())
        self._advanced_visible = False

        # Row 9: Developer Test Mode (Naver only)
        # -------------------------------------------------------------
        self.dev_mode_frame = ctk.CTkFrame(self, fg_color="transparent")
        # Frame placement is handled by _update_widgets_state dynamically
        
        self.dev_mode_var = ctk.BooleanVar(value=False)
        self.dev_mode_checkbox = ctk.CTkCheckBox(
            self.dev_mode_frame,
            text="개발자 테스트 모드 (화면 표시 & 최종 예약 안함)",
            variable=self.dev_mode_var,
            font=theme.FONT_BODY_SM,
            fg_color=theme.ACCENT_BLUE,
            hover_color=theme.ACCENT_BLUE,
            text_color=theme.TEXT_PRIMARY,
            corner_radius=theme.ROUNDED_SM,
            command=self.auto_save
        )
        self.dev_mode_checkbox.pack(side="left", anchor="w")

        # Setup focus effects for entries
        self._setup_entry_focus(self.theme_pk_entry)
        self._setup_entry_focus(self.date_entry)
        self._setup_entry_focus(self.time_entry)
        self._setup_entry_focus(self.name_entry)
        self._setup_entry_focus(self.people_entry)
        self._setup_entry_focus(self.phone_entry)
        self._setup_entry_focus(self.api_key_entry)

        # Initialize layout
        self.set_site(self.current_site)
        self._update_widgets_state()
        self._is_initializing = False

    def _setup_entry_focus(self, entry):
        # Configure thin Apple hairline border
        entry.configure(border_width=1, font=theme.FONT_BODY_MD)
        entry.bind("<FocusIn>", lambda e: entry.configure(border_color=theme.ACCENT_BLUE) if entry.cget("state") == "normal" else None, add="+")
        entry.bind("<FocusOut>", lambda e: entry.configure(border_color=theme.HAIRLINE_COLOR), add="+")
        entry.bind("<KeyRelease>", lambda e: self.auto_save(), add="+")
        entry.bind("<FocusOut>", lambda e: self.auto_save(), add="+")

    def _open_date_picker(self):
        DatePickerDialog(self, self.date_entry.get().strip(), self._set_selected_date)

    def _set_selected_date(self, value):
        self.date_entry.delete(0, "end")
        self.date_entry.insert(0, value)
        self._on_date_change()

    def _open_time_picker(self):
        if self.current_site != "제로월드":
            from tkinter import messagebox

            messagebox.showinfo("시간 조회", "현재는 제로월드의 실시간 시간 조회를 지원합니다.", parent=self)
            return
        branch_id = self.config.get("branches", {}).get(self.branch_var.get(), "")
        theme_id = ZEROWORLD_THEMES.get(branch_id, {}).get(self.theme_var.get(), "")
        reservation_date = self.date_entry.get().strip()
        if not branch_id or not theme_id or len(reservation_date) != 10:
            from tkinter import messagebox

            messagebox.showwarning("시간 조회", "지점, 테마, 날짜를 먼저 선택해주세요.", parent=self)
            return

        def loader():
            from engines.zeroworld_catalog import fetch_time_slots

            return fetch_time_slots(branch_id, reservation_date, theme_id)

        TimePickerDialog(self, loader, self._set_selected_time)

    def _set_selected_time(self, value):
        self.time_entry.delete(0, "end")
        self.time_entry.insert(0, value)
        self.auto_save()

    def _toggle_advanced(self):
        self._advanced_visible = not self._advanced_visible
        if self._advanced_visible:
            self.advanced_frame.grid(row=9, column=0, columnspan=2, padx=12, pady=(0, 8), sticky="ew")
            self.advanced_toggle_btn.configure(text="고급 설정  ▴")
        else:
            self.advanced_frame.grid_forget()
            self.advanced_toggle_btn.configure(text="고급 설정  ▾")
        self._update_widgets_state()

    def _save_secret_settings(self, event=None):
        try:
            self.secret_store.set("yescaptcha_api_key", self.api_key_entry.get().strip())
        except RuntimeError as exc:
            if hasattr(self.master, "log_panel"):
                self.master.log_panel.append_log(str(exc), "error")

    def _request_catalog_refresh(self):
        if hasattr(self.master, "_refresh_current_catalog"):
            self.master._refresh_current_catalog()

    def _show_catalog_pending(self):
        if hasattr(self.master, "_show_catalog_pending"):
            self.master._show_catalog_pending()

    def set_catalog_refresh_state(self, text, pending_count=0, changed_count=0, busy=False):
        self.catalog_refresh_status.configure(text=text)
        badge_parts = []
        if changed_count:
            badge_parts.append(f"변경 {changed_count}")
        if pending_count:
            badge_parts.append(f"확인 {pending_count}")
        self.catalog_change_badge.configure(text=" · ".join(badge_parts))
        if not getattr(self, "_booking_running", False):
            self.catalog_refresh_btn.configure(state="disabled" if busy else "normal")

    def _on_mode_change(self, mode):
        # Save current slider value to appropriate variable before switching, but not during initialization
        if not getattr(self, "_is_initializing", False):
            if self.last_mode == NAVER_MODE:
                self.naver_threads = int(self.threads_slider.get())
            else:
                self.standard_threads = int(self.threads_slider.get())

        if mode == NAVER_MODE:
            # Restrict threads slider to maximum of 8 and load cached value
            self.threads_slider.configure(to=8, number_of_steps=7)
            self.threads_slider.set(self.naver_threads)
            self.threads_value_label.configure(text=str(self.naver_threads))
        else:
            self.set_site(self.current_site)
            self.standard_threads = max(1, min(self.standard_threads, 50))
            self.threads_slider.configure(to=50, number_of_steps=49)
            self.threads_slider.set(self.standard_threads)
            self.threads_value_label.configure(text=str(self.standard_threads))
            
        self._update_widgets_state()
            
        self.last_mode = mode
        if self.mode_callback:
            self.mode_callback(mode)

    def _update_widgets_state(self):
        if getattr(self, "_booking_running", False):
            return
        is_naver = (self.engine_mode_btn.get() == NAVER_MODE)
        
        if getattr(self, "_advanced_visible", False):
            self.threads_frame.grid(row=8, column=0, columnspan=2, padx=12, pady=(4, 8), sticky="ew")
        else:
            self.threads_frame.grid_forget()
        if self.current_site == "키이스케이프":
            self.threads_slider.configure(state="disabled")
            self.threads_slider.set(1)
            self.threads_value_label.configure(text="1", text_color=theme.TEXT_DISABLED)
            self.threads_title_label.configure(text_color=theme.TEXT_DISABLED)
        else:
            self.threads_slider.configure(state="normal")
            self.threads_title_label.configure(text_color=theme.TEXT_MUTE)
            self.threads_value_label.configure(text_color=theme.ACCENT_BLUE)
            if is_naver:
                self.threads_slider.set(self.naver_threads)
                self.threads_value_label.configure(text=str(self.naver_threads))
            else:
                self.threads_slider.set(self.standard_threads)
                self.threads_value_label.configure(text=str(self.standard_threads))

        if is_naver:
            # Disable Naver-incompatible controls but keep them in layout to prevent vertical layout shifting
            self.branch_dropdown.configure(state="disabled")
            self.branch_label.configure(text_color=theme.TEXT_DISABLED)
            
            self.day_type_segmented.configure(state="disabled")
            self.day_type_label.configure(text_color=theme.TEXT_DISABLED)
            
            themes_dict = self.config.get("themes", {}).get("1", {})
            has_themes = len(themes_dict) > 0 and not (len(themes_dict) == 1 and list(themes_dict.keys())[0] == "기본테마")
            if has_themes:
                self.theme_dropdown.configure(state="normal")
                self.theme_label.configure(text_color=theme.TEXT_MUTE)
            else:
                self.theme_dropdown.configure(state="disabled")
                self.theme_label.configure(text_color=theme.TEXT_DISABLED)
            
            self.custom_theme_checkbox.configure(state="disabled", text_color=theme.TEXT_DISABLED)
            self.theme_pk_entry.configure(state="disabled", text_color=theme.TEXT_DISABLED)
            
            # Disable phone entry as Naver Booking autofills phone from active logged in user session
            self.phone_entry.configure(state="disabled", text_color=theme.TEXT_DISABLED)
            self.phone_label.configure(text_color=theme.TEXT_DISABLED)
            
            # Show frame and enable Developer Mode checkbox
            self.dev_mode_frame.grid(row=10, column=0, columnspan=2, padx=12, pady=(4, 10), sticky="ew")
            self.dev_mode_checkbox.configure(state="normal", text_color=theme.TEXT_PRIMARY)
            self.engine_mode_frame.grid(row=6, column=0, columnspan=2, padx=12, pady=4, sticky="ew")
        else:
            # Enable standard controls
            self.branch_dropdown.configure(state="normal")
            self.branch_label.configure(text_color=theme.TEXT_MUTE)
            
            self.day_type_segmented.configure(state="normal")
            self.day_type_label.configure(text_color=theme.TEXT_MUTE)
            
            self.theme_label.configure(text_color=theme.TEXT_MUTE)
            self.custom_theme_checkbox.configure(state="normal", text_color=theme.TEXT_MUTE)
            self.theme_pk_entry.configure(state="normal", text_color=theme.TEXT_PRIMARY)
            
            self.phone_entry.configure(state="normal", text_color=theme.TEXT_PRIMARY)
            self.phone_label.configure(text_color=theme.TEXT_MUTE)
            
            # Hide and uncheck Developer Mode checkbox
            self.dev_mode_frame.grid_forget()
            self.dev_mode_var.set(False)
            self._toggle_custom_theme()
            self.engine_mode_frame.grid(row=6, column=0, columnspan=2, padx=12, pady=(4, 10), sticky="ew")

        # Update Server Time Checkbox state
        current_mode = self.engine_mode_btn.get()
        site_name = self.current_site
        is_supported_site = (current_mode == NAVER_MODE and site_name != "(네이버 예약을 등록하세요)") or (site_name == "키이스케이프")

        if is_supported_site:
            self.show_server_time_checkbox.configure(state="normal", text_color=theme.TEXT_MUTE)
        else:
            if self.show_server_time_checkbox.get() == 1:
                self.show_server_time_checkbox.deselect()
                self._toggle_server_time()
            self.show_server_time_checkbox.configure(state="disabled", text_color=theme.TEXT_DISABLED)

    def set_running_state(self, running: bool):
        self._booking_running = running
        state = "disabled" if running else "normal"
        widgets = (
            self.branch_dropdown,
            self.day_type_segmented,
            self.theme_dropdown,
            self.custom_theme_checkbox,
            self.theme_pk_entry,
            self.date_entry,
            self.date_picker_btn,
            self.time_entry,
            self.time_picker_btn,
            self.name_entry,
            self.people_entry,
            self.phone_entry,
            self.threads_slider,
            self.engine_mode_btn,
            self.show_server_time_checkbox,
            self.dev_mode_checkbox,
            self.advanced_toggle_btn,
            self.remember_personal_checkbox,
            self.api_key_entry,
            self.catalog_auto_refresh_checkbox,
            self.catalog_refresh_btn,
        )
        for widget in widgets:
            try:
                widget.configure(state=state)
            except Exception:
                continue
        if not running:
            self._update_widgets_state()

    def set_site(self, site_name):
        was_initializing = getattr(self, "_is_initializing", False)
        self._is_initializing = True
        self.current_site = site_name
        if site_name in self.custom_sites:
            self.config = self.custom_sites[site_name]
            # Custom sites do not use differentiated weekdays/weekends configurations in SITES_CONFIG
            has_weekday_weekend = False
        elif site_name in SITES_CONFIG:
            self.config = SITES_CONFIG[site_name]
            has_weekday_weekend = self.config["has_weekday_weekend"]
        else:
            # Fallback for dummy values (e.g. "(네이버 예약을 등록하세요)")
            self.config = {
                "branches": {},
                "themes": {},
                "has_weekday_weekend": False
            }
            has_weekday_weekend = False

        self.branch_frame.grid_forget()
        self.day_type_frame.grid_forget()

        # In Naver mode, hide all standard-engine-only form sections entirely, but show theme selection if there are multiple themes
        is_naver = (self.engine_mode_btn.get() == NAVER_MODE)
        if is_naver:
            self.custom_theme_frame.grid_forget()
            themes_dict = self.config.get("themes", {}).get("1", {})
            has_themes = len(themes_dict) > 0 and not (len(themes_dict) == 1 and list(themes_dict.keys())[0] == "기본테마")
            if has_themes:
                self.theme_frame.grid(row=1, column=0, columnspan=2, padx=12, pady=4, sticky="ew")
            else:
                self.theme_frame.grid_forget()
        else:
            # Keep theme and custom theme frames always mapped in grid to prevent vertical jumping
            self.theme_frame.grid(row=1, column=0, columnspan=2, padx=12, pady=4, sticky="ew")
            self.custom_theme_frame.grid(row=2, column=0, columnspan=2, padx=12, pady=4, sticky="ew")

            if has_weekday_weekend:
                # Show both branch and day type selection side by side
                self.branch_frame.grid(row=0, column=0, padx=(12, 4), pady=(10, 4), sticky="ew")
                self.day_type_frame.grid(row=0, column=1, padx=(4, 12), pady=(10, 4), sticky="ew")
                branch_options = list(self.config["branches"].keys())
                self.branch_dropdown.configure(values=branch_options)
                if branch_options:
                    prev_val = self.branch_var.get()
                    if prev_val in branch_options:
                        self.branch_var.set(prev_val)
                    else:
                        self.branch_var.set(branch_options[0])
            else:
                self.branch_frame.grid(row=0, column=0, columnspan=2, padx=12, pady=(10, 4), sticky="ew")
                branch_options = list(self.config["branches"].keys())
                self.branch_dropdown.configure(values=branch_options)
                if branch_options:
                    prev_val = self.branch_var.get()
                    if prev_val in branch_options:
                        self.branch_var.set(prev_val)
                    else:
                        self.branch_var.set(branch_options[0])

        self._update_theme_options()
        self._update_widgets_state()
        self._is_initializing = was_initializing

    def _on_branch_change(self, value):
        self._update_theme_options()
        self.auto_save()

    def _on_day_type_change(self, value):
        self._update_theme_options()
        self.auto_save()

    def _toggle_custom_theme(self):
        # Only run toggle behavior if not in Naver mode to prevent overriding disabled state
        if self.engine_mode_btn.get() == NAVER_MODE:
            return
        if self.custom_theme_checkbox.get() == 1:
            self.theme_dropdown.configure(state="disabled")
            self.theme_pk_entry.pack(fill="x", after=self.checkbox_container, pady=(2, 0))
        else:
            self.theme_dropdown.configure(state="normal")
            self.theme_pk_entry.pack_forget()
        self.auto_save()

    def _toggle_server_time(self):
        # Call MainWindow update function if master has it
        if hasattr(self.master, "_update_server_time_sync_state"):
            self.master._update_server_time_sync_state()
        self.auto_save()

    def _on_threads_slider_move(self, value):
        if self.current_site == "키이스케이프":
            return
        val = int(value)
        self.threads_value_label.configure(text=str(val))
        if self.engine_mode_btn.get() == NAVER_MODE:
            self.naver_threads = val
        else:
            self.standard_threads = val
        self.auto_save()

    def _on_date_change(self, event=None):
        """Auto-detect weekday/weekend from the entered date."""
        if not self.current_site.startswith("제로월드"):
            return
        date_str = self.date_entry.get().strip()
        if len(date_str) != 10:
            return
        try:
            dt = datetime.strptime(date_str, "%Y-%m-%d")
            day_of_week = dt.weekday()  # 0=Mon ... 6=Sun
            if day_of_week >= 5:  # Saturday or Sunday
                self.day_type_var.set("주말")
            else:
                self.day_type_var.set("평일")
            self._update_theme_options()
            self.auto_save()
        except ValueError:
            pass

    def _on_theme_change(self, value):
        self.auto_save()

    def auto_save(self):
        if getattr(self, "_is_initializing", False):
            return
        if self._save_after_id:
            try:
                self.after_cancel(self._save_after_id)
            except Exception:
                pass
        self._save_after_id = self.after(400, self._perform_auto_save)

    def _perform_auto_save(self):
        self._save_after_id = None
        if hasattr(self, "current_site") and self.current_site:
            self.save_config(self.current_site)

    def _update_theme_options(self):
        if self.current_site in self.custom_sites:
            branch_name = self.branch_var.get()
            branch_id = self.config["branches"].get(branch_name, "1")
            themes_dict = self.config["themes"].get(branch_id, {})
            theme_names = sorted(list(themes_dict.keys()))
        elif self.current_site == "둠이스케이프":
            branch_name = self.branch_var.get()
            branch_id = self.config["branches"].get(branch_name, "3")
            themes_dict = DOOMESCAPE_THEMES.get(branch_id, {})
            theme_names = sorted(list(themes_dict.keys()))
        elif self.current_site == "제로월드":
            branch_name = self.branch_var.get()
            branch_id = self.config["branches"].get(branch_name, "1")
            themes_dict = ZEROWORLD_THEMES.get(branch_id, {})
            theme_names = sorted(list(themes_dict.keys()))
        elif self.current_site == "비트포비아 던전":
            branch_name = self.branch_var.get()
            branch_id = self.config["branches"].get(branch_name, "3")
            themes_dict = PHOBIADUNGEON_THEMES.get(branch_id, {})
            theme_names = sorted(list(themes_dict.keys()))
        elif self.current_site == "키이스케이프":
            branch_name = self.branch_var.get()
            branch_id = self.config["branches"].get(branch_name, "14")
            themes_dict = KEYESCAPE_THEMES.get(branch_id, {})
            theme_names = sorted(list(themes_dict.keys()))
        else:
            branch_name = self.branch_var.get()
            branch_id = self.config["branches"].get(branch_name, "1")
            themes_dict = JIGUBYEOL_THEMES.get(branch_id, {})
            theme_names = sorted(list(themes_dict.keys()))

        self.theme_dropdown.configure(values=theme_names)
        if theme_names:
            prev_theme = self.theme_var.get()
            if prev_theme in theme_names:
                self.theme_var.set(prev_theme)
            else:
                self.theme_var.set(theme_names[0])
        else:
            self.theme_var.set("")

    def get_reservation_data(self):
        is_naver = self.engine_mode_btn.get() == NAVER_MODE
        branch_name = self.branch_var.get()
        branches = self.config.get("branches", {})
        if not branch_name and branches:
            branch_name = next(iter(branches))
        branch_id = "1" if is_naver else branches.get(branch_name, "1")

        theme_name = self.theme_var.get()
        if is_naver:
            theme_pk = self.config.get("themes", {}).get("1", {}).get(theme_name, "")
            if not theme_pk or theme_pk == "naver":
                theme_pk = self.config.get("url", "")
        elif self.custom_theme_checkbox.get() == 1:
            theme_pk = self.theme_pk_entry.get().strip()
        elif self.current_site in self.custom_sites:
            theme_pk = self.config.get("themes", {}).get(branch_id, {}).get(theme_name, "")
        elif self.current_site == "둠이스케이프":
            theme_pk = DOOMESCAPE_THEMES.get(branch_id, {}).get(theme_name, "")
        elif self.current_site == "제로월드":
            theme_pk = ZEROWORLD_THEMES.get(branch_id, {}).get(theme_name, "")
        elif self.current_site == "비트포비아 던전":
            theme_pk = theme_name
        elif self.current_site == "키이스케이프":
            theme_pk = KEYESCAPE_THEMES.get(branch_id, {}).get(theme_name, {}).get("info_num", "")
        else:
            theme_pk = JIGUBYEOL_THEMES.get(branch_id, {}).get(theme_name, "")

        raw_values = {
            "branch": branch_id,
            "branchLabel": self.branch_var.get(),
            "reservationDate": self.date_entry.get().strip(),
            "name": self.name_entry.get().strip(),
            "phone": self.phone_entry.get().strip(),
            "people": self.people_entry.get().strip(),
            "themePK": theme_pk,
            "themeLabel": self.theme_var.get() if not self.custom_theme_checkbox.get() else f"직접 입력 ({theme_pk})",
            "reservationTime": self.time_entry.get().strip(),
            "paymentType": "1",
            "policy": "true",
            "devMode": self.dev_mode_var.get(),
            "site_url": self.config.get("url", ""),
            "engine_metadata": {
                "branch": self.config.get("branch_metadata", {}).get(branch_id, {}),
                "theme": self.config.get("theme_metadata", {}).get(branch_id, {}).get(theme_pk, {}),
                "engine_options": self.config.get("engine_options", {}),
            },
        }
        try:
            request = ReservationRequest.from_mapping(self.current_site, raw_values)
        except ValueError:
            return None, "인원 수를 숫자로 입력해주세요.", 0, False
        errors = request.validate(phone_required=not is_naver)
        if errors:
            return None, errors[0], 0, False

        threads = 1 if self.current_site == "키이스케이프" else int(self.threads_slider.get())
        return request, None, threads, not is_naver

    def _format_phone(self, event=None):
        if event and event.keysym in ("BackSpace", "Delete", "Left", "Right", "Up", "Down"):
            return
            
        text = self.phone_entry.get()
        digits = "".join(c for c in text if c.isdigit())
        
        formatted = ""
        if digits.startswith("02"):
            if len(digits) <= 2:
                formatted = digits
            elif len(digits) <= 5:
                formatted = f"{digits[:2]}-{digits[2:]}"
            elif len(digits) <= 9:
                formatted = f"{digits[:2]}-{digits[2:5]}-{digits[5:]}"
            else:
                formatted = f"{digits[:2]}-{digits[2:6]}-{digits[6:]}"
        else:
            if len(digits) <= 3:
                formatted = digits
            elif len(digits) <= 6:
                formatted = f"{digits[:3]}-{digits[3:]}"
            elif len(digits) <= 10:
                formatted = f"{digits[:3]}-{digits[3:6]}-{digits[6:]}"
            else:
                formatted = f"{digits[:3]}-{digits[3:7]}-{digits[7:11]}"
                
        current_cursor = self.phone_entry.index("insert")
        hyphens_before = text[:current_cursor].count("-")
        
        self.phone_entry.delete(0, "end")
        self.phone_entry.insert(0, formatted)
        
        hyphens_after = formatted.count("-")
        new_cursor = current_cursor + (hyphens_after - hyphens_before)
        new_cursor = max(0, min(new_cursor, len(formatted)))
        self.phone_entry.icursor(new_cursor)

    def _format_date(self, event=None):
        if event and event.keysym in ("BackSpace", "Delete", "Left", "Right", "Up", "Down"):
            return
            
        text = self.date_entry.get()
        digits = "".join(c for c in text if c.isdigit())
        
        formatted = ""
        if len(digits) <= 4:
            formatted = digits
        elif len(digits) <= 6:
            formatted = f"{digits[:4]}-{digits[4:]}"
        else:
            formatted = f"{digits[:4]}-{digits[4:6]}-{digits[6:8]}"
            
        current_cursor = self.date_entry.index("insert")
        hyphens_before = text[:current_cursor].count("-")
        
        self.date_entry.delete(0, "end")
        self.date_entry.insert(0, formatted)
        
        hyphens_after = formatted.count("-")
        new_cursor = current_cursor + (hyphens_after - hyphens_before)
        new_cursor = max(0, min(new_cursor, len(formatted)))
        self.date_entry.icursor(new_cursor)
        
        self._on_date_change(event)

    def _format_time(self, event=None):
        if event and event.keysym in ("BackSpace", "Delete", "Left", "Right", "Up", "Down"):
            return
            
        text = self.time_entry.get()
        digits = "".join(c for c in text if c.isdigit())
        
        formatted = ""
        if len(digits) <= 2:
            formatted = digits
        else:
            formatted = f"{digits[:2]}:{digits[2:4]}"
            
        current_cursor = self.time_entry.index("insert")
        colons_before = text[:current_cursor].count(":")
        
        self.time_entry.delete(0, "end")
        self.time_entry.insert(0, formatted)
        
        colons_after = formatted.count(":")
        new_cursor = current_cursor + (colons_after - colons_before)
        new_cursor = max(0, min(new_cursor, len(formatted)))
        self.time_entry.icursor(new_cursor)

    def load_config(self):
        config = load_json("config.json", {})
        api_key = self.secret_store.get("yescaptcha_api_key")
        stored_name = self.secret_store.get("reservation_name")
        stored_phone = self.secret_store.get("reservation_phone")
        if api_key:
            self.api_key_entry.delete(0, "end")
            self.api_key_entry.insert(0, api_key)
        if not config:
            if stored_name:
                self.name_entry.insert(0, stored_name)
            if stored_phone:
                self.phone_entry.insert(0, stored_phone)
            return
        self._is_initializing = True
        config_migrated = False
        try:
            remember_personal = bool(config.get("remember_personal_info", True))
            self.remember_personal_var.set(remember_personal)
            saved_site = config.get("site", "제로월드")
            if saved_site in {"제로월드(신)", "제로월드(구)", "제로월드 강남", "제로월드 홍대"}:
                if "강남" in saved_site:
                    config["branch"] = "강남점"
                elif "홍대" in saved_site:
                    config["branch"] = "홍대점"
                saved_site = "제로월드"
            if saved_site == self.current_site:
                saved_branch = config.get("branch", "")
                saved_branch_id = str(config.get("selected_branch_id", ""))
                if saved_branch_id:
                    stable_branch_ids = self.config.get("branch_ids", {})
                    saved_branch = next(
                        (
                            name for name, branch_id in stable_branch_ids.items()
                            if str(branch_id) == saved_branch_id
                        ),
                        next(
                            (
                                name
                                for name, branch_id in self.config.get("branches", {}).items()
                                if str(branch_id) == saved_branch_id
                            ),
                            saved_branch,
                        ),
                    )
                if saved_branch:
                    self.branch_var.set(saved_branch)
                if "day_type" in config:
                    self.day_type_var.set(config["day_type"])
                    
                self._update_theme_options()
                
                if "theme" in config or config.get("selected_theme_id"):
                    theme_val = config.get("theme", "")
                    saved_theme_id = str(config.get("selected_theme_id", ""))
                    if saved_theme_id:
                        branch_id = self.config.get("branches", {}).get(self.branch_var.get(), "")
                        theme_val = next(
                            (
                                name
                                for name in self.theme_dropdown.cget("values")
                                if self._theme_id_for_name(branch_id, name) == saved_theme_id
                            ),
                            theme_val,
                        )
                    if not self.current_site.startswith("제로월드"):
                        theme_val = JIGUBYEOL_THEME_ALIASES.get(theme_val, theme_val)
                    if theme_val in self.theme_dropdown.cget("values"):
                        self.theme_var.set(theme_val)
                    
                if "custom_theme" in config:
                    if config["custom_theme"]:
                        self.custom_theme_checkbox.select()
                        self.theme_dropdown.configure(state="disabled")
                        self.theme_pk_entry.pack(fill="x", after=self.checkbox_container, pady=(2, 0))
                    else:
                        self.custom_theme_checkbox.deselect()
                        self.theme_dropdown.configure(state="normal")
                        self.theme_pk_entry.pack_forget()
                        
                if "theme_pk" in config:
                    self.theme_pk_entry.delete(0, "end")
                    self.theme_pk_entry.insert(0, config["theme_pk"])
            else:
                self._update_theme_options()
                
            if "date" in config:
                self.date_entry.delete(0, "end")
                self.date_entry.insert(0, config["date"])
                
            if "time" in config:
                self.time_entry.delete(0, "end")
                self.time_entry.insert(0, config["time"])
                
            name = stored_name or config.get("name", "")
            phone = stored_phone or config.get("phone", "")
            if remember_personal and name:
                self.name_entry.delete(0, "end")
                self.name_entry.insert(0, name)
                if not stored_name:
                    self.secret_store.set("reservation_name", name)

            if remember_personal and phone:
                self.phone_entry.delete(0, "end")
                self.phone_entry.insert(0, phone)
                if not stored_phone:
                    self.secret_store.set("reservation_phone", phone)

            if "name" in config or "phone" in config:
                config.pop("name", None)
                config.pop("phone", None)
                config["site"] = saved_site
                config_migrated = True
                
            if "people" in config:
                self.people_entry.delete(0, "end")
                self.people_entry.insert(0, config["people"])
                
            # Parse memory threads first
            if "threads" in config:
                self.standard_threads = max(1, min(int(config["threads"]), 50))
            if "naver_threads" in config:
                self.naver_threads = max(1, min(int(config["naver_threads"]), 8))

            if "engine_mode" in config:
                mode_val = LEGACY_MODE_MAP.get(config["engine_mode"], config["engine_mode"])
                self.engine_mode_btn.set(mode_val)
                self._on_mode_change(mode_val)
            elif "is_async" in config:
                val = STANDARD_MODE
                self.engine_mode_btn.set(val)
                self._on_mode_change(val)

            if "show_server_time" in config:
                if config["show_server_time"]:
                    self.show_server_time_checkbox.select()
                else:
                    self.show_server_time_checkbox.deselect()
            else:
                self.show_server_time_checkbox.deselect()
            self.catalog_auto_refresh_var.set(bool(config.get("catalog_auto_refresh", True)))
            if saved_site == self.current_site:
                if not config.get("selected_branch_id"):
                    selected_branch_id = self._selected_branch_id()
                    if selected_branch_id:
                        config["selected_branch_id"] = selected_branch_id
                        config_migrated = True
                if not config.get("selected_theme_id"):
                    selected_theme_id = self._selected_theme_id()
                    if selected_theme_id:
                        config["selected_theme_id"] = selected_theme_id
                        config_migrated = True
            if config_migrated:
                config["site"] = saved_site
                save_json("config.json", config)
        except Exception:
            pass
        finally:
            self._is_initializing = False

    def save_config(self, site_name):
        try:
            # Sync active memory thread counts before saving
            if self.engine_mode_btn.get() == NAVER_MODE:
                self.naver_threads = int(self.threads_slider.get())
            else:
                self.standard_threads = int(self.threads_slider.get())

            remember_personal = bool(self.remember_personal_var.get())
            if remember_personal:
                self.secret_store.set("reservation_name", self.name_entry.get().strip())
                self.secret_store.set("reservation_phone", self.phone_entry.get().strip())
            else:
                self.secret_store.delete("reservation_name")
                self.secret_store.delete("reservation_phone")

            config = {
                "site": site_name,
                "branch": self.branch_var.get(),
                "day_type": self.day_type_var.get(),
                "theme": self.theme_var.get(),
                "custom_theme": bool(self.custom_theme_checkbox.get()),
                "theme_pk": self.theme_pk_entry.get().strip(),
                "date": self.date_entry.get().strip(),
                "time": self.time_entry.get().strip(),
                "people": self.people_entry.get().strip(),
                "threads": self.standard_threads,
                "naver_threads": self.naver_threads,
                "is_async": self.engine_mode_btn.get() == STANDARD_MODE,
                "engine_mode": self.engine_mode_btn.get(),
                "show_server_time": bool(self.show_server_time_checkbox.get()),
                "remember_personal_info": remember_personal,
                "catalog_auto_refresh": bool(self.catalog_auto_refresh_var.get()),
                "selected_branch_id": self._selected_branch_id(),
                "selected_theme_id": self._selected_theme_id(),
            }
            save_json("config.json", config)
        except (OSError, RuntimeError, ValueError) as exc:
            if hasattr(self.master, "log_panel"):
                self.master.log_panel.append_log(f"설정을 저장하지 못했습니다: {exc}", "warning")

    def _selected_theme_id(self):
        branch_id = self.config.get("branches", {}).get(self.branch_var.get(), "")
        return self._theme_id_for_name(branch_id, self.theme_var.get())

    def _selected_branch_id(self):
        branch_name = self.branch_var.get()
        return str(
            self.config.get("branch_ids", {}).get(
                branch_name,
                self.config.get("branches", {}).get(branch_name, ""),
            )
        )

    def _theme_id_for_name(self, branch_id, theme_name):
        stable_theme_id = self.config.get("theme_ids", {}).get(branch_id, {}).get(theme_name)
        if stable_theme_id is not None:
            return str(stable_theme_id)
        if self.current_site == "키이스케이프":
            value = KEYESCAPE_THEMES.get(branch_id, {}).get(theme_name, {})
            return str(value.get("info_num", "")) if isinstance(value, dict) else str(value)
        if self.current_site == "제로월드":
            return str(ZEROWORLD_THEMES.get(branch_id, {}).get(theme_name, ""))
        if self.current_site == "둠이스케이프":
            return str(DOOMESCAPE_THEMES.get(branch_id, {}).get(theme_name, ""))
        if self.current_site in self.custom_sites:
            return str(self.config.get("themes", {}).get(branch_id, {}).get(theme_name, ""))
        return str(JIGUBYEOL_THEMES.get(branch_id, {}).get(theme_name, ""))
