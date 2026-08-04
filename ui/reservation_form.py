import customtkinter as ctk
import ui.theme as theme
from data.themes import (
    ZEROWORLD_THEMES,
    JIGUBYEOL_THEMES, PHOBIADUNGEON_THEMES, SITES_CONFIG, JIGUBYEOL_THEME_ALIASES,
    KEYESCAPE_THEMES, DOOMESCAPE_THEMES
)
from engines.yescaptcha_client import YesCaptchaClient, DEFAULT_SOFT_ID
from pengucro.models import (
    LEGACY_MODE_MAP,
    NAVER_MODE,
    STANDARD_MODE,
    ReservationRequest,
    coerce_bool,
)
from pengucro.storage import SecretStore, load_json, save_json
from datetime import datetime, timedelta
import calendar

# Keys stored in config.json that no widget in this form owns. save_config()
# replaces the whole file, so these must survive the round trip.
PRESERVED_CONFIG_KEYS = (
    "force_scroll_repaint",
    "scroll_repaint_strong",
    "keyescape_use_real_chrome",
    "keyescape_close_chrome_on_exit",
    "keyescape_agree_all",
    "naver_use_real_chrome",
    "naver_close_chrome_on_exit",
    "naver_poll_interval",
    "naver_poll_burst_interval",
    "naver_poll_relax_after",
    "yescaptcha_enabled",
    "yescaptcha_test_mode",
    "yescaptcha_client_key",
    "yescaptcha_soft_id",
)


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
        header.pack(fill="x", padx=theme.SPACE_4, pady=(theme.SPACE_4, theme.SPACE_2))
        nav_style = {
            "width": theme.H_CONTROL,
            "height": theme.H_CONTROL,
            "corner_radius": theme.ROUNDED_SM,
            "fg_color": theme.CONTROL_COLOR,
            "hover_color": theme.CONTROL_HOVER,
            "border_width": 1,
            "border_color": theme.CONTROL_BORDER,
            "text_color": theme.TEXT_BODY,
        }
        ctk.CTkButton(header, text="‹", command=lambda: self._move(-1), **nav_style).pack(side="left")
        self.month_label = ctk.CTkLabel(
            header, font=theme.FONT_HEADING, text_color=theme.TEXT_PRIMARY
        )
        self.month_label.pack(side="left", expand=True)
        ctk.CTkButton(header, text="›", command=lambda: self._move(1), **nav_style).pack(side="right")

        self.days_frame = ctk.CTkFrame(self, fg_color="transparent")
        self.days_frame.pack(
            fill="both", expand=True, padx=theme.SPACE_4, pady=(0, theme.SPACE_4)
        )
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
            weekend = column >= 5
            ctk.CTkLabel(
                self.days_frame,
                text=label,
                font=theme.FONT_CAPTION,
                text_color=theme.TEXT_TERTIARY if weekend else theme.TEXT_MUTE,
            ).grid(row=0, column=column, padx=2, pady=(0, theme.SPACE_1), sticky="nsew")
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
                    font=theme.FONT_BODY_MD,
                    fg_color=theme.ELEVATED_COLOR,
                    hover_color=theme.ACCENT_BLUE,
                    text_color=theme.TEXT_PRIMARY,
                    text_color_disabled=theme.TEXT_DISABLED,
                    corner_radius=theme.ROUNDED_SM,
                    command=lambda chosen=value: self._choose(chosen),
                )
                if value < today:
                    button.configure(state="disabled")
                elif value == today:
                    # Mark today so the grid has an orientation point.
                    button.configure(border_width=1, border_color=theme.ACCENT_BLUE)
                button.grid(row=row, column=column, padx=2, pady=2, sticky="nsew")

    def _choose(self, value):
        self.on_select(value.isoformat())
        self.destroy()


class TimePickerDialog(ctk.CTkToplevel):
    """Time slot picker.

    Deliberately does *not* use CTkScrollableFrame for the common case. A
    booking day exposes roughly 6-20 slots, which fit in a compact grid, and
    CTkScrollableFrame embeds real child windows in a Tk canvas that scrolls
    with ``yscrollincrement=1`` -- a combination that leaves repaint artifacts
    (ghosting) on Windows. Laying the slots out as a fixed grid removes the
    scroll path entirely. Only an unusually long list falls back to the
    scrolling variant, and that one is the leak-free SafeScrollableFrame.
    """

    COLUMNS = 3
    MAX_GRID_ROWS = 8            # Beyond this the dialog scrolls instead
    ROW_HEIGHT = 38
    CHROME_HEIGHT = 132          # Title bar + status line + padding

    def __init__(self, parent, loader, on_select):
        super().__init__(parent)
        self.loader = loader
        self.on_select = on_select
        self._load_result = None
        self._list_host = None
        self.title("예약 시간 조회")
        self.geometry("360x220")
        self.minsize(340, 200)
        self.resizable(False, False)
        self.configure(fg_color=theme.CANVAS_COLOR)
        self.transient(parent.winfo_toplevel())
        self.grab_set()

        self.status = ctk.CTkLabel(
            self,
            text="예약 가능한 시간을 조회하고 있습니다...",
            font=theme.FONT_LABEL,
            text_color=theme.TEXT_MUTE,
            wraplength=310,
            justify="left",
        )
        self.status.pack(fill="x", padx=theme.SPACE_4, pady=(theme.SPACE_4, theme.SPACE_2))

        self.list_container = ctk.CTkFrame(
            self,
            fg_color=theme.SURFACE_COLOR,
            border_color=theme.HAIRLINE_COLOR,
            border_width=1,
            corner_radius=theme.ROUNDED_MD,
        )
        self.list_container.pack(
            fill="both", expand=True, padx=theme.SPACE_4, pady=(0, theme.SPACE_4)
        )

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
            self.status.configure(text=f"시간 조회 실패: {error}", text_color=theme.TINT_ERROR_FG)
            self._show_empty("조회에 실패했습니다.")
            return
        if not slots:
            self.status.configure(
                text="사이트가 아직 시간 버튼을 제공하지 않았습니다.",
                text_color=theme.ACCENT_YELLOW,
            )
            self._show_empty("표시할 시간이 없습니다.")
            return

        estimated = [slot for slot in slots if getattr(slot, "estimated", False)]
        available = [slot for slot in slots if slot.available]
        if estimated:
            source_date = getattr(estimated[0], "source_date", "")
            source_text = f"{source_date} 같은 요일" if source_date else "같은 요일의 최근"
            self.status.configure(
                text=(
                    f"아직 닫힌 날짜 · {source_text} 시간표 {len(slots)}개를 표시합니다. "
                    "시간은 선택할 수 있으며 오픈 후 실제 상태를 다시 확인합니다."
                ),
                text_color=theme.ACCENT_YELLOW,
            )
        else:
            self.status.configure(
                text=(
                    f"예약 가능 {len(available)}개 · 마감/미오픈 {len(slots) - len(available)}개 "
                    f"(전체 {len(slots)}개) · 마감은 ✕ 표시"
                ),
                text_color=theme.TINT_SUCCESS_FG if available else theme.ACCENT_YELLOW,
            )

        rows = (len(slots) + self.COLUMNS - 1) // self.COLUMNS
        host = self._build_host(rows)
        for column in range(self.COLUMNS):
            host.columnconfigure(column, weight=1, uniform="slot")

        for index, slot in enumerate(slots):
            row, column = divmod(index, self.COLUMNS)
            # A glyph suffix carries the state as well as the colour does, so
            # availability is not conveyed by colour alone.
            selectable = getattr(slot, "selectable", slot.available)
            label = f"{slot.time} ◇" if getattr(slot, "estimated", False) else (
                slot.time if slot.available else f"{slot.time} ✕"
            )
            button = ctk.CTkButton(
                host,
                text=label,
                font=theme.FONT_BODY_MD,
                state="normal" if selectable else "disabled",
                fg_color=theme.ELEVATED_COLOR if selectable else theme.SURFACE_COLOR,
                hover_color=theme.ACCENT_BLUE,
                border_width=1,
                border_color=theme.CONTROL_BORDER if selectable else theme.HAIRLINE_COLOR,
                text_color=theme.TEXT_PRIMARY if selectable else theme.TEXT_DISABLED,
                text_color_disabled=theme.TEXT_DISABLED,
                corner_radius=theme.ROUNDED_SM,
                command=lambda value=slot.time: self._choose(value),
                height=theme.H_CONTROL,
            )
            button.grid(
                row=row,
                column=column,
                sticky="ew",
                padx=theme.SPACE_1,
                pady=theme.SPACE_1,
            )

        self._fit_to_rows(min(rows, self.MAX_GRID_ROWS))

    def _build_host(self, rows):
        """Plain frame for a normal list, scrolling frame only when required."""
        if self._list_host is not None:
            self._list_host.destroy()
        if rows <= self.MAX_GRID_ROWS:
            host = ctk.CTkFrame(self.list_container, fg_color="transparent")
        else:
            from ui.scrollable import SafeScrollableFrame

            host = SafeScrollableFrame(
                self.list_container,
                fg_color=theme.SURFACE_COLOR,
                border_width=0,
                corner_radius=0,
            )
            self.resizable(False, True)
        host.pack(fill="both", expand=True, padx=theme.SPACE_2, pady=theme.SPACE_2)
        self._list_host = host
        return host

    def _show_empty(self, message):
        host = self._build_host(1)
        ctk.CTkLabel(
            host,
            text=message,
            font=theme.FONT_LABEL,
            text_color=theme.TEXT_TERTIARY,
        ).pack(expand=True)
        self._fit_to_rows(1)

    def _fit_to_rows(self, rows):
        height = self.CHROME_HEIGHT + max(1, rows) * self.ROW_HEIGHT
        self.geometry(f"360x{min(height, 560)}")

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
        
        # Thread memory states.
        #
        # Naver defaults to 1: a single account can hold a single booking, so extra
        # workers only duplicate the same request from the same session. The engine
        # clamps anything higher anyway, and this keeps the slider honest about it.
        self.standard_threads = 30
        self.naver_threads = 1
        self.keyescape_threads = 1
        self.last_mode = STANDARD_MODE

        # Grid configuration for 2 columns
        self.columnconfigure((0, 1), weight=1, uniform="equal")

        # -------------------------------------------------------------
        # Row 0: Branch Selection / Day Type Selection (Dynamic)
        # -------------------------------------------------------------
        self.branch_frame = ctk.CTkFrame(self, fg_color="transparent")
        self.branch_label = ctk.CTkLabel(self.branch_frame, text="지점", font=theme.FONT_BODY_SM, text_color=theme.TEXT_MUTE)
        self.branch_label.pack(anchor="w", pady=(0, theme.LABEL_GAP))
        
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
            height=theme.H_CONTROL,
            anchor="w"
        )
        self.branch_dropdown.pack(fill="x")

        self.day_type_frame = ctk.CTkFrame(self, fg_color="transparent")
        self.day_type_label = ctk.CTkLabel(self.day_type_frame, text="요일 구분", font=theme.FONT_BODY_SM, text_color=theme.TEXT_MUTE)
        self.day_type_label.pack(anchor="w", pady=(0, theme.LABEL_GAP))
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
            height=theme.H_CONTROL
        )
        self.day_type_segmented.pack(fill="x")

        # -------------------------------------------------------------
        # Row 1: Theme Selection (Full Width OptionMenu)
        # -------------------------------------------------------------
        self.theme_frame = ctk.CTkFrame(self, fg_color="transparent")
        self.theme_frame.grid(row=1, column=0, columnspan=2, padx=theme.CARD_PAD, pady=theme.ROW_GAP, sticky="ew")
        
        self.theme_label = ctk.CTkLabel(self.theme_frame, text="테마 선택", font=theme.FONT_BODY_SM, text_color=theme.TEXT_MUTE)
        self.theme_label.pack(anchor="w", pady=(0, theme.LABEL_GAP))
        
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
            height=theme.H_CONTROL,
            anchor="w"
        )
        self.theme_dropdown.pack(fill="x")

        # -------------------------------------------------------------
        # Row 2: Custom Theme Entry (Full Width)
        # -------------------------------------------------------------
        self.custom_theme_frame = ctk.CTkFrame(self, fg_color="transparent")
        self.custom_theme_frame.grid(row=2, column=0, columnspan=2, padx=theme.CARD_PAD, pady=theme.ROW_GAP, sticky="ew")
        
        # Container to align checkboxes horizontally
        self.checkbox_container = ctk.CTkFrame(self.custom_theme_frame, fg_color="transparent")
        self.checkbox_container.pack(fill="x", anchor="w", pady=(0, theme.LABEL_GAP))

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
        self.custom_theme_checkbox.pack(side="left", padx=(0, theme.SPACE_4))

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
            height=theme.H_CONTROL
        )
        self.theme_pk_entry.pack(fill="x")
        self.theme_pk_entry.pack_forget()

        # -------------------------------------------------------------
        # Row 3: Date & Time (Split row)
        # -------------------------------------------------------------
        self.date_frame = ctk.CTkFrame(self, fg_color="transparent")
        self.date_frame.grid(row=3, column=0, padx=(theme.CARD_PAD, theme.SPACE_1), pady=theme.ROW_GAP, sticky="ew")
        self.date_label = ctk.CTkLabel(self.date_frame, text="날짜 (YYYY-MM-DD)", font=theme.FONT_BODY_SM, text_color=theme.TEXT_MUTE)
        self.date_label.pack(anchor="w", pady=(0, theme.LABEL_GAP))
        
        tomorrow = (datetime.now() + timedelta(days=1)).strftime("%Y-%m-%d")
        self.date_entry = ctk.CTkEntry(
            self.date_frame,
            placeholder_text="예: 2026-06-01",
            fg_color=theme.ELEVATED_COLOR,
            border_color=theme.HAIRLINE_COLOR,
            text_color=theme.TEXT_PRIMARY,
            placeholder_text_color=theme.TEXT_DISABLED,
            corner_radius=theme.ROUNDED_MD,
            height=theme.H_CONTROL
        )
        self.date_entry.insert(0, tomorrow)
        self.date_picker_btn = ctk.CTkButton(
            self.date_frame,
            text="📅",
            width=34,
            height=theme.H_CONTROL,
            fg_color=theme.ELEVATED_COLOR,
            hover_color=theme.CARD_COLOR,
            command=self._open_date_picker,
        )
        self.date_picker_btn.pack(side="right", padx=(4, 0))
        self.date_entry.pack(side="left", fill="x", expand=True)
        self.date_entry.bind("<KeyRelease>", self._format_date)
        self.date_entry.bind("<FocusOut>", self._on_date_change)

        self.time_frame = ctk.CTkFrame(self, fg_color="transparent")
        self.time_frame.grid(row=3, column=1, padx=(theme.SPACE_1, theme.CARD_PAD), pady=theme.ROW_GAP, sticky="ew")
        self.time_label = ctk.CTkLabel(self.time_frame, text="시간 (HH:MM)", font=theme.FONT_BODY_SM, text_color=theme.TEXT_MUTE)
        self.time_label.pack(anchor="w", pady=(0, theme.LABEL_GAP))
        
        self.time_entry = ctk.CTkEntry(
            self.time_frame,
            placeholder_text="예: 14:00",
            fg_color=theme.ELEVATED_COLOR,
            border_color=theme.HAIRLINE_COLOR,
            text_color=theme.TEXT_PRIMARY,
            placeholder_text_color=theme.TEXT_DISABLED,
            corner_radius=theme.ROUNDED_MD,
            height=theme.H_CONTROL
        )
        self.time_picker_btn = ctk.CTkButton(
            self.time_frame,
            text="조회",
            width=42,
            height=theme.H_CONTROL,
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
        self.name_frame.grid(row=4, column=0, padx=(theme.CARD_PAD, theme.SPACE_1), pady=theme.ROW_GAP, sticky="ew")
        self.name_label = ctk.CTkLabel(self.name_frame, text="이름", font=theme.FONT_BODY_SM, text_color=theme.TEXT_MUTE)
        self.name_label.pack(anchor="w", pady=(0, theme.LABEL_GAP))
        
        self.name_entry = ctk.CTkEntry(
            self.name_frame,
            placeholder_text="예약자명",
            fg_color=theme.ELEVATED_COLOR,
            border_color=theme.HAIRLINE_COLOR,
            text_color=theme.TEXT_PRIMARY,
            placeholder_text_color=theme.TEXT_DISABLED,
            corner_radius=theme.ROUNDED_MD,
            height=theme.H_CONTROL
        )
        self.name_entry.pack(fill="x")

        self.people_frame = ctk.CTkFrame(self, fg_color="transparent")
        self.people_frame.grid(row=4, column=1, padx=(theme.SPACE_1, theme.CARD_PAD), pady=theme.ROW_GAP, sticky="ew")
        self.people_label = ctk.CTkLabel(self.people_frame, text="인원 수", font=theme.FONT_BODY_SM, text_color=theme.TEXT_MUTE)
        self.people_label.pack(anchor="w", pady=(0, theme.LABEL_GAP))
        
        self.people_entry = ctk.CTkEntry(
            self.people_frame,
            placeholder_text="2",
            fg_color=theme.ELEVATED_COLOR,
            border_color=theme.HAIRLINE_COLOR,
            text_color=theme.TEXT_PRIMARY,
            placeholder_text_color=theme.TEXT_DISABLED,
            corner_radius=theme.ROUNDED_MD,
            height=theme.H_CONTROL
        )
        self.people_entry.insert(0, "2")
        self.people_entry.pack(fill="x")

        # -------------------------------------------------------------
        # Row 5: Phone Number (Full Width)
        # -------------------------------------------------------------
        self.phone_frame = ctk.CTkFrame(self, fg_color="transparent")
        self.phone_frame.grid(row=5, column=0, columnspan=2, padx=theme.CARD_PAD, pady=theme.ROW_GAP, sticky="ew")
        self.phone_label = ctk.CTkLabel(self.phone_frame, text="전화번호", font=theme.FONT_BODY_SM, text_color=theme.TEXT_MUTE)
        self.phone_label.pack(anchor="w", pady=(0, theme.LABEL_GAP))
        
        self.phone_entry = ctk.CTkEntry(
            self.phone_frame,
            placeholder_text="예: 010-1234-5678",
            fg_color=theme.ELEVATED_COLOR,
            border_color=theme.HAIRLINE_COLOR,
            text_color=theme.TEXT_PRIMARY,
            placeholder_text_color=theme.TEXT_DISABLED,
            corner_radius=theme.ROUNDED_MD,
            height=theme.H_CONTROL
        )
        self.phone_entry.pack(fill="x")
        self.phone_entry.bind("<KeyRelease>", self._format_phone)

        # -------------------------------------------------------------
        # Advanced: concurrent attempts (shown below the advanced toggle)
        # -------------------------------------------------------------
        self.threads_frame = ctk.CTkFrame(self, fg_color="transparent")
        self.threads_frame.grid(row=8, column=0, columnspan=2, padx=theme.CARD_PAD, pady=(theme.ROW_GAP, theme.SPACE_2), sticky="ew")

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
        self.threads_slider.pack(side="right", fill="x", expand=True, padx=(theme.SPACE_3, 0))

        # -------------------------------------------------------------
        # Row 6: Booking Method
        # -------------------------------------------------------------
        self.engine_mode_frame = ctk.CTkFrame(self, fg_color="transparent")
        self.engine_mode_frame.grid(row=6, column=0, columnspan=2, padx=theme.CARD_PAD, pady=(theme.ROW_GAP, theme.SPACE_2), sticky="ew")

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
            height=theme.H_CONTROL,
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
            height=theme.H_GHOST,
            corner_radius=theme.ROUNDED_SM,
            command=self._toggle_advanced,
        )
        self.advanced_toggle_btn.grid(
            row=7,
            column=0,
            columnspan=2,
            padx=theme.CARD_PAD,
            pady=(0, theme.SPACE_1),
            sticky="ew",
        )

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
        self.remember_personal_checkbox.grid(row=0, column=0, sticky="w", pady=(0, theme.SPACE_2))

        # YesCaptcha Auto-Solver Frame inside Advanced Settings
        self.yescaptcha_frame = ctk.CTkFrame(
            self.advanced_frame,
            fg_color=theme.ELEVATED_COLOR,
            border_color=theme.HAIRLINE_COLOR,
            border_width=1,
            corner_radius=theme.ROUNDED_MD
        )
        self.yescaptcha_frame.grid(row=1, column=0, sticky="ew", pady=(0, theme.SPACE_2))

        self.yc_header_frame = ctk.CTkFrame(self.yescaptcha_frame, fg_color="transparent")
        self.yc_header_frame.pack(fill="x", padx=8, pady=(6, 4))

        self.yescaptcha_enabled_var = ctk.BooleanVar(value=False)
        self.yescaptcha_checkbox = ctk.CTkCheckBox(
            self.yc_header_frame,
            text="YesCaptcha 자동 해결 사용 (ON/OFF)",
            variable=self.yescaptcha_enabled_var,
            font=(theme.FONT_FAMILY, 11, "bold"),
            fg_color=theme.ACCENT_BLUE,
            hover_color=theme.ACCENT_BLUE,
            text_color=theme.TEXT_PRIMARY,
            checkbox_width=14,
            checkbox_height=14,
            corner_radius=theme.ROUNDED_SM,
            command=self._on_yescaptcha_toggle
        )
        self.yescaptcha_checkbox.pack(side="left", anchor="w")

        self.yescaptcha_balance_btn = ctk.CTkButton(
            self.yc_header_frame,
            text="잔액 확인",
            font=theme.FONT_BODY_SM,
            fg_color=theme.SURFACE_COLOR,
            hover_color=theme.CARD_COLOR,
            text_color=theme.TEXT_PRIMARY,
            height=22,
            width=64,
            corner_radius=theme.ROUNDED_SM,
            command=self._check_yescaptcha_balance
        )
        self.yescaptcha_balance_btn.pack(side="right")

        self.yc_inputs_frame = ctk.CTkFrame(self.yescaptcha_frame, fg_color="transparent")
        self.yc_inputs_frame.pack(fill="x", padx=8, pady=(0, 6))

        self.yc_key_label = ctk.CTkLabel(self.yc_inputs_frame, text="API Key:", font=theme.FONT_BODY_SM, text_color=theme.TEXT_MUTE)
        self.yc_key_label.pack(side="left", padx=(0, 4))

        self.yescaptcha_client_key_entry = ctk.CTkEntry(
            self.yc_inputs_frame,
            placeholder_text="YesCaptcha Client Key",
            fg_color=theme.SURFACE_COLOR,
            border_color=theme.HAIRLINE_COLOR,
            text_color=theme.TEXT_PRIMARY,
            placeholder_text_color=theme.TEXT_DISABLED,
            corner_radius=theme.ROUNDED_SM,
            height=24
        )
        self.yescaptcha_client_key_entry.pack(side="left", fill="x", expand=True, padx=(0, 6))

        self.yc_soft_label = ctk.CTkLabel(self.yc_inputs_frame, text="SoftID:", font=theme.FONT_BODY_SM, text_color=theme.TEXT_MUTE)
        self.yc_soft_label.pack(side="left", padx=(0, 4))

        self.yescaptcha_soft_id_entry = ctk.CTkEntry(
            self.yc_inputs_frame,
            placeholder_text="SoftID",
            fg_color=theme.SURFACE_COLOR,
            border_color=theme.HAIRLINE_COLOR,
            text_color=theme.TEXT_PRIMARY,
            placeholder_text_color=theme.TEXT_DISABLED,
            corner_radius=theme.ROUNDED_SM,
            height=24,
            width=60
        )
        self.yescaptcha_soft_id_entry.insert(0, DEFAULT_SOFT_ID)
        self.yescaptcha_soft_id_entry.pack(side="left")

        self.yescaptcha_test_mode_var = ctk.BooleanVar(value=False)
        self.yescaptcha_test_mode_checkbox = ctk.CTkCheckBox(
            self.yescaptcha_frame,
            text="즉시 테스트 모드 (시작 즉시 1회 검증 · 포인트 사용)",
            variable=self.yescaptcha_test_mode_var,
            font=theme.FONT_BODY_SM,
            fg_color=theme.ACCENT_BLUE,
            hover_color=theme.ACCENT_BLUE,
            text_color=theme.TEXT_DISABLED,
            checkbox_width=14,
            checkbox_height=14,
            corner_radius=theme.ROUNDED_SM,
            state="disabled",
            command=self._on_yescaptcha_test_mode_toggle,
        )
        self.yescaptcha_test_mode_checkbox.pack(
            fill="x", padx=8, pady=(0, 6), anchor="w"
        )

        self._setup_entry_focus(self.yescaptcha_client_key_entry)
        self._setup_entry_focus(self.yescaptcha_soft_id_entry)

        self.npay_auto_pay_var = ctk.BooleanVar(value=False)
        self.npay_auto_pay_checkbox = ctk.CTkCheckBox(
            self.advanced_frame,
            text="Npay 머니 자동결제 (실제 결제)",
            variable=self.npay_auto_pay_var,
            font=theme.FONT_BODY_SM,
            fg_color=theme.ACCENT_BLUE,
            hover_color=theme.ACCENT_BLUE,
            text_color=theme.TEXT_MUTE,
            checkbox_width=14,
            checkbox_height=14,
            corner_radius=theme.ROUNDED_SM,
            command=self.auto_save,
        )

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
        self.catalog_auto_refresh_checkbox.grid(
            row=2, column=0, sticky="w", pady=(theme.SPACE_2, theme.SPACE_2)
        )

        self.catalog_refresh_frame = ctk.CTkFrame(self.advanced_frame, fg_color="transparent")
        self.catalog_refresh_frame.grid(row=3, column=0, sticky="ew")
        self.catalog_refresh_frame.columnconfigure(1, weight=1)
        self.catalog_refresh_btn = ctk.CTkButton(
            self.catalog_refresh_frame,
            text="현재 사이트 갱신",
            width=118,
            height=theme.H_CONTROL,
            font=theme.FONT_BODY_SM,
            fg_color=theme.ELEVATED_COLOR,
            hover_color=theme.CARD_COLOR,
            command=self._request_catalog_refresh,
        )
        self.catalog_refresh_btn.grid(row=0, column=0, sticky="w")
        # This label carries real information ("최근 2026-07-26 ..."), so it must
        # not use TEXT_DISABLED, which is only ~1.9:1 against the card and was
        # effectively unreadable.
        self.catalog_refresh_status = ctk.CTkLabel(
            self.catalog_refresh_frame,
            text="갱신 기록 없음",
            font=theme.FONT_LABEL,
            text_color=theme.TEXT_TERTIARY,
            anchor="e",
        )
        self.catalog_refresh_status.grid(row=0, column=1, sticky="e", padx=(theme.SPACE_2, 0))
        self.catalog_change_badge = ctk.CTkLabel(
            self.catalog_refresh_frame,
            text="",
            width=0,
            font=(theme.FONT_FAMILY, 10, "bold"),
            text_color=theme.ACCENT_YELLOW,
            cursor="hand2",
        )
        self.catalog_change_badge.grid(row=0, column=2, sticky="e", padx=(theme.SPACE_2, 0))
        self.catalog_change_badge.bind("<Button-1>", lambda _event: self._show_catalog_pending())

        # Developer test mode lives inside 고급 설정.
        #
        # It used to be gridded onto the form itself at row 10 and shown only in
        # Naver mode, which put it below the advanced panel and outside it -- so it
        # read as a stray checkbox and was invisible for Keyescape, whose engine
        # supports the same flag.
        # One line, not a checkbox plus a hint label: the advanced panel expands by
        # stealing height from the log panel, and the log panel has a floor. A
        # second line pushed past it, so the panel would have been clipped.
        self.dev_mode_frame = ctk.CTkFrame(self.advanced_frame, fg_color="transparent")
        self.dev_mode_frame.grid(row=4, column=0, sticky="ew", pady=(theme.SPACE_2, 0))
        self.dev_mode_frame.columnconfigure(0, weight=1)

        self.dev_mode_var = ctk.BooleanVar(value=False)
        self.dev_mode_checkbox = ctk.CTkCheckBox(
            self.dev_mode_frame,
            text=self.DEV_MODE_TEXT_ON,
            variable=self.dev_mode_var,
            font=theme.FONT_BODY_SM,
            fg_color=theme.ACCENT_BLUE,
            hover_color=theme.ACCENT_BLUE,
            text_color=theme.TEXT_MUTE,
            checkbox_width=14,
            checkbox_height=14,
            corner_radius=theme.ROUNDED_SM,
            command=self.auto_save,
        )
        self.dev_mode_checkbox.grid(row=0, column=0, sticky="w")
        self._advanced_visible = False

        # Setup focus effects for entries
        self._setup_entry_focus(self.theme_pk_entry)
        self._setup_entry_focus(self.date_entry)
        self._setup_entry_focus(self.time_entry)
        self._setup_entry_focus(self.name_entry)
        self._setup_entry_focus(self.people_entry)
        self._setup_entry_focus(self.phone_entry)

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
        reservation_date = self.date_entry.get().strip()
        is_naver = self.engine_mode_btn.get() == NAVER_MODE
        lookup_config = self.config
        if is_naver:
            branch_id = "1"
            theme_id = self.config.get("themes", {}).get("1", {}).get(
                self.theme_var.get(), ""
            )
            if not theme_id or theme_id == "naver":
                theme_id = self.config.get("url", "")
            # Naver's fetcher needs the selected item URL (/items/{id}), while a
            # multi-theme site's root URL only contains the business id.
            lookup_config = dict(self.config)
            lookup_config["url"] = theme_id
        else:
            branch_id = self.config.get("branches", {}).get(self.branch_var.get(), "")
            theme_id = self._theme_id_for_name(branch_id, self.theme_var.get())

        if not branch_id or not theme_id or len(reservation_date) != 10:
            from tkinter import messagebox

            required = "테마, 날짜" if is_naver else "지점, 테마, 날짜"
            messagebox.showwarning(
                "시간 조회", f"{required}를 먼저 선택해주세요.", parent=self
            )
            return

        def loader():
            from engines.time_slot_fetchers import fetch_any_time_slots

            return fetch_any_time_slots(
                lookup_config, branch_id, theme_id, reservation_date
            )

        TimePickerDialog(self, loader, self._set_selected_time)

    def _set_selected_time(self, value):
        self.time_entry.delete(0, "end")
        self.time_entry.insert(0, value)
        self.auto_save()

    def _toggle_advanced(self):
        self._advanced_visible = not self._advanced_visible
        if self._advanced_visible:
            self.advanced_frame.grid(
                row=9,
                column=0,
                columnspan=2,
                padx=theme.CARD_PAD,
                pady=(0, theme.SPACE_2),
                sticky="ew",
            )
            self.advanced_toggle_btn.configure(text="고급 설정  ▴")
        else:
            self.advanced_frame.grid_forget()
            self.advanced_toggle_btn.configure(text="고급 설정  ▾")
        self._update_widgets_state()

    def _save_secret_settings(self, event=None):
        # Retained as a no-op hook: the third-party captcha key it used to store
        # was removed together with the automatic solving path.
        try:
            self.secret_store.delete("yescaptcha_api_key")
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

    def _site_uses_keyescape(self, site_name=None) -> bool:
        """Return whether a standard-mode site is backed by KeyescapeEngine."""
        site_name = self.current_site if site_name is None else site_name
        if site_name == "키이스케이프":
            return True
        site = self.custom_sites.get(site_name) or {}
        return (site.get("engine_id") or site.get("style")) == "keyescape"

    def _keyescape_ui_active(self) -> bool:
        return (
            self.engine_mode_btn.get() != NAVER_MODE
            and self._site_uses_keyescape()
        )

    def _remember_active_thread_value(self) -> None:
        """Save the slider under the policy that owned it before a mode change."""
        value = int(self.threads_slider.get())
        if self.last_mode == NAVER_MODE:
            self.naver_threads = 1
        elif self._site_uses_keyescape():
            self.keyescape_threads = max(1, min(value, 3))
        else:
            self.standard_threads = max(1, min(value, 50))

    def _apply_thread_policy(self) -> None:
        """Fully reset the shared slider for the active engine family.

        Every branch writes range, step count, state, value and labels. This is
        intentionally idempotent: site and mode callbacks can arrive in either
        order without leaking Keyescape's cap into standard sites or unlocking
        Naver's fixed single worker.
        """
        if self.engine_mode_btn.get() == NAVER_MODE:
            self.naver_threads = 1
            self.threads_slider.configure(
                from_=1, to=8, number_of_steps=7, state="disabled"
            )
            self.threads_slider.set(1)
            self.threads_value_label.configure(
                text="1", text_color=theme.TEXT_DISABLED
            )
            self.threads_title_label.configure(
                text="동시 시도 수 (네이버는 1개 고정)",
                text_color=theme.TEXT_DISABLED,
            )
            return

        if self._site_uses_keyescape():
            self.keyescape_threads = max(1, min(self.keyescape_threads, 3))
            self.threads_slider.configure(
                from_=1, to=3, number_of_steps=2, state="normal"
            )
            self.threads_slider.set(self.keyescape_threads)
            self.threads_value_label.configure(
                text=str(self.keyescape_threads), text_color=theme.ACCENT_BLUE
            )
            self.threads_title_label.configure(
                text="동시 시도 페이지 (최대 3)", text_color=theme.TEXT_MUTE
            )
            return

        self.standard_threads = max(1, min(self.standard_threads, 50))
        self.threads_slider.configure(
            from_=1, to=50, number_of_steps=49, state="normal"
        )
        self.threads_slider.set(self.standard_threads)
        self.threads_value_label.configure(
            text=str(self.standard_threads), text_color=theme.ACCENT_BLUE
        )
        self.threads_title_label.configure(
            text="동시 시도 수", text_color=theme.TEXT_MUTE
        )

    def _on_mode_change(self, mode):
        if not getattr(self, "_is_initializing", False):
            self._remember_active_thread_value()

        self.last_mode = mode
        if mode == NAVER_MODE:
            self.naver_threads = 1

        # MainWindow selects the remembered site for the new mode. set_site()
        # applies the same policy during that callback; the final call below is
        # deliberate and makes standalone ReservationForm use correct as well.
        if self.mode_callback:
            self.mode_callback(mode)
        self._update_widgets_state()

    # Engines that actually honour reservation_data["devMode"]: they drive a real
    # browser, so stopping short of the final click leaves something to inspect.
    # The HTTP engines post a form and have no such halfway point.
    DEV_MODE_ENGINE_IDS = ("naver", "keyescape")
    DEV_MODE_TEXT_ON = "개발자 테스트 (Npay는 임시 예약 후 결제 직전 정지)"
    DEV_MODE_TEXT_OFF = "개발자 테스트 모드 (네이버·키이스케이프 전용)"

    def _dev_mode_supported(self) -> bool:
        """Only the browser-driven engines have a halfway point to stop at."""
        if self.engine_mode_btn.get() == NAVER_MODE:
            return True
        if self.current_site == "키이스케이프":
            return True
        site = self.custom_sites.get(self.current_site) or {}
        engine_id = site.get("engine_id") or site.get("style")
        return engine_id in self.DEV_MODE_ENGINE_IDS

    def developer_mode_enabled(self) -> bool:
        """Return the checkbox's visible state, which is authoritative."""
        if not self._dev_mode_supported():
            return False
        return bool(self.dev_mode_checkbox.get())

    def npay_auto_pay_enabled(self) -> bool:
        """Final Npay payment is opt-in and only meaningful in Naver mode."""
        if self.engine_mode_btn.get() != NAVER_MODE:
            return False
        return bool(self.npay_auto_pay_checkbox.get())

    def _update_dev_mode_state(self) -> None:
        if self._dev_mode_supported():
            self.dev_mode_checkbox.configure(
                state="normal", text=self.DEV_MODE_TEXT_ON, text_color=theme.TEXT_MUTE
            )
            return
        # The row stays in place so the panel does not jump, but the flag is
        # cleared: a stale checkmark would silently suppress a real booking on a
        # site whose engine ignores it.
        if self.dev_mode_checkbox.get():
            self.dev_mode_checkbox.deselect()
        self.dev_mode_checkbox.configure(
            state="disabled", text=self.DEV_MODE_TEXT_OFF,
            text_color=theme.TEXT_DISABLED,
        )

    def _update_widgets_state(self):
        if getattr(self, "_booking_running", False):
            return
        is_naver = (self.engine_mode_btn.get() == NAVER_MODE)
        keyescape_active = self._keyescape_ui_active()
        yescaptcha_on = (
            keyescape_active and bool(self.yescaptcha_enabled_var.get())
        )
        self.yescaptcha_test_mode_checkbox.configure(
            state="normal" if yescaptcha_on else "disabled",
            text_color=theme.TEXT_MUTE if yescaptcha_on else theme.TEXT_DISABLED,
        )

        if keyescape_active:
            self.yescaptcha_frame.grid(
                row=1, column=0, sticky="ew", pady=(0, theme.SPACE_2)
            )
        else:
            self.yescaptcha_frame.grid_forget()
        
        if getattr(self, "_advanced_visible", False):
            self.threads_frame.grid(row=8, column=0, columnspan=2, padx=theme.CARD_PAD, pady=(theme.ROW_GAP, theme.SPACE_2), sticky="ew")
        else:
            self.threads_frame.grid_forget()
        self._apply_thread_policy()

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

            self.engine_mode_frame.grid(row=6, column=0, columnspan=2, padx=theme.CARD_PAD, pady=theme.ROW_GAP, sticky="ew")
            self.npay_auto_pay_checkbox.grid(row=1, column=0, sticky="w")
            self.npay_auto_pay_checkbox.configure(
                state="normal", text_color=theme.TEXT_MUTE
            )
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
            
            self._toggle_custom_theme()
            self.engine_mode_frame.grid(row=6, column=0, columnspan=2, padx=theme.CARD_PAD, pady=(theme.ROW_GAP, theme.SPACE_2), sticky="ew")
            self.npay_auto_pay_checkbox.grid_forget()

        self._update_dev_mode_state()

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
            self.npay_auto_pay_checkbox,
            self.advanced_toggle_btn,
            self.remember_personal_checkbox,
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
                self.theme_frame.grid(row=1, column=0, columnspan=2, padx=theme.CARD_PAD, pady=theme.ROW_GAP, sticky="ew")
            else:
                self.theme_frame.grid_forget()
        else:
            # Keep theme and custom theme frames always mapped in grid to prevent vertical jumping
            self.theme_frame.grid(row=1, column=0, columnspan=2, padx=theme.CARD_PAD, pady=theme.ROW_GAP, sticky="ew")
            self.custom_theme_frame.grid(row=2, column=0, columnspan=2, padx=theme.CARD_PAD, pady=theme.ROW_GAP, sticky="ew")

            if has_weekday_weekend:
                # Show both branch and day type selection side by side
                self.branch_frame.grid(
                    row=0,
                    column=0,
                    padx=(theme.CARD_PAD, theme.SPACE_1),
                    pady=(theme.SPACE_2, theme.ROW_GAP),
                    sticky="ew",
                )
                self.day_type_frame.grid(
                    row=0,
                    column=1,
                    padx=(theme.SPACE_1, theme.CARD_PAD),
                    pady=(theme.SPACE_2, theme.ROW_GAP),
                    sticky="ew",
                )
                branch_options = list(self.config["branches"].keys())
                self.branch_dropdown.configure(values=branch_options)
                if branch_options:
                    prev_val = self.branch_var.get()
                    if prev_val in branch_options:
                        self.branch_var.set(prev_val)
                    else:
                        self.branch_var.set(branch_options[0])
            else:
                self.branch_frame.grid(
                    row=0,
                    column=0,
                    columnspan=2,
                    padx=theme.CARD_PAD,
                    pady=(theme.SPACE_2, theme.ROW_GAP),
                    sticky="ew",
                )
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

    def _on_yescaptcha_toggle(self):
        if not self.yescaptcha_enabled_var.get():
            self.yescaptcha_test_mode_var.set(False)
        self._update_widgets_state()
        self.auto_save()

    def _on_yescaptcha_test_mode_toggle(self):
        if self.yescaptcha_test_mode_var.get() and not self.yescaptcha_enabled_var.get():
            self.yescaptcha_test_mode_var.set(False)
        self.auto_save()

    def _check_yescaptcha_balance(self):
        from tkinter import messagebox
        client_key = self.yescaptcha_client_key_entry.get().strip()
        soft_id = self.yescaptcha_soft_id_entry.get().strip() or DEFAULT_SOFT_ID
        if not client_key:
            messagebox.showwarning("YesCaptcha 경고", "YesCaptcha Client Key (API Key)를 입력해 주세요.")
            return

        client = YesCaptchaClient(client_key, soft_id)
        ok, balance, msg = client.get_balance()
        
        main_win = self.winfo_toplevel()
        if hasattr(main_win, "log_panel") and hasattr(main_win.log_panel, "append_log"):
            main_win.log_panel.append_log(f"[YesCaptcha] {msg}", "success" if ok else "error")

        if ok:
            messagebox.showinfo("YesCaptcha 잔액 확인", f"조회 성공!\n현재 보유 잔액/포인트: {int(balance):,} P")
        else:
            messagebox.showerror("YesCaptcha 조회 실패", f"잔액 조회 실패:\n{msg}")

    def _on_threads_slider_move(self, value):
        val = int(value)
        if self.engine_mode_btn.get() == NAVER_MODE:
            self.naver_threads = 1
            self.threads_slider.set(1)
            self.threads_value_label.configure(text="1")
            return
        self.threads_value_label.configure(text=str(val))
        if self._site_uses_keyescape():
            self.keyescape_threads = max(1, min(val, 3))
        else:
            self.standard_threads = max(1, min(val, 50))
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

        keyescape_active = self._keyescape_ui_active()
        yescaptcha_enabled = (
            keyescape_active and bool(self.yescaptcha_enabled_var.get())
        )
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
            # Read the checkbox itself, not only its backing Tk variable. This
            # prevents a stale variable value from suppressing a real booking
            # after the user visibly turned developer mode off.
            "devMode": self.developer_mode_enabled(),
            "npayAutoPay": self.npay_auto_pay_enabled(),
            "site_url": self.config.get("url", ""),
            "yescaptcha_enabled": yescaptcha_enabled,
            "yescaptcha_test_mode": (
                yescaptcha_enabled and bool(self.yescaptcha_test_mode_var.get())
            ),
            "yescaptcha_client_key": (
                self.yescaptcha_client_key_entry.get().strip()
                if keyescape_active else ""
            ),
            "yescaptcha_soft_id": self.yescaptcha_soft_id_entry.get().strip() or DEFAULT_SOFT_ID,
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

        if is_naver:
            threads = 1
        elif keyescape_active:
            threads = int(self.threads_slider.get())
            threads = max(1, min(threads, 3))
        else:
            threads = max(1, min(int(self.threads_slider.get()), 50))
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
        stored_name = self.secret_store.get("reservation_name")
        stored_phone = self.secret_store.get("reservation_phone")
        # Drop any captcha key left over from an earlier version so the secret
        # store does not keep a credential nothing uses.
        try:
            if self.secret_store.get("yescaptcha_api_key"):
                self.secret_store.delete("yescaptcha_api_key")
        except RuntimeError:
            pass
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
                # NaverEngine always owns exactly one browser worker.
                self.naver_threads = 1
            if "keyescape_threads" in config:
                self.keyescape_threads = max(
                    1, min(int(config["keyescape_threads"]), 3)
                )

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

            if "yescaptcha_enabled" in config:
                if coerce_bool(config["yescaptcha_enabled"]):
                    self.yescaptcha_checkbox.select()
                else:
                    self.yescaptcha_checkbox.deselect()
            if (
                coerce_bool(config.get("yescaptcha_enabled", False))
                and coerce_bool(config.get("yescaptcha_test_mode", False))
            ):
                self.yescaptcha_test_mode_checkbox.select()
            else:
                self.yescaptcha_test_mode_checkbox.deselect()
            yescaptcha_on = bool(self.yescaptcha_enabled_var.get())
            self.yescaptcha_test_mode_checkbox.configure(
                state="normal" if yescaptcha_on else "disabled",
                text_color=theme.TEXT_MUTE if yescaptcha_on else theme.TEXT_DISABLED,
            )
            if "yescaptcha_client_key" in config:
                self.yescaptcha_client_key_entry.delete(0, "end")
                self.yescaptcha_client_key_entry.insert(0, config["yescaptcha_client_key"])
            if "yescaptcha_soft_id" in config:
                self.yescaptcha_soft_id_entry.delete(0, "end")
                self.yescaptcha_soft_id_entry.insert(0, config["yescaptcha_soft_id"])

            self.catalog_auto_refresh_var.set(bool(config.get("catalog_auto_refresh", True)))
            self.npay_auto_pay_var.set(bool(config.get("naver_npay_auto_pay", False)))
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
                self.naver_threads = 1
            elif self._site_uses_keyescape():
                self.keyescape_threads = max(
                    1, min(int(self.threads_slider.get()), 3)
                )
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
                "keyescape_threads": self.keyescape_threads,
                "is_async": self.engine_mode_btn.get() == STANDARD_MODE,
                "engine_mode": self.engine_mode_btn.get(),
                "show_server_time": bool(self.show_server_time_checkbox.get()),
                "remember_personal_info": remember_personal,
                "catalog_auto_refresh": bool(self.catalog_auto_refresh_var.get()),
                "naver_npay_auto_pay": bool(self.npay_auto_pay_var.get()),
                "selected_branch_id": self._selected_branch_id(),
                "selected_theme_id": self._selected_theme_id(),
                "yescaptcha_enabled": bool(self.yescaptcha_enabled_var.get()),
                "yescaptcha_test_mode": bool(self.yescaptcha_test_mode_var.get()),
                "yescaptcha_client_key": self.yescaptcha_client_key_entry.get().strip(),
                "yescaptcha_soft_id": self.yescaptcha_soft_id_entry.get().strip(),
            }
            # save_config rewrites config.json wholesale, so settings that are
            # not surfaced in this form (currently the scroll repaint switch)
            # have to be carried over or they would be silently reset on the
            # next auto-save.
            existing = load_json("config.json", {})
            if isinstance(existing, dict):
                for key in PRESERVED_CONFIG_KEYS:
                    if key in existing and key not in config:
                        config[key] = existing[key]
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
