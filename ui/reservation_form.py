import customtkinter as ctk
from PIL import Image, ImageDraw
import ui.theme as theme
from data.themes import (
    ZEROWORLD_THEMES,
    JIGUBYEOL_THEMES, PHOBIADUNGEON_THEMES, SITES_CONFIG, JIGUBYEOL_THEME_ALIASES,
    KEYESCAPE_THEMES, DOOMESCAPE_THEMES
)
from engines.yescaptcha_client import YesCaptchaClient, DEFAULT_SOFT_ID
from engines.dpsnnn_engine import DPSNNN_DEFAULT_WORKERS, DPSNNN_MAX_WORKERS
from engines.cgv_client import (
    CGV_DEFAULT_WORKERS,
    CGV_MANUAL_SITE_VALUE,
    CGV_MAX_WORKERS,
    parse_seat_groups,
    schedule_items,
)
from pengucro.models import (
    LEGACY_MODE_MAP,
    NAVER_MODE,
    STANDARD_MODE,
    TRIPCOM_MODE,
    ReservationRequest,
    coerce_bool,
    parse_bool_flag,
)
from pengucro.storage import SecretStore, load_json, update_json
from datetime import datetime, timedelta
import calendar

STANDARD_MAX_WORKERS = 50
ZEROWORLD_JIGUBYEOL_MAX_WORKERS = 32
DOOMESCAPE_MAX_WORKERS = 10
YESCAPTCHA_SECRET_KEY = "yescaptcha_api_key"

def _merge_form_config(
    existing: object,
    form_values: dict,
    form_baseline: dict,
    *,
    plaintext_yescaptcha_key: str | None = None,
    plaintext_yescaptcha_expected: str | None = None,
    remove_plaintext_yescaptcha_key: str | None = None,
) -> dict:
    """Merge one form snapshot without reverting newer shared settings."""

    merged = dict(existing) if isinstance(existing, dict) else {}
    missing = object()
    for key, value in form_values.items():
        if key not in merged or value != form_baseline.get(key, missing):
            merged[key] = value
    if plaintext_yescaptcha_key is not None:
        # Kept only when DPAPI is unavailable so an existing key is never lost.
        current = str(merged.get("yescaptcha_client_key", "") or "").strip()
        if (
            plaintext_yescaptcha_expected is None
            or not current
            or current == plaintext_yescaptcha_expected
        ):
            merged["yescaptcha_client_key"] = plaintext_yescaptcha_key
    elif remove_plaintext_yescaptcha_key is not None and str(
        merged.get("yescaptcha_client_key", "") or ""
    ).strip() == str(remove_plaintext_yescaptcha_key).strip():
        merged.pop("yescaptcha_client_key", None)
    return merged


def _bounded_int(value, fallback, minimum, maximum):
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        parsed = int(fallback)
    return max(int(minimum), min(parsed, int(maximum)))


def _resolve_yescaptcha_secret(
    secret_store, config: dict
) -> tuple[str, bool, str | None]:
    """Return the winning key, DPAPI state, and stale plaintext to remove."""

    stored_key = secret_store.get(YESCAPTCHA_SECRET_KEY)
    legacy_key = str(config.get("yescaptcha_client_key", "") or "").strip()
    if stored_key:
        if legacy_key and legacy_key != stored_key:
            winning_key, persisted = secret_store.compare_and_set(
                YESCAPTCHA_SECRET_KEY, stored_key, legacy_key
            )
            if persisted:
                return winning_key, bool(winning_key), legacy_key
            return legacy_key, False, None
        stale_plaintext = legacy_key if "yescaptcha_client_key" in config else None
        return stored_key, True, stale_plaintext
    if not legacy_key:
        return "", False, None
    winning_key, secret_backed = secret_store.get_or_set(
        YESCAPTCHA_SECRET_KEY, legacy_key
    )
    if secret_backed and winning_key:
        return winning_key, True, legacy_key
    return legacy_key, False, None


def _remove_matching_yescaptcha_plaintext(
    existing: object, expected_key: str
) -> dict:
    values = dict(existing) if isinstance(existing, dict) else {}
    current = str(values.get("yescaptcha_client_key", "") or "").strip()
    if current == expected_key:
        values.pop("yescaptcha_client_key", None)
    return values


def _persist_yescaptcha_secret(
    secret_store,
    entered_key: str,
    loaded_key: str,
    secret_backed: bool,
) -> tuple[str, bool, bool]:
    """Persist a key without letting an unchanged stale window overwrite it."""

    if entered_key != loaded_key:
        winning_key, persisted = secret_store.compare_and_set(
            YESCAPTCHA_SECRET_KEY, loaded_key, entered_key
        )
        if persisted:
            return winning_key, bool(winning_key), False
        return entered_key, secret_backed, True
    if entered_key and not secret_backed:
        winning_key, persisted = secret_store.get_or_set(
            YESCAPTCHA_SECRET_KEY, entered_key
        )
        if persisted and winning_key:
            return winning_key, True, False
        return entered_key, False, True
    return entered_key, secret_backed, False


def _merge_config_migration(existing: object, before: dict, after: dict) -> dict:
    """Apply only migration edits whose source values were not changed elsewhere."""

    merged = dict(existing) if isinstance(existing, dict) else {}
    missing = object()
    for key in before.keys() | after.keys():
        old_value = before.get(key, missing)
        new_value = after.get(key, missing)
        if old_value == new_value:
            continue
        if merged.get(key, missing) != old_value:
            continue
        if new_value is missing:
            merged.pop(key, None)
        else:
            merged[key] = new_value
    return merged


def _create_lucide_eye_icon(size: tuple[int, int] = (15, 15), color: str = "#8E8E93") -> ctk.CTkImage:
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


def _create_lucide_eye_off_icon(size: tuple[int, int] = (15, 15), color: str = "#8E8E93") -> ctk.CTkImage:
    scale = 8
    canvas_dim = 24
    canvas_size = canvas_dim * scale
    img = Image.new("RGBA", (canvas_size, canvas_size), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)
    sw = int(1.8 * scale)

    # Lower curve
    draw.arc([2 * scale, 4 * scale, 22 * scale, 20 * scale], start=20, end=150, fill=color, width=sw)
    # Upper arcs
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


class DatePickerDialog(ctk.CTkToplevel):
    def __init__(self, parent, initial_date, on_select, allowed_dates=None):
        super().__init__(parent)
        self.on_select = on_select
        self.allowed_dates = {str(value) for value in (allowed_dates or ()) if str(value)}
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
                if value < today or (
                    self.allowed_dates and value.isoformat() not in self.allowed_dates
                ):
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
            basis = getattr(estimated[0], "estimate_basis", "")
            reason = getattr(estimated[0], "estimate_reason", "")
            basis_text = {
                "same_weekday": "같은 요일",
                "same_day_type": "같은 주중/주말 유형",
                "nearest": "가장 가까운 공개 날짜",
            }.get(basis, "최근 공개 날짜")
            source_text = f"{source_date} {basis_text}" if source_date else basis_text
            if reason == "traffic_over":
                prefix = "둠이스케이프 서버 일일 트래픽 초과 · 저장된 시간표 사용"
            elif reason == "server_outage":
                prefix = "둠이스케이프 서버 연결 장애 · 저장된 시간표 사용"
            else:
                prefix = "아직 닫힌 날짜"
            self.status.configure(
                text=(
                    f"{prefix} · {source_text}의 예상 시간표 {len(slots)}개를 표시합니다. "
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
        self._secret_baseline = {}
        self._config_baseline = {}
        self._yescaptcha_secret_backed = False
        self.cgv_selection = {}
        
        # Thread memory states.
        #
        # Naver defaults to 1: a single account can hold a single booking, so extra
        # workers only duplicate the same request from the same session. The engine
        # clamps anything higher anyway, and this keeps the slider honest about it.
        self.standard_threads = 30
        self.naver_threads = 1
        self.keyescape_threads = 1
        self.dpsnnn_threads = DPSNNN_DEFAULT_WORKERS
        self.cgv_threads = CGV_DEFAULT_WORKERS
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

        self.cgv_selector_frame = ctk.CTkFrame(self.branch_frame, fg_color="transparent")
        self.cgv_selector_frame.columnconfigure(0, weight=1)
        self.cgv_selection_summary = ctk.CTkLabel(
            self.cgv_selector_frame,
            text="지점·영화·회차·좌석을 선택해주세요.",
            font=theme.FONT_LABEL,
            text_color=theme.TEXT_MUTE,
            anchor="w",
            justify="left",
        )
        self.cgv_selection_summary.grid(row=0, column=0, sticky="ew", padx=(0, theme.SPACE_2))
        self.cgv_selector_button = ctk.CTkButton(
            self.cgv_selector_frame,
            text="선택",
            command=self._open_cgv_selector,
            width=72,
            height=theme.H_CONTROL,
            fg_color=theme.ACCENT_BLUE,
            hover_color=theme.ACCENT_BLUE_HOVER,
            text_color=theme.TEXT_PRIMARY,
            corner_radius=theme.ROUNDED_MD,
        )
        self.cgv_selector_button.grid(row=0, column=1, sticky="e")
        self.cgv_selector_frame.pack(fill="x")
        self.cgv_selector_frame.pack_forget()

        self.cgv_site_no_entry = ctk.CTkEntry(
            self.branch_frame,
            placeholder_text="CGV 지점번호 4자리 (예: 0013)",
            fg_color=theme.ELEVATED_COLOR,
            border_color=theme.HAIRLINE_COLOR,
            text_color=theme.TEXT_PRIMARY,
            placeholder_text_color=theme.TEXT_DISABLED,
            corner_radius=theme.ROUNDED_MD,
            height=theme.H_CONTROL,
        )
        self.cgv_site_no_entry.pack(fill="x", pady=(4, 0))
        self.cgv_site_no_entry.pack_forget()

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

        self.cgv_movie_entry = ctk.CTkEntry(
            self.theme_frame,
            placeholder_text="영화명 일부 또는 전체 (예: 오디세이)",
            fg_color=theme.ELEVATED_COLOR,
            border_color=theme.HAIRLINE_COLOR,
            text_color=theme.TEXT_PRIMARY,
            placeholder_text_color=theme.TEXT_DISABLED,
            corner_radius=theme.ROUNDED_MD,
            height=theme.H_CONTROL,
        )
        self.cgv_movie_entry.pack(fill="x")
        self.cgv_movie_entry.pack_forget()

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

        self.cgv_options_frame = ctk.CTkFrame(self.custom_theme_frame, fg_color="transparent")
        self.cgv_options_frame.columnconfigure((0, 1), weight=1, uniform="cgv")
        self.cgv_auditorium_entry = ctk.CTkEntry(
            self.cgv_options_frame,
            placeholder_text="상영관/포맷 (예: IMAX)",
            fg_color=theme.ELEVATED_COLOR,
            border_color=theme.HAIRLINE_COLOR,
            text_color=theme.TEXT_PRIMARY,
            placeholder_text_color=theme.TEXT_DISABLED,
            corner_radius=theme.ROUNDED_MD,
            height=theme.H_CONTROL,
        )
        self.cgv_auditorium_entry.grid(row=0, column=0, padx=(0, 4), sticky="ew")
        self.cgv_seats_entry = ctk.CTkEntry(
            self.cgv_options_frame,
            placeholder_text="좌석 우선순위 (A22,A23 | A17,A18)",
            fg_color=theme.ELEVATED_COLOR,
            border_color=theme.HAIRLINE_COLOR,
            text_color=theme.TEXT_PRIMARY,
            placeholder_text_color=theme.TEXT_DISABLED,
            corner_radius=theme.ROUNDED_MD,
            height=theme.H_CONTROL,
        )
        self.cgv_seats_entry.grid(row=0, column=1, padx=(4, 0), sticky="ew")
        self.cgv_options_frame.pack(fill="x")
        self.cgv_options_frame.pack_forget()

        self.tripcom_event_frame = ctk.CTkFrame(
            self,
            fg_color=theme.ELEVATED_COLOR,
            border_width=1,
            border_color=theme.HAIRLINE_COLOR,
            corner_radius=theme.ROUNDED_MD,
        )
        self.tripcom_event_label = ctk.CTkLabel(
            self.tripcom_event_frame,
            text="고급 설정에서 이벤트 정보를 갱신해주세요.",
            font=theme.FONT_BODY_SM,
            text_color=theme.TEXT_MUTE,
            justify="left",
            anchor="w",
        )
        self.tripcom_event_label.pack(fill="x", padx=10, pady=7)
        self.tripcom_event_frame.grid_forget()

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

        # Top row: title label and value badge
        self.threads_label_frame = ctk.CTkFrame(self.threads_frame, fg_color="transparent")
        self.threads_label_frame.pack(fill="x", pady=(0, theme.LABEL_GAP))

        self.threads_title_label = ctk.CTkLabel(
            self.threads_label_frame,
            text="동시 시도 수",
            font=theme.FONT_BODY_SM,
            text_color=theme.TEXT_MUTE,
            anchor="w"
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
            height=12,
            corner_radius=6,
            button_length=16,
            button_corner_radius=8
        )
        self.threads_slider.set(30)
        self.threads_slider.pack(fill="x", expand=True, pady=(2, 0))

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

        self.cgv_auth_frame = ctk.CTkFrame(
            self.advanced_frame,
            fg_color=theme.ELEVATED_COLOR,
            border_color=theme.HAIRLINE_COLOR,
            border_width=1,
            corner_radius=theme.ROUNDED_MD,
        )
        self.cgv_auth_frame.grid(row=2, column=0, sticky="ew", pady=(0, theme.SPACE_2))
        self.cgv_auth_frame.grid_remove()
        self.cgv_auth_frame.columnconfigure((0, 1), weight=1)
        ctk.CTkLabel(
            self.cgv_auth_frame,
            text="CGV 예매 방식",
            font=theme.FONT_BODY_SM,
            text_color=theme.TEXT_MUTE,
        ).grid(row=0, column=0, sticky="w", padx=theme.SPACE_2, pady=(theme.SPACE_2, theme.SPACE_1))
        self.cgv_booking_mode_var = ctk.StringVar(value="회원")
        self.cgv_booking_mode = ctk.CTkSegmentedButton(
            self.cgv_auth_frame,
            values=["회원", "비회원"],
            variable=self.cgv_booking_mode_var,
            command=self._on_cgv_booking_mode_change,
            fg_color=theme.SURFACE_COLOR,
            selected_color=theme.ACCENT_BLUE,
            selected_hover_color=theme.ACCENT_BLUE_HOVER,
            unselected_color=theme.SURFACE_COLOR,
            unselected_hover_color=theme.CARD_COLOR,
            text_color=theme.TEXT_PRIMARY,
            height=theme.H_CONTROL,
            corner_radius=theme.ROUNDED_MD,
        )
        self.cgv_booking_mode.grid(
            row=0, column=1, sticky="ew", padx=theme.SPACE_2,
            pady=(theme.SPACE_2, theme.SPACE_1)
        )
        self.cgv_auth_hint = ctk.CTkLabel(
            self.cgv_auth_frame,
            text="전용 Chrome의 가장 최근 CGV 로그인 세션을 사용합니다.",
            font=theme.FONT_LABEL,
            text_color=theme.TEXT_TERTIARY,
            anchor="w",
            justify="left",
        )
        self.cgv_auth_hint.grid(
            row=1, column=0, columnspan=2, sticky="ew",
            padx=theme.SPACE_2, pady=(theme.SPACE_1, theme.SPACE_2)
        )
        self.cgv_nonmember_frame = ctk.CTkFrame(self.cgv_auth_frame, fg_color="transparent")
        self.cgv_nonmember_frame.columnconfigure((0, 1, 2), weight=1)
        self.cgv_nonmember_birth_entry = ctk.CTkEntry(
            self.cgv_nonmember_frame,
            placeholder_text="생년월일 YYYYMMDD",
            fg_color=theme.SURFACE_COLOR,
            border_color=theme.HAIRLINE_COLOR,
            text_color=theme.TEXT_PRIMARY,
            placeholder_text_color=theme.TEXT_MUTE,
            height=theme.H_CONTROL,
            corner_radius=theme.ROUNDED_MD,
        )
        self.cgv_nonmember_birth_entry.grid(row=0, column=0, sticky="ew", padx=(0, theme.SPACE_1))
        self.cgv_nonmember_phone_entry = ctk.CTkEntry(
            self.cgv_nonmember_frame,
            placeholder_text="휴대전화번호",
            fg_color=theme.SURFACE_COLOR,
            border_color=theme.HAIRLINE_COLOR,
            text_color=theme.TEXT_PRIMARY,
            placeholder_text_color=theme.TEXT_MUTE,
            height=theme.H_CONTROL,
            corner_radius=theme.ROUNDED_MD,
        )
        self.cgv_nonmember_phone_entry.grid(row=0, column=1, sticky="ew", padx=theme.SPACE_1)
        self.cgv_nonmember_password_entry = ctk.CTkEntry(
            self.cgv_nonmember_frame,
            placeholder_text="예매 비밀번호 8~16자",
            show="•",
            fg_color=theme.SURFACE_COLOR,
            border_color=theme.HAIRLINE_COLOR,
            text_color=theme.TEXT_PRIMARY,
            placeholder_text_color=theme.TEXT_MUTE,
            height=theme.H_CONTROL,
            corner_radius=theme.ROUNDED_MD,
        )
        self.cgv_nonmember_password_entry.grid(row=0, column=2, sticky="ew", padx=(theme.SPACE_1, 0))
        self._setup_entry_focus(self.cgv_nonmember_birth_entry)
        self._setup_entry_focus(self.cgv_nonmember_phone_entry)
        self._setup_entry_focus(self.cgv_nonmember_password_entry)

        self._icon_eye = _create_lucide_eye_icon((16, 16), color="#8E8E93")
        self._icon_eye_off = _create_lucide_eye_off_icon((16, 16), color="#8E8E93")
        self.cgv_npay_eye_visible = False

        self.cgv_npay_frame = ctk.CTkFrame(self.cgv_auth_frame, fg_color="transparent")
        self.cgv_npay_frame.grid(
            row=3, column=0, columnspan=2, sticky="ew",
            padx=theme.SPACE_2, pady=(theme.SPACE_1, theme.SPACE_1)
        )
        self.cgv_npay_frame.columnconfigure(0, weight=0)
        self.cgv_npay_frame.columnconfigure(1, weight=1)

        ctk.CTkLabel(
            self.cgv_npay_frame,
            text="네이버페이 비밀번호",
            font=theme.FONT_BODY_SM,
            text_color=theme.TEXT_MUTE,
        ).grid(row=0, column=0, sticky="w", padx=(0, theme.SPACE_2))

        self.cgv_npay_entry_box = ctk.CTkFrame(self.cgv_npay_frame, fg_color="transparent")
        self.cgv_npay_entry_box.grid(row=0, column=1, sticky="ew")
        self.cgv_npay_entry_box.columnconfigure(0, weight=1)

        self.cgv_npay_password_entry = ctk.CTkEntry(
            self.cgv_npay_entry_box,
            placeholder_text="비밀번호 6자리",
            show="•",
            fg_color=theme.SURFACE_COLOR,
            border_color=theme.HAIRLINE_COLOR,
            text_color=theme.TEXT_PRIMARY,
            placeholder_text_color=theme.TEXT_MUTE,
            height=theme.H_CONTROL,
            corner_radius=theme.ROUNDED_MD,
        )
        self.cgv_npay_password_entry.grid(row=0, column=0, sticky="ew", padx=(0, theme.SPACE_1))
        self._setup_entry_focus(self.cgv_npay_password_entry)

        self.cgv_npay_eye_button = ctk.CTkButton(
            self.cgv_npay_entry_box,
            image=self._icon_eye,
            text="",
            width=32,
            height=theme.H_CONTROL,
            fg_color=theme.SURFACE_COLOR,
            hover_color=theme.CARD_COLOR,
            border_color=theme.HAIRLINE_COLOR,
            border_width=1,
            corner_radius=theme.ROUNDED_MD,
            command=self._toggle_cgv_npay_eye,
        )
        self.cgv_npay_eye_button.grid(row=0, column=1, sticky="e")

        self.cgv_npay_hint = ctk.CTkLabel(
            self.cgv_auth_frame,
            text="네이버페이 결제창에서 6자리 비밀번호를 자동 입력합니다. (미입력 시 수동 입력)",
            font=theme.FONT_LABEL,
            text_color=theme.TEXT_TERTIARY,
            anchor="w",
            justify="left",
        )
        self.cgv_npay_hint.grid(
            row=4, column=0, columnspan=2, sticky="ew",
            padx=theme.SPACE_2, pady=(0, theme.SPACE_2)
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
            row=3, column=0, sticky="w", pady=(theme.SPACE_2, theme.SPACE_2)
        )

        self.catalog_refresh_frame = ctk.CTkFrame(self.advanced_frame, fg_color="transparent")
        self.catalog_refresh_frame.grid(row=4, column=0, sticky="ew")
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

        self.keyescape_cache_frame = ctk.CTkFrame(
            self.advanced_frame, fg_color="transparent"
        )
        self.keyescape_cache_frame.grid(row=5, column=0, sticky="ew", pady=(theme.SPACE_2, 0))
        self.keyescape_cache_frame.columnconfigure(1, weight=1)
        self.keyescape_cache_btn = ctk.CTkButton(
            self.keyescape_cache_frame,
            text="전체 시간표 미리 저장",
            width=142,
            height=theme.H_CONTROL,
            font=theme.FONT_BODY_SM,
            fg_color=theme.ELEVATED_COLOR,
            hover_color=theme.CARD_COLOR,
            command=self._request_keyescape_cache_refresh,
        )
        self.keyescape_cache_btn.grid(row=0, column=0, sticky="w")
        self.keyescape_cache_status = ctk.CTkLabel(
            self.keyescape_cache_frame,
            text="저장 기록 없음",
            font=theme.FONT_LABEL,
            text_color=theme.TEXT_TERTIARY,
            anchor="e",
        )
        self.keyescape_cache_status.grid(
            row=0, column=1, sticky="e", padx=(theme.SPACE_2, 0)
        )
        self._keyescape_cache_busy = False
        self.keyescape_cache_frame.grid_remove()

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
        self.dev_mode_frame.grid(row=6, column=0, sticky="ew", pady=(theme.SPACE_2, 0))
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
        self._setup_entry_focus(self.cgv_site_no_entry)
        self._setup_entry_focus(self.cgv_movie_entry)
        self._setup_entry_focus(self.cgv_auditorium_entry)
        self._setup_entry_focus(self.cgv_seats_entry)
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
        metadata = self._selected_theme_metadata()
        allowed_dates = (
            metadata.get("allowed_dates", ())
            if self.engine_mode_btn.get() == TRIPCOM_MODE
            else ()
        )
        DatePickerDialog(
            self,
            self.date_entry.get().strip(),
            self._set_selected_date,
            allowed_dates=allowed_dates,
        )

    def _set_selected_date(self, value):
        self.date_entry.delete(0, "end")
        self.date_entry.insert(0, value)
        if self.cgv_selection and self.cgv_selection.get("date") != value:
            # A CGV selection is date-specific.  Keep the chosen theater, but
            # require an actual screening/seat selection for the new date.
            self.cgv_selection = {
                "site_no": self.cgv_selection.get("site_no", ""),
                "site_name": self.cgv_selection.get("site_name", ""),
                "region": self.cgv_selection.get("region", ""),
            }
            self._render_cgv_selection_summary()
        self._on_date_change()

    def _open_cgv_selector(self):
        try:
            people = int(self.people_entry.get().strip() or "2")
        except ValueError:
            people = 2
        reservation_date = self.date_entry.get().strip()
        try:
            datetime.strptime(reservation_date, "%Y-%m-%d")
        except ValueError:
            reservation_date = (datetime.now().date() + timedelta(days=1)).isoformat()
        if self.cgv_selection.get("date"):
            reservation_date = str(self.cgv_selection["date"])
        if self.cgv_selection.get("people"):
            try:
                people = int(self.cgv_selection["people"])
            except (TypeError, ValueError):
                pass
        from ui.cgv_booking_dialog import CgvBookingDialog

        return CgvBookingDialog(
            self,
            reservation_date=reservation_date,
            people=people,
            initial=self.cgv_selection,
            on_select=self._set_cgv_selection,
        )

    @staticmethod
    def _set_entry_text_safely(entry, text: str) -> None:
        try:
            prev_state = entry.cget("state")
        except Exception:
            prev_state = getattr(entry, "config", {}).get("state", "normal")
        if prev_state == "disabled":
            entry.configure(state="normal")
        entry.delete(0, "end")
        entry.insert(0, str(text))
        if prev_state == "disabled":
            entry.configure(state="disabled")

    def _set_cgv_selection(self, selection):
        self.cgv_selection = dict(selection or {})
        date_value = str(self.cgv_selection.get("date", ""))
        time_value = str(self.cgv_selection.get("show_time", ""))
        people_value = str(self.cgv_selection.get("people", "2"))
        site_name = str(self.cgv_selection.get("site_name", ""))
        movie_name = str(self.cgv_selection.get("movie", ""))
        if date_value:
            ReservationForm._set_entry_text_safely(self.date_entry, date_value)
        if time_value:
            ReservationForm._set_entry_text_safely(self.time_entry, time_value)
        if people_value:
            ReservationForm._set_entry_text_safely(self.people_entry, people_value)
        if site_name:
            self.branch_var.set(site_name)
        if movie_name:
            self.theme_var.set(movie_name)
        self._render_cgv_selection_summary()
        self.auto_save()

    def _render_cgv_selection_summary(self):
        selection = self.cgv_selection
        if not selection.get("site_no"):
            self.cgv_selection_summary.configure(
                text="IMAX 지점·영화·회차·좌석을 선택해주세요.",
                text_color=theme.TEXT_MUTE,
            )
            self.cgv_selector_button.configure(text="선택")
            return
        if not selection.get("movie"):
            self.cgv_selection_summary.configure(
                text=f"{selection.get('site_name', 'CGV')} · 영화·회차와 좌석을 다시 선택해주세요.",
                text_color=theme.ACCENT_YELLOW,
            )
            self.cgv_selector_button.configure(text="계속")
            return
        site_name = selection.get("site_name") or "CGV"
        movie = selection.get("movie", "")
        auditorium = selection.get("auditorium", "")
        format_name = selection.get("format", "")
        date_str = selection.get("date", "")
        people_str = f"{selection.get('people', 2)}명"
        preferred_times = selection.get("preferred_times") or (
            [selection.get("show_time")] if selection.get("show_time") else []
        )
        times_preview = " → ".join(preferred_times) if preferred_times else "시간 미선택"
        seats = str(selection.get("seats", ""))
        seat_preview = seats if len(seats) <= 28 else f"{seats[:25]}..."
        summary_lines = [
            f"{site_name}  ·  {movie} ({format_name or auditorium})",
            f"{date_str}  ·  {people_str}  ·  시간: {times_preview}  ·  좌석: {seat_preview}",
        ]
        self.cgv_selection_summary.configure(
            text="\n".join(summary_lines),
            text_color=theme.TEXT_BODY,
        )
        self.cgv_selector_button.configure(text="변경")

    def _open_time_picker(self):
        if self.engine_mode_btn.get() == TRIPCOM_MODE:
            open_time = str(self._selected_theme_metadata().get("open_time", ""))[:5]
            if open_time:
                self._set_selected_time(open_time)
            return
        if getattr(self, "_site_uses_cgv", lambda: False)():
            self._open_cgv_selector()
            return
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
        self.auto_save()

    def _persist_secret_if_changed(self, key, value):
        value = str(value or "")
        baseline = getattr(self, "_secret_baseline", {})
        if value == baseline.get(key, ""):
            return True
        if self.secret_store.set(key, value):
            baseline[key] = value
            self._secret_baseline = baseline
            return True
        return False

    def _request_catalog_refresh(self):
        if hasattr(self.master, "_refresh_current_catalog"):
            self.master._refresh_current_catalog()

    def _request_keyescape_cache_refresh(self):
        if hasattr(self.master, "_refresh_all_keyescape_timetables"):
            self.master._refresh_all_keyescape_timetables()

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

    def set_keyescape_cache_state(self, text, *, busy=False):
        self._keyescape_cache_busy = bool(busy)
        self.keyescape_cache_status.configure(text=str(text))
        disabled = bool(busy or getattr(self, "_booking_running", False))
        self.keyescape_cache_btn.configure(state="disabled" if disabled else "normal")

    def _site_uses_keyescape(self, site_name=None) -> bool:
        """Return whether a standard-mode site is backed by KeyescapeEngine."""
        site_name = self.current_site if site_name is None else site_name
        if site_name == "키이스케이프":
            return True
        site = self.custom_sites.get(site_name) or {}
        return (site.get("engine_id") or site.get("style")) == "keyescape"

    def _site_uses_dpsnnn(self, site_name=None) -> bool:
        """Return whether the selected custom site uses the Dpsnnn engine."""
        site_name = self.current_site if site_name is None else site_name
        site = self.custom_sites.get(site_name) or {}
        return site.get("engine_id") == "dpsnnn"

    def _site_uses_cgv(self, site_name=None) -> bool:
        site_name = self.current_site if site_name is None else site_name
        if site_name == "CGV":
            return True
        site = self.custom_sites.get(site_name) or {}
        return site.get("engine_id") == "cgv"

    def _standard_thread_limit(self, site_name=None) -> int:
        """Return the measured UI ceiling for the selected standard engine."""
        selected = getattr(self, "current_site", "") if site_name is None else site_name
        custom_sites = getattr(self, "custom_sites", {}) or {}
        site = custom_sites.get(selected) or {}
        engine_id = str(site.get("engine_id") or "").lower()
        style = str(site.get("style") or "").lower()
        if selected == "둠이스케이프" or engine_id == "doomescape":
            return DOOMESCAPE_MAX_WORKERS
        if selected in {"제로월드", "지구별방탈출"}:
            return ZEROWORLD_JIGUBYEOL_MAX_WORKERS
        if engine_id in {
            "jigubyeol",
            "sinbiworld",
            "zeroworld_laravel",
            "zeroworld_gu",
            "zeroworld_shin",
        } or style in {"jigubyeol", "zeroworld"}:
            return ZEROWORLD_JIGUBYEOL_MAX_WORKERS
        return STANDARD_MAX_WORKERS

    def _selected_cgv_site_no(self) -> str:
        selected = str(self.cgv_selection.get("site_no", "")).strip()
        if selected:
            return selected
        branch_value = str(
            self.config.get("branches", {}).get(self.branch_var.get(), "")
        ).strip()
        if branch_value == CGV_MANUAL_SITE_VALUE:
            return self.cgv_site_no_entry.get().strip()
        return branch_value

    def _keyescape_ui_active(self) -> bool:
        return (
            self.engine_mode_btn.get() != NAVER_MODE
            and self._site_uses_keyescape()
        )

    def _remember_active_thread_value(self) -> None:
        """Save the slider under the policy that owned it before a mode change."""
        value = int(self.threads_slider.get())
        if self.last_mode in {NAVER_MODE, TRIPCOM_MODE}:
            self.naver_threads = 1
        elif self._site_uses_keyescape():
            self.keyescape_threads = max(1, min(value, 3))
        elif self._site_uses_dpsnnn():
            self.dpsnnn_threads = max(1, min(value, DPSNNN_MAX_WORKERS))
        elif self._site_uses_cgv():
            self.cgv_threads = max(1, min(value, CGV_MAX_WORKERS))
        else:
            limit = ReservationForm._standard_thread_limit(self)
            self.standard_threads = max(1, min(value, limit))

    def _apply_thread_policy(self) -> None:
        """Fully reset the shared slider for the active engine family.

        Every branch writes range, step count, state, value and labels. This is
        intentionally idempotent: site and mode callbacks can arrive in either
        order without leaking Keyescape's cap into standard sites or unlocking
        Naver's fixed single worker.
        """
        if self.engine_mode_btn.get() == TRIPCOM_MODE:
            self.threads_slider.configure(
                from_=1, to=8, number_of_steps=7, state="disabled"
            )
            self.threads_slider.set(1)
            self.threads_value_label.configure(text="1", text_color=theme.TEXT_DISABLED)
            self.threads_title_label.configure(
                text="동시 시도 수 (Trip.com은 1개 고정)",
                text_color=theme.TEXT_DISABLED,
            )
            return

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

        if getattr(self, "_site_uses_cgv", lambda: False)():
            self.cgv_threads = max(1, min(self.cgv_threads, CGV_MAX_WORKERS))
            self.threads_slider.configure(
                from_=1,
                to=CGV_MAX_WORKERS,
                number_of_steps=CGV_MAX_WORKERS - 1,
                state="normal",
            )
            self.threads_slider.set(self.cgv_threads)
            self.threads_value_label.configure(
                text=str(self.cgv_threads), text_color=theme.ACCENT_BLUE
            )
            self.threads_title_label.configure(
                text=(
                    f"회차 조회 동시 연결 (최대 {CGV_MAX_WORKERS} · "
                    "좌석은 단일 감시 · 최초 응답 재사용 · 제한 시 브라우저 전환)"
                ),
                text_color=theme.TEXT_MUTE,
            )
            return

        if self._site_uses_dpsnnn():
            self.dpsnnn_threads = max(
                1, min(self.dpsnnn_threads, DPSNNN_MAX_WORKERS)
            )
            self.threads_slider.configure(
                from_=1,
                to=DPSNNN_MAX_WORKERS,
                number_of_steps=DPSNNN_MAX_WORKERS - 1,
                state="normal",
            )
            self.threads_slider.set(self.dpsnnn_threads)
            self.threads_value_label.configure(
                text=str(self.dpsnnn_threads), text_color=theme.ACCENT_BLUE
            )
            self.threads_title_label.configure(
                text=f"동시 감시 세션 (단편선 실측 상한 {DPSNNN_MAX_WORKERS})",
                text_color=theme.TEXT_MUTE,
            )
            return

        limit = ReservationForm._standard_thread_limit(self)
        self.standard_threads = max(1, min(self.standard_threads, limit))
        self.threads_slider.configure(
            from_=1, to=limit, number_of_steps=limit - 1, state="normal"
        )
        self.threads_slider.set(self.standard_threads)
        self.threads_value_label.configure(
            text=str(self.standard_threads), text_color=theme.ACCENT_BLUE
        )
        if limit == DOOMESCAPE_MAX_WORKERS:
            title = f"동시 감시 세션 (둠이스케이프 실측 최고 {limit})"
        elif limit == ZEROWORLD_JIGUBYEOL_MAX_WORKERS:
            title = f"동시 시도 수 (실측 병목 전 최대 {limit})"
        else:
            title = "동시 시도 수"
        self.threads_title_label.configure(text=title, text_color=theme.TEXT_MUTE)

    def _on_mode_change(self, mode):
        if not getattr(self, "_is_initializing", False):
            self._remember_active_thread_value()

        self.last_mode = mode
        if mode in {NAVER_MODE, TRIPCOM_MODE}:
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
    DEV_MODE_ENGINE_IDS = ("naver", "keyescape", "dpsnnn", "tripcom", "cgv")
    DEV_MODE_TEXT_ON = "개발자 테스트 (Npay는 임시 예약 후 결제 직전 정지)"
    DEV_MODE_TEXT_OFF = "개발자 테스트 모드 (브라우저 예약 엔진 전용)"

    def _dev_mode_supported(self) -> bool:
        """Only the browser-driven engines have a halfway point to stop at."""
        if self.engine_mode_btn.get() in {NAVER_MODE, TRIPCOM_MODE}:
            return True
        if self.current_site in {"키이스케이프", "CGV"}:
            return True
        site = self.custom_sites.get(self.current_site) or {}
        engine_id = site.get("engine_id") or site.get("style")
        return engine_id in self.DEV_MODE_ENGINE_IDS

    def developer_mode_enabled(self) -> bool:
        """Return the checkbox's visible state, which is authoritative."""
        if not self._dev_mode_supported():
            return False
        return bool(self.dev_mode_checkbox.get())

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
        is_tripcom = (self.engine_mode_btn.get() == TRIPCOM_MODE)
        is_cgv = self._site_uses_cgv() and not is_naver and not is_tripcom
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
            cache_frame = getattr(self, "keyescape_cache_frame", None)
            if cache_frame is not None:
                cache_frame.grid(
                    row=5, column=0, sticky="ew", pady=(theme.SPACE_2, 0)
                )
        else:
            self.yescaptcha_frame.grid_forget()
            cache_frame = getattr(self, "keyescape_cache_frame", None)
            if cache_frame is not None:
                cache_frame.grid_remove()
        if is_cgv:
            self.cgv_auth_frame.grid()
            self._on_cgv_booking_mode_change()
            self.catalog_auto_refresh_checkbox.configure(state="disabled")
        else:
            self.cgv_auth_frame.grid_remove()
        
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
        elif is_tripcom:
            self.branch_dropdown.configure(state="normal")
            self.branch_label.configure(text_color=theme.TEXT_MUTE)
            self.day_type_segmented.configure(state="disabled")
            self.day_type_label.configure(text_color=theme.TEXT_DISABLED)
            self.theme_dropdown.configure(state="normal")
            self.theme_label.configure(text_color=theme.TEXT_MUTE)
            self.custom_theme_checkbox.configure(state="disabled", text_color=theme.TEXT_DISABLED)
            self.theme_pk_entry.configure(state="disabled", text_color=theme.TEXT_DISABLED)
            self.time_entry.configure(state="disabled", text_color=theme.TEXT_DISABLED)
            self.time_picker_btn.configure(state="disabled")
            self.phone_entry.configure(state="normal", text_color=theme.TEXT_PRIMARY)
            self.phone_label.configure(text_color=theme.TEXT_MUTE)
            self.engine_mode_frame.grid(row=6, column=0, columnspan=2, padx=theme.CARD_PAD, pady=theme.ROW_GAP, sticky="ew")
        else:
            # Enable standard controls
            self.branch_dropdown.configure(state="normal")
            self.branch_label.configure(text_color=theme.TEXT_MUTE)
            self.cgv_site_no_entry.configure(state="normal", text_color=theme.TEXT_PRIMARY)
            
            self.day_type_segmented.configure(state="normal")
            self.day_type_label.configure(text_color=theme.TEXT_MUTE)
            
            self.theme_label.configure(text_color=theme.TEXT_MUTE)
            self.cgv_movie_entry.configure(state="normal", text_color=theme.TEXT_PRIMARY)
            self.cgv_auditorium_entry.configure(state="normal", text_color=theme.TEXT_PRIMARY)
            self.cgv_seats_entry.configure(state="normal", text_color=theme.TEXT_PRIMARY)
            self.custom_theme_checkbox.configure(state="normal", text_color=theme.TEXT_MUTE)
            self.theme_pk_entry.configure(state="normal", text_color=theme.TEXT_PRIMARY)
            self.time_entry.configure(state="normal", text_color=theme.TEXT_PRIMARY)
            self.time_picker_btn.configure(state="normal")
            
            self.name_entry.configure(state="normal", text_color=theme.TEXT_PRIMARY)
            self.name_label.configure(text_color=theme.TEXT_MUTE)
            self.phone_entry.configure(state="normal", text_color=theme.TEXT_PRIMARY)
            self.phone_label.configure(text_color=theme.TEXT_MUTE)
            if is_cgv:
                self.branch_dropdown.configure(state="disabled")
                self.branch_label.configure(text_color=theme.TEXT_DISABLED)
                self.theme_dropdown.configure(state="disabled")
                self.theme_label.configure(text_color=theme.TEXT_DISABLED)
                self.date_entry.configure(state="disabled", text_color=theme.TEXT_DISABLED)
                self.date_label.configure(text_color=theme.TEXT_DISABLED)
                self.date_picker_btn.configure(state="disabled")
                self.time_entry.configure(state="disabled", text_color=theme.TEXT_DISABLED)
                self.time_label.configure(text_color=theme.TEXT_DISABLED)
                self.time_picker_btn.configure(state="disabled")
                self.people_entry.configure(state="disabled", text_color=theme.TEXT_DISABLED)
                self.people_label.configure(text_color=theme.TEXT_DISABLED)
                self.name_entry.configure(state="disabled", text_color=theme.TEXT_DISABLED)
                self.name_label.configure(text_color=theme.TEXT_DISABLED)
                self.phone_entry.configure(state="disabled", text_color=theme.TEXT_DISABLED)
                self.phone_label.configure(text_color=theme.TEXT_DISABLED)
                self.custom_theme_checkbox.configure(state="disabled", text_color=theme.TEXT_DISABLED)
                self.theme_pk_entry.configure(state="disabled", text_color=theme.TEXT_DISABLED)
                self.cgv_selector_button.configure(state="normal")
            else:
                self.date_entry.configure(state="normal", text_color=theme.TEXT_PRIMARY)
                self.date_label.configure(text_color=theme.TEXT_MUTE)
                self.date_picker_btn.configure(state="normal")
                self.time_label.configure(text_color=theme.TEXT_MUTE)
                self.people_entry.configure(state="normal", text_color=theme.TEXT_PRIMARY)
                self.people_label.configure(text_color=theme.TEXT_MUTE)
            
            self._toggle_custom_theme()
            self.engine_mode_frame.grid(row=6, column=0, columnspan=2, padx=theme.CARD_PAD, pady=(theme.ROW_GAP, theme.SPACE_2), sticky="ew")

        self._update_dev_mode_state()

        # Update Server Time Checkbox state
        current_mode = self.engine_mode_btn.get()
        site_name = self.current_site
        is_supported_site = (
            (current_mode == NAVER_MODE and site_name != "(네이버 예약을 등록하세요)")
            or current_mode == TRIPCOM_MODE
            or site_name == "키이스케이프"
        )

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
            self.cgv_selector_button,
            self.cgv_site_no_entry,
            self.day_type_segmented,
            self.theme_dropdown,
            self.cgv_movie_entry,
            self.custom_theme_checkbox,
            self.theme_pk_entry,
            self.cgv_auditorium_entry,
            self.cgv_seats_entry,
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
            self.catalog_auto_refresh_checkbox,
            self.catalog_refresh_btn,
            self.keyescape_cache_btn,
            self.cgv_booking_mode,
            self.cgv_nonmember_birth_entry,
            self.cgv_nonmember_phone_entry,
            self.cgv_nonmember_password_entry,
            self.cgv_npay_password_entry,
            self.cgv_npay_eye_button,
        )
        for widget in widgets:
            try:
                widget.configure(state=state)
            except Exception:
                continue
        if not running:
            self._update_widgets_state()
            if getattr(self, "_keyescape_cache_busy", False):
                self.keyescape_cache_btn.configure(state="disabled")

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
        is_tripcom = (self.engine_mode_btn.get() == TRIPCOM_MODE)
        is_cgv = self._site_uses_cgv() and not is_naver and not is_tripcom
        if is_naver:
            self.custom_theme_frame.grid_forget()
            self.tripcom_event_frame.grid_forget()
            themes_dict = self.config.get("themes", {}).get("1", {})
            has_themes = len(themes_dict) > 0 and not (len(themes_dict) == 1 and list(themes_dict.keys())[0] == "기본테마")
            if has_themes:
                self.theme_frame.grid(row=1, column=0, columnspan=2, padx=theme.CARD_PAD, pady=theme.ROW_GAP, sticky="ew")
            else:
                self.theme_frame.grid_forget()
        elif is_tripcom:
            self.custom_theme_frame.grid_forget()
            self.tripcom_event_frame.grid(
                row=2, column=0, columnspan=2,
                padx=theme.CARD_PAD, pady=theme.ROW_GAP, sticky="ew"
            )
            self.theme_frame.grid(row=1, column=0, columnspan=2, padx=theme.CARD_PAD, pady=theme.ROW_GAP, sticky="ew")
            self.branch_frame.grid(
                row=0, column=0, columnspan=2,
                padx=theme.CARD_PAD, pady=(theme.SPACE_2, theme.ROW_GAP), sticky="ew"
            )
            branch_options = list(self.config.get("branches", {}).keys())
            self.branch_dropdown.configure(values=branch_options)
            if branch_options and self.branch_var.get() not in branch_options:
                self.branch_var.set(branch_options[0])
        else:
            self.tripcom_event_frame.grid_forget()
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

        self._configure_cgv_fields(is_cgv)
        self._update_theme_options()
        self._update_widgets_state()
        self._is_initializing = was_initializing

    def _on_branch_change(self, value):
        self._sync_cgv_site_entry_visibility()
        self._update_theme_options()
        self.auto_save()

    def _configure_cgv_fields(self, active: bool) -> None:
        if active:
            self.branch_label.configure(text="CGV 예매 대상")
            self.branch_dropdown.pack_forget()
            if not self.cgv_selector_frame.winfo_manager():
                self.cgv_selector_frame.pack(fill="x")
            self.theme_frame.grid_forget()
            self.custom_theme_frame.grid_forget()
            self.name_label.configure(text="이름 (CGV 로그인 정보 사용)")
            self.phone_label.configure(text="전화번호 (CGV 로그인 정보 사용)")
            self.cgv_auth_frame.grid()
            self._on_cgv_booking_mode_change()
            self.catalog_auto_refresh_checkbox.configure(
                text="CGV 지점은 선택 창에서 실시간 조회", state="disabled"
            )
            self.catalog_refresh_btn.configure(text="CGV 선택 창 열기")
            self._render_cgv_selection_summary()
        else:
            self.branch_label.configure(text="지점")
            self.cgv_selector_frame.pack_forget()
            if not self.branch_dropdown.winfo_manager():
                self.branch_dropdown.pack(fill="x")
            if not self.theme_frame.winfo_manager():
                self.theme_frame.grid(
                    row=1, column=0, columnspan=2, padx=theme.CARD_PAD,
                    pady=theme.ROW_GAP, sticky="ew"
                )
            if not self.custom_theme_frame.winfo_manager():
                self.custom_theme_frame.grid(
                    row=2, column=0, columnspan=2, padx=theme.CARD_PAD,
                    pady=theme.ROW_GAP, sticky="ew"
                )
            self.theme_label.configure(text="테마 선택")
            self.cgv_movie_entry.pack_forget()
            if not self.theme_dropdown.winfo_manager():
                self.theme_dropdown.pack(fill="x")
            self.cgv_options_frame.pack_forget()
            if not self.checkbox_container.winfo_manager():
                self.checkbox_container.pack(fill="x", anchor="w", pady=(0, theme.LABEL_GAP))
            self.name_label.configure(text="이름")
            self.phone_label.configure(text="전화번호")
            self.cgv_auth_frame.grid_remove()
            self.catalog_auto_refresh_checkbox.configure(
                text="시작 시 사이트 정보 자동 갱신", state="normal"
            )
            self.catalog_refresh_btn.configure(text="현재 사이트 갱신")
        self._sync_cgv_site_entry_visibility()

    def _sync_cgv_site_entry_visibility(self) -> None:
        if self._site_uses_cgv() and str(
            self.config.get("branches", {}).get(self.branch_var.get(), "")
        ) == CGV_MANUAL_SITE_VALUE:
            if not self.cgv_site_no_entry.winfo_manager():
                self.cgv_site_no_entry.pack(fill="x", pady=(4, 0))
        else:
            self.cgv_site_no_entry.pack_forget()

    def _on_day_type_change(self, value):
        self._update_theme_options()
        self.auto_save()

    def _toggle_custom_theme(self):
        # Only run toggle behavior if not in Naver mode to prevent overriding disabled state
        if self.engine_mode_btn.get() in {NAVER_MODE, TRIPCOM_MODE}:
            return
        if self._site_uses_cgv():
            self.theme_dropdown.configure(state="disabled")
            self.theme_pk_entry.pack_forget()
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
        if self.engine_mode_btn.get() in {NAVER_MODE, TRIPCOM_MODE}:
            self.naver_threads = 1
            self.threads_slider.set(1)
            self.threads_value_label.configure(text="1")
            return
        self.threads_value_label.configure(text=str(val))
        if self._site_uses_keyescape():
            self.keyescape_threads = max(1, min(val, 3))
        elif self._site_uses_dpsnnn():
            self.dpsnnn_threads = max(1, min(val, DPSNNN_MAX_WORKERS))
        elif self._site_uses_cgv():
            self.cgv_threads = max(1, min(val, CGV_MAX_WORKERS))
        else:
            limit = ReservationForm._standard_thread_limit(self)
            self.standard_threads = max(1, min(val, limit))
        self.auto_save()

    def _on_cgv_booking_mode_change(self, _value=None):
        nonmember = self.cgv_booking_mode_var.get() == "비회원"
        if nonmember:
            self.cgv_auth_hint.configure(
                text=(
                    "현재 CGV 공식 비회원 예매는 생년월일·예매 비밀번호·휴대전화 인증을 요구합니다. "
                    "시작 후 열린 Chrome에서 문자 인증번호를 확인합니다."
                ),
                text_color=theme.ACCENT_YELLOW,
                wraplength=420,
            )
            self.cgv_nonmember_frame.grid(
                row=2, column=0, columnspan=2, sticky="ew",
                padx=theme.SPACE_2, pady=(0, theme.SPACE_2)
            )
        else:
            self.cgv_auth_hint.configure(
                text="전용 Chrome의 가장 최근 CGV 로그인 세션을 사용합니다.",
                text_color=theme.TEXT_TERTIARY,
                wraplength=420,
            )
            self.cgv_nonmember_frame.grid_forget()
        self.auto_save()

    def _toggle_cgv_npay_eye(self) -> None:
        self.cgv_npay_eye_visible = not getattr(self, "cgv_npay_eye_visible", False)
        if self.cgv_npay_eye_visible:
            self.cgv_npay_password_entry.configure(show="")
            self.cgv_npay_eye_button.configure(image=self._icon_eye_off)
        else:
            self.cgv_npay_password_entry.configure(show="•")
            self.cgv_npay_eye_button.configure(image=self._icon_eye)

    def _on_date_change(self, event=None):
        """Auto-detect weekday/weekend from the entered date."""
        if self._site_uses_cgv():
            date_str = self.date_entry.get().strip()
            if self.cgv_selection and self.cgv_selection.get("date") != date_str:
                self.cgv_selection = {
                    "site_no": self.cgv_selection.get("site_no", ""),
                    "site_name": self.cgv_selection.get("site_name", ""),
                    "region": self.cgv_selection.get("region", ""),
                }
                self._render_cgv_selection_summary()
                self.auto_save()
            return
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
        if self.engine_mode_btn.get() == TRIPCOM_MODE:
            self._apply_tripcom_event_selection()
        self.auto_save()

    def _selected_theme_metadata(self):
        branch_id = self.config.get("branches", {}).get(self.branch_var.get(), "")
        theme_id = self._theme_id_for_name(branch_id, self.theme_var.get())
        metadata = self.config.get("theme_metadata", {}).get(branch_id, {}).get(theme_id, {})
        return dict(metadata) if isinstance(metadata, dict) else {}

    def _apply_tripcom_event_selection(self):
        metadata = self._selected_theme_metadata()
        allowed = [str(value) for value in metadata.get("allowed_dates", ()) if str(value)]
        current = self.date_entry.get().strip()
        if allowed and current not in allowed:
            today = datetime.now().date().isoformat()
            selected = next((value for value in allowed if value >= today), allowed[0])
            self.date_entry.delete(0, "end")
            self.date_entry.insert(0, selected)
        open_time = str(metadata.get("open_time", ""))[:5]
        if open_time:
            self.time_entry.configure(state="normal")
            self.time_entry.delete(0, "end")
            self.time_entry.insert(0, open_time)
            self.time_entry.configure(state="disabled")
        if not metadata:
            text = "고급 설정에서 이벤트 정보를 갱신해주세요."
        else:
            status_labels = {
                "preheat": "오픈 전 · 상품 정보 확인 완료",
                "flash_sale": (
                    "항공 초특가 판매 중"
                    if metadata.get("action_kind") == "flight_flash_sale"
                    else "5만원 이벤트 판매 중"
                ),
                "backup_sale": "5만원 이벤트 종료 · 일반 특가만 판매",
                "sold_out": (
                    "항공 초특가 매진"
                    if metadata.get("action_kind") == "flight_flash_sale"
                    else "5만원 이벤트 매진"
                ),
                "ended": "이벤트 종료",
            }
            availability = status_labels.get(str(metadata.get("sale_status", "")))
            if not availability:
                availability = "앱 전용" if metadata.get("app_only") else (
                    "현재 재고 있음" if metadata.get("in_stock") is True else (
                        "현재 소진/오픈 전" if metadata.get("in_stock") is False else "재고 확인 필요"
                    )
                )
            dates = " · ".join(allowed[:3])
            if len(allowed) > 3:
                dates += f" 외 {len(allowed) - 3}일"
            detail = ""
            if metadata.get("action_kind") == "hotel_flash_sale":
                detail = (
                    f"\n{metadata.get('hotel_name', '')} · {metadata.get('room_name', '')}"
                    f" · 체크인 {metadata.get('check_in', '-') }"
                )
            elif metadata.get("action_kind") == "flight_flash_sale":
                detail = (
                    f"\n{metadata.get('departure_city', '')}→{metadata.get('arrival_city', '')}"
                    f" · 탑승 {metadata.get('departure_date', '-') }"
                    f" · {metadata.get('airline_name', '')}"
                    f" · {metadata.get('event_price', '-') }원"
                )
            elif metadata.get("action_kind") == "flight_coupon":
                detail = (
                    "\n항공권 선착순 할인코드 · 복사/발급 후 결제 단계에서 사용"
                    " · 코드 확보만으로 할인 재고가 확정되지는 않음"
                )
            text = f"오픈 {open_time or '-'} · {availability}\n가능 날짜 {dates or '-'}{detail}"
        self.tripcom_event_label.configure(text=text)

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
        if self.engine_mode_btn.get() == TRIPCOM_MODE:
            branch_name = self.branch_var.get()
            branch_id = self.config.get("branches", {}).get(branch_name, "")
            themes_dict = self.config.get("themes", {}).get(branch_id, {})
            theme_names = sorted(list(themes_dict.keys()))
        elif self.current_site in self.custom_sites:
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
        if self.engine_mode_btn.get() == TRIPCOM_MODE:
            self._apply_tripcom_event_selection()

    def get_reservation_data(self):
        is_naver = self.engine_mode_btn.get() == NAVER_MODE
        is_tripcom = self.engine_mode_btn.get() == TRIPCOM_MODE
        is_cgv = self._site_uses_cgv() and not is_naver and not is_tripcom
        branch_name = self.branch_var.get()
        branches = self.config.get("branches", {})
        if not branch_name and branches:
            branch_name = next(iter(branches))
        branch_id = "1" if is_naver else branches.get(branch_name, "1")
        if is_cgv:
            branch_id = self._selected_cgv_site_no()

        theme_name = self.theme_var.get()
        if is_naver:
            theme_pk = self.config.get("themes", {}).get("1", {}).get(theme_name, "")
            if not theme_pk or theme_pk == "naver":
                theme_pk = self.config.get("url", "")
        elif is_tripcom:
            theme_pk = self.config.get("themes", {}).get(branch_id, {}).get(theme_name, "")
        elif is_cgv:
            theme_pk = str(self.cgv_selection.get("movie", "")).strip()
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
        res_date = (
            str(self.cgv_selection.get("date", "")).strip()
            if is_cgv and self.cgv_selection.get("date")
            else self.date_entry.get().strip()
        )
        res_people = (
            str(self.cgv_selection.get("people", "")).strip()
            if is_cgv and self.cgv_selection.get("people")
            else self.people_entry.get().strip()
        )
        res_time = (
            str(self.cgv_selection.get("show_time", "")).strip()
            if is_cgv and self.cgv_selection.get("show_time")
            else self.time_entry.get().strip()
        )
        raw_values = {
            "branch": branch_id,
            "branchLabel": self.branch_var.get(),
            "reservationDate": res_date,
            "name": self.name_entry.get().strip(),
            "phone": self.phone_entry.get().strip(),
            "people": res_people,
            "themePK": theme_pk,
            "themeLabel": (
                theme_pk
                if is_cgv
                else self.theme_var.get()
                if not self.custom_theme_checkbox.get()
                else f"직접 입력 ({theme_pk})"
            ),
            "reservationTime": res_time,
            "paymentType": "1",
            "policy": "true",
            # Read the checkbox itself, not only its backing Tk variable. This
            # prevents a stale variable value from suppressing a real booking
            # after the user visibly turned developer mode off.
            "devMode": self.developer_mode_enabled(),
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
        if is_cgv:
            preferred_times = list(
                self.cgv_selection.get("preferred_times")
                or ([self.cgv_selection.get("show_time")] if self.cgv_selection.get("show_time") else [])
            )
            raw_values["engine_metadata"]["cgv"] = {
                "site_name": self.cgv_selection.get("site_name", branch_name),
                "movie": theme_pk,
                "auditorium": str(self.cgv_selection.get("auditorium", "")).strip(),
                "format": str(self.cgv_selection.get("format", "")).strip(),
                "seats": str(self.cgv_selection.get("seats", "")).strip(),
                "show_time": str(self.cgv_selection.get("show_time", "")).strip(),
                "preferred_times": preferred_times,
                "is_preopen": bool(self.cgv_selection.get("is_preopen", False)),
                "mov_no": str(self.cgv_selection.get("mov_no", "")).strip(),
                "reference_date": str(
                    self.cgv_selection.get("reference_date", "")
                ).strip(),
                "reference_only": bool(self.cgv_selection.get("reference_only", False)),
                "booking_mode": self.cgv_booking_mode_var.get(),
                "nonmember_birth": self.cgv_nonmember_birth_entry.get().strip(),
                "nonmember_phone": self.cgv_nonmember_phone_entry.get().strip(),
                "nonmember_password": self.cgv_nonmember_password_entry.get(),
                "npay_password": (
                    self.cgv_npay_password_entry.get().strip()
                    if hasattr(self, "cgv_npay_password_entry")
                    else ""
                ),
            }
        try:
            request = ReservationRequest.from_mapping(self.current_site, raw_values)
        except ValueError:
            return None, "인원 수를 숫자로 입력해주세요.", 0, False
        errors = request.validate(
            phone_required=not (is_naver or is_cgv),
            name_required=not is_cgv,
        )
        if errors:
            return None, errors[0], 0, False

        if is_cgv:
            import re

            cgv_metadata = raw_values["engine_metadata"]["cgv"]
            if not re.fullmatch(r"\d{4}", request.branch):
                return None, "CGV 전용 선택 화면에서 지점을 선택해주세요.", 0, False
            if not cgv_metadata["movie"]:
                return None, "CGV 전용 선택 화면에서 영화와 회차를 선택해주세요.", 0, False
            if not cgv_metadata["auditorium"]:
                return None, "CGV 전용 선택 화면에서 상영관을 선택해주세요.", 0, False
            if request.people > 8:
                return None, "CGV 관람 인원은 최대 8명입니다.", 0, False
            if not parse_seat_groups(cgv_metadata["seats"], request.people):
                return (
                    None,
                    "CGV 좌석도에서 인원수와 같은 개수의 좌석 우선순위를 선택해주세요.",
                    0,
                    False,
                )
            if cgv_metadata["booking_mode"] == "비회원":
                if not re.fullmatch(r"\d{8}", cgv_metadata["nonmember_birth"]):
                    return None, "CGV 비회원 생년월일을 YYYYMMDD 8자리로 입력해주세요.", 0, False
                if not re.fullmatch(r"01\d{8,9}", re.sub(r"\D", "", cgv_metadata["nonmember_phone"])):
                    return None, "CGV 비회원 휴대전화번호를 정확히 입력해주세요.", 0, False
                password = cgv_metadata["nonmember_password"]
                if not (
                    8 <= len(password) <= 16
                    and re.search(r"[A-Za-z]", password)
                    and re.search(r"\d", password)
                    and re.search(r"[^A-Za-z0-9]", password)
                ):
                    return None, "CGV 비회원 예매 비밀번호는 영문·숫자·특수문자를 포함한 8~16자여야 합니다.", 0, False

        if is_tripcom:
            event_metadata = raw_values["engine_metadata"]["theme"]
            allowed_dates = {str(value) for value in event_metadata.get("allowed_dates", ())}
            if allowed_dates and request.reservation_date not in allowed_dates:
                return None, "선택한 이벤트가 열리는 날짜를 달력에서 선택해주세요.", 0, False
            if event_metadata.get("app_only"):
                return None, "이 이벤트는 Trip.com 앱 전용이라 PC에서 선점할 수 없습니다.", 0, False
            if event_metadata.get("action_kind") == "hotel_flash_sale" and str(
                event_metadata.get("sale_status", "")
            ) in {"backup_sale", "sold_out", "ended"}:
                return (
                    None,
                    "선택한 5만원 호텔 이벤트는 이미 종료되었거나 매진되었습니다. 이벤트를 갱신해주세요.",
                    0,
                    False,
                )
            if event_metadata.get("action_kind") == "flight_flash_sale" and str(
                event_metadata.get("sale_status", "")
            ) in {"sold_out", "ended"}:
                return (
                    None,
                    "선택한 항공 초특가 이벤트는 이미 종료되었거나 매진되었습니다. 이벤트를 갱신해주세요.",
                    0,
                    False,
                )

        if is_naver or is_tripcom:
            threads = 1
        elif keyescape_active:
            threads = int(self.threads_slider.get())
            threads = max(1, min(threads, 3))
        elif is_cgv:
            slider = getattr(self, "threads_slider", None)
            threads = max(1, min(int(slider.get() if slider else CGV_MAX_WORKERS), CGV_MAX_WORKERS))
        elif getattr(self, "_site_uses_dpsnnn", lambda: False)():
            slider = getattr(self, "threads_slider", None)
            threads = max(
                1, min(int(slider.get() if slider else DPSNNN_MAX_WORKERS), DPSNNN_MAX_WORKERS)
            )
        else:
            slider = getattr(self, "threads_slider", None)
            limit = ReservationForm._standard_thread_limit(self)
            threads = max(1, min(int(slider.get() if slider else limit), limit))
        return request, None, threads, not (is_naver or is_tripcom)

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
        self._is_initializing = True
        try:
            self._load_config_values()
            try:
                self._config_baseline = self._current_config_values(
                    self.current_site
                )
            except (AttributeError, RuntimeError, ValueError):
                self._config_baseline = {}
        except Exception as exc:
            try:
                self._config_baseline = self._current_config_values(
                    self.current_site
                )
            except (AttributeError, RuntimeError, ValueError):
                self._config_baseline = {}
            master = getattr(self, "master", None)
            if master is not None and hasattr(master, "log_panel"):
                master.log_panel.append_log(
                    f"저장된 설정 일부를 불러오지 못했습니다: {exc}", "warning"
                )
        finally:
            self._is_initializing = False

    def _load_config_values(self):
        loaded_config = load_json("config.json", {})
        config = dict(loaded_config) if isinstance(loaded_config, dict) else {}
        stored_name = self.secret_store.get("reservation_name")
        stored_phone = self.secret_store.get("reservation_phone")
        stored_cgv_birth = self.secret_store.get("cgv_nonmember_birth")
        stored_cgv_phone = self.secret_store.get("cgv_nonmember_phone")
        stored_cgv_password = self.secret_store.get("cgv_nonmember_password")
        stored_cgv_npay_password = self.secret_store.get("cgv_npay_password")
        (
            stored_yescaptcha_key,
            self._yescaptcha_secret_backed,
            stale_plaintext_key,
        ) = _resolve_yescaptcha_secret(self.secret_store, config)
        if stale_plaintext_key is not None:
            config.pop("yescaptcha_client_key", None)

            def remove_legacy_key(raw):
                return _remove_matching_yescaptcha_plaintext(
                    raw, stale_plaintext_key
                )

            try:
                update_json("config.json", remove_legacy_key, {})
            except (OSError, TimeoutError, ValueError):
                pass
        self._secret_baseline = {
            "reservation_name": stored_name,
            "reservation_phone": stored_phone,
            "cgv_nonmember_birth": stored_cgv_birth,
            "cgv_nonmember_phone": stored_cgv_phone,
            "cgv_nonmember_password": stored_cgv_password,
            "cgv_npay_password": stored_cgv_npay_password,
            YESCAPTCHA_SECRET_KEY: stored_yescaptcha_key,
        }
        self.yescaptcha_client_key_entry.delete(0, "end")
        if stored_yescaptcha_key:
            self.yescaptcha_client_key_entry.insert(0, stored_yescaptcha_key)

        yescaptcha_enabled = coerce_bool(
            config.get("yescaptcha_enabled", False)
        )
        yescaptcha_test_mode = bool(
            yescaptcha_enabled
            and coerce_bool(config.get("yescaptcha_test_mode", False))
        )
        yescaptcha_soft_id = str(
            config.get("yescaptcha_soft_id", DEFAULT_SOFT_ID) or DEFAULT_SOFT_ID
        ).strip() or DEFAULT_SOFT_ID
        if yescaptcha_enabled:
            self.yescaptcha_checkbox.select()
        else:
            self.yescaptcha_checkbox.deselect()
        if yescaptcha_test_mode:
            self.yescaptcha_test_mode_checkbox.select()
        else:
            self.yescaptcha_test_mode_checkbox.deselect()
        self.yescaptcha_test_mode_checkbox.configure(
            state="normal" if yescaptcha_enabled else "disabled",
            text_color=theme.TEXT_MUTE if yescaptcha_enabled else theme.TEXT_DISABLED,
        )
        self.yescaptcha_soft_id_entry.delete(0, "end")
        self.yescaptcha_soft_id_entry.insert(0, yescaptcha_soft_id)
        migration_baseline = dict(config)
        if not config:
            if stored_name:
                self.name_entry.insert(0, stored_name)
            if stored_phone:
                self.phone_entry.insert(0, stored_phone)
            if stored_cgv_birth:
                self.cgv_nonmember_birth_entry.insert(0, stored_cgv_birth)
            if stored_cgv_phone:
                self.cgv_nonmember_phone_entry.insert(0, stored_cgv_phone)
            if stored_cgv_password:
                self.cgv_nonmember_password_entry.insert(0, stored_cgv_password)
            if stored_cgv_npay_password and hasattr(self, "cgv_npay_password_entry"):
                self.cgv_npay_password_entry.insert(0, stored_cgv_npay_password)
            return
        self._is_initializing = True
        config_migrated = False
        try:
            remember_personal = parse_bool_flag(
                config.get("remember_personal_info", True), default=True
            )
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
                if self._site_uses_cgv():
                    saved_selection = config.get("cgv_selection", {})
                    if isinstance(saved_selection, dict):
                        self.cgv_selection = dict(saved_selection)
                    for key, entry in (
                        ("cgv_site_no", self.cgv_site_no_entry),
                        ("cgv_movie", self.cgv_movie_entry),
                        ("cgv_auditorium", self.cgv_auditorium_entry),
                        ("cgv_seats", self.cgv_seats_entry),
                    ):
                        if key in config:
                            entry.delete(0, "end")
                            entry.insert(0, str(config[key]))
                    if not self.cgv_selection and config.get("cgv_site_no"):
                        self.cgv_selection = {
                            "site_no": str(config.get("cgv_site_no", "")),
                            "site_name": self.branch_var.get(),
                            "movie": str(config.get("cgv_movie", "")),
                            "auditorium": str(config.get("cgv_auditorium", "")),
                            "show_time": str(config.get("time", "")),
                            "seats": str(config.get("cgv_seats", "")),
                            "date": str(config.get("date", "")),
                        }
                        config_migrated = True
                    self._render_cgv_selection_summary()
                    self._sync_cgv_site_entry_visibility()
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
            name_secret_backed = bool(stored_name)
            phone_secret_backed = bool(stored_phone)
            if remember_personal and name:
                self.name_entry.delete(0, "end")
                self.name_entry.insert(0, name)
                if not stored_name:
                    if self.secret_store.set("reservation_name", name):
                        self._secret_baseline["reservation_name"] = name
                        name_secret_backed = True

            if remember_personal and phone:
                self.phone_entry.delete(0, "end")
                self.phone_entry.insert(0, phone)
                if not stored_phone:
                    if self.secret_store.set("reservation_phone", phone):
                        self._secret_baseline["reservation_phone"] = phone
                        phone_secret_backed = True

            if "name" in config and (
                not remember_personal or not name or name_secret_backed
            ):
                config.pop("name", None)
                config_migrated = True
            if "phone" in config and (
                not remember_personal or not phone or phone_secret_backed
            ):
                config.pop("phone", None)
                config_migrated = True
            if config_migrated:
                config["site"] = saved_site
                
            if "people" in config:
                self.people_entry.delete(0, "end")
                self.people_entry.insert(0, config["people"])
                
            # Parse memory threads first
            if "threads" in config:
                self.standard_threads = _bounded_int(
                    config["threads"], self.standard_threads, 1, 50
                )
            if "naver_threads" in config:
                # NaverEngine always owns exactly one browser worker.
                self.naver_threads = 1
            if "keyescape_threads" in config:
                self.keyescape_threads = _bounded_int(
                    config["keyescape_threads"], self.keyescape_threads, 1, 3
                )
            if "dpsnnn_threads" in config:
                self.dpsnnn_threads = _bounded_int(
                    config["dpsnnn_threads"],
                    self.dpsnnn_threads,
                    1,
                    DPSNNN_MAX_WORKERS,
                )
            if "cgv_threads" in config:
                self.cgv_threads = _bounded_int(
                    config["cgv_threads"], self.cgv_threads, 1, CGV_MAX_WORKERS
                )

            if "engine_mode" in config:
                mode_val = LEGACY_MODE_MAP.get(config["engine_mode"], config["engine_mode"])
                if mode_val == TRIPCOM_MODE:
                    mode_val = STANDARD_MODE
                self.engine_mode_btn.set(mode_val)
                self._on_mode_change(mode_val)
            elif "is_async" in config:
                val = STANDARD_MODE
                self.engine_mode_btn.set(val)
                self._on_mode_change(val)

            if "show_server_time" in config:
                if coerce_bool(config["show_server_time"]):
                    self.show_server_time_checkbox.select()
                else:
                    self.show_server_time_checkbox.deselect()
            else:
                self.show_server_time_checkbox.deselect()

            self.catalog_auto_refresh_var.set(
                parse_bool_flag(
                    config.get("catalog_auto_refresh", True), default=True
                )
            )
            cgv_mode = str(config.get("cgv_booking_mode", "회원"))
            self.cgv_booking_mode_var.set(cgv_mode if cgv_mode in {"회원", "비회원"} else "회원")
            cgv_birth = stored_cgv_birth or str(
                config.get("cgv_nonmember_birth", "")
            )
            cgv_phone = stored_cgv_phone or str(
                config.get("cgv_nonmember_phone", "")
            )
            cgv_birth_secret_backed = bool(stored_cgv_birth)
            cgv_phone_secret_backed = bool(stored_cgv_phone)
            self.cgv_nonmember_birth_entry.delete(0, "end")
            self.cgv_nonmember_birth_entry.insert(0, cgv_birth)
            self.cgv_nonmember_phone_entry.delete(0, "end")
            self.cgv_nonmember_phone_entry.insert(0, cgv_phone)
            cgv_password = stored_cgv_password
            if cgv_password:
                self.cgv_nonmember_password_entry.delete(0, "end")
                self.cgv_nonmember_password_entry.insert(0, cgv_password)
            if stored_cgv_npay_password and hasattr(self, "cgv_npay_password_entry"):
                self.cgv_npay_password_entry.delete(0, "end")
                self.cgv_npay_password_entry.insert(0, stored_cgv_npay_password)
            if "cgv_nonmember_birth" in config or "cgv_nonmember_phone" in config:
                if cgv_birth:
                    if self.secret_store.set("cgv_nonmember_birth", cgv_birth):
                        self._secret_baseline["cgv_nonmember_birth"] = cgv_birth
                        cgv_birth_secret_backed = True
                if cgv_phone:
                    if self.secret_store.set("cgv_nonmember_phone", cgv_phone):
                        self._secret_baseline["cgv_nonmember_phone"] = cgv_phone
                        cgv_phone_secret_backed = True
                if "cgv_nonmember_birth" in config and (
                    not cgv_birth or cgv_birth_secret_backed
                ):
                    config.pop("cgv_nonmember_birth", None)
                    config_migrated = True
                if "cgv_nonmember_phone" in config and (
                    not cgv_phone or cgv_phone_secret_backed
                ):
                    config.pop("cgv_nonmember_phone", None)
                    config_migrated = True
            self._on_cgv_booking_mode_change()
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
                update_json(
                    "config.json",
                    lambda current: _merge_config_migration(
                        current, migration_baseline, config
                    ),
                    {},
                )
        except Exception:
            raise
        finally:
            self._is_initializing = False

    def _current_config_values(self, site_name):
        return {
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
            "dpsnnn_threads": self.dpsnnn_threads,
            "cgv_threads": self.cgv_threads,
            "cgv_selection": dict(self.cgv_selection),
            "cgv_booking_mode": self.cgv_booking_mode_var.get(),
            "is_async": self.engine_mode_btn.get() == STANDARD_MODE,
            "engine_mode": self.engine_mode_btn.get(),
            "show_server_time": bool(self.show_server_time_checkbox.get()),
            "remember_personal_info": bool(self.remember_personal_var.get()),
            "catalog_auto_refresh": bool(self.catalog_auto_refresh_var.get()),
            "selected_branch_id": self._selected_branch_id(),
            "selected_theme_id": self._selected_theme_id(),
            "yescaptcha_enabled": bool(self.yescaptcha_enabled_var.get()),
            "yescaptcha_test_mode": bool(self.yescaptcha_test_mode_var.get()),
            "yescaptcha_soft_id": (
                self.yescaptcha_soft_id_entry.get().strip() or DEFAULT_SOFT_ID
            ),
        }

    def save_config(self, site_name):
        try:
            # Sync active memory thread counts before saving
            if self.engine_mode_btn.get() in {NAVER_MODE, TRIPCOM_MODE}:
                self.naver_threads = 1
            elif self._site_uses_keyescape():
                self.keyescape_threads = max(
                    1, min(int(self.threads_slider.get()), 3)
                )
            elif self._site_uses_dpsnnn():
                self.dpsnnn_threads = max(
                    1,
                    min(int(self.threads_slider.get()), DPSNNN_MAX_WORKERS),
                )
            elif self._site_uses_cgv():
                self.cgv_threads = max(
                    1, min(int(self.threads_slider.get()), CGV_MAX_WORKERS)
                )
            else:
                limit = ReservationForm._standard_thread_limit(self)
                self.standard_threads = max(
                    1, min(int(self.threads_slider.get()), limit)
                )

            remember_personal = bool(self.remember_personal_var.get())
            self._persist_secret_if_changed(
                "reservation_name",
                self.name_entry.get().strip() if remember_personal else "",
            )
            self._persist_secret_if_changed(
                "reservation_phone",
                self.phone_entry.get().strip() if remember_personal else "",
            )
            cgv_password = self.cgv_nonmember_password_entry.get()
            cgv_birth = self.cgv_nonmember_birth_entry.get().strip()
            cgv_phone = self.cgv_nonmember_phone_entry.get().strip()
            self._persist_secret_if_changed("cgv_nonmember_password", cgv_password)
            self._persist_secret_if_changed("cgv_nonmember_birth", cgv_birth)
            self._persist_secret_if_changed("cgv_nonmember_phone", cgv_phone)
            if hasattr(self, "cgv_npay_password_entry"):
                cgv_npay_password = self.cgv_npay_password_entry.get().strip()
                self._persist_secret_if_changed(
                    "cgv_npay_password", cgv_npay_password
                )

            yescaptcha_key = self.yescaptcha_client_key_entry.get().strip()
            loaded_yescaptcha_key = self._secret_baseline.get(
                YESCAPTCHA_SECRET_KEY, ""
            )
            yescaptcha_key_edited = yescaptcha_key != loaded_yescaptcha_key
            (
                winning_yescaptcha_key,
                self._yescaptcha_secret_backed,
                yescaptcha_secret_write_failed,
            ) = _persist_yescaptcha_secret(
                self.secret_store,
                yescaptcha_key,
                loaded_yescaptcha_key,
                self._yescaptcha_secret_backed,
            )
            if not yescaptcha_secret_write_failed:
                if winning_yescaptcha_key != yescaptcha_key:
                    self.yescaptcha_client_key_entry.delete(0, "end")
                    if winning_yescaptcha_key:
                        self.yescaptcha_client_key_entry.insert(
                            0, winning_yescaptcha_key
                        )
                yescaptcha_key = winning_yescaptcha_key
                self._secret_baseline[YESCAPTCHA_SECRET_KEY] = yescaptcha_key
            yescaptcha_plaintext_fallback = (
                yescaptcha_key
                if yescaptcha_key
                and (not self._yescaptcha_secret_backed or yescaptcha_secret_write_failed)
                else ""
            )
            if yescaptcha_plaintext_fallback:
                yescaptcha_plaintext_directive = yescaptcha_plaintext_fallback
                yescaptcha_plaintext_expected = (
                    None
                    if yescaptcha_key_edited
                    else loaded_yescaptcha_key
                )
                yescaptcha_plaintext_remove = None
            elif not yescaptcha_secret_write_failed and (
                self._yescaptcha_secret_backed
                or yescaptcha_key_edited
            ):
                yescaptcha_plaintext_directive = None
                yescaptcha_plaintext_expected = None
                yescaptcha_plaintext_remove = loaded_yescaptcha_key or None
            else:
                yescaptcha_plaintext_directive = None
                yescaptcha_plaintext_expected = None
                yescaptcha_plaintext_remove = None

            config = self._current_config_values(site_name)
            config_baseline = dict(getattr(self, "_config_baseline", {}))

            def merge(existing):
                return _merge_form_config(
                    existing,
                    config,
                    config_baseline,
                    plaintext_yescaptcha_key=yescaptcha_plaintext_directive,
                    plaintext_yescaptcha_expected=yescaptcha_plaintext_expected,
                    remove_plaintext_yescaptcha_key=yescaptcha_plaintext_remove,
                )

            update_json("config.json", merge, {})
            self._config_baseline = config
        except (OSError, RuntimeError, TimeoutError, ValueError) as exc:
            if hasattr(self.master, "log_panel"):
                self.master.log_panel.append_log(f"설정을 저장하지 못했습니다: {exc}", "warning")

    def _selected_theme_id(self):
        branch_id = self.config.get("branches", {}).get(self.branch_var.get(), "")
        return self._theme_id_for_name(branch_id, self.theme_var.get())

    def _selected_branch_id(self):
        if self._site_uses_cgv():
            return str(self.cgv_selection.get("site_no", ""))
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
        if self.current_site == "Trip.com 핫딜":
            return str(self.config.get("themes", {}).get(branch_id, {}).get(theme_name, ""))
        if self.current_site in self.custom_sites:
            return str(self.config.get("themes", {}).get(branch_id, {}).get(theme_name, ""))
        return str(JIGUBYEOL_THEMES.get(branch_id, {}).get(theme_name, ""))
