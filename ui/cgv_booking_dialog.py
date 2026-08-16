from __future__ import annotations

import queue
import re
import threading
import time
import webbrowser
from collections import defaultdict
from datetime import datetime, timedelta
from typing import Any, Callable, Mapping

import customtkinter as ctk

import ui.theme as theme
from engines.cgv_browser_client import CgvBrowserClient, CgvRequestCancelled
from engines.cgv_client import (
    CgvSeat,
    build_seat_guide,
    can_extend_contiguous_seat_group,
    can_extend_physical_seat_group,
    choose_recommended_seat_group,
    is_contiguous_seat_group,
    normalize_time,
    parse_seat_groups,
    recommend_cgv_seats,
    seat_layout_columns,
    seat_row_sort_key,
)
from ui.scrollable import SafeScrollableFrame


def _movie_name(item: Mapping[str, Any]) -> str:
    return str(item.get("expoProdNm") or item.get("movNm") or item.get("prodNm") or "").strip()


def _auditorium_name(item: Mapping[str, Any]) -> str:
    return str(item.get("expoScnsNm") or item.get("scnsNm") or "").strip()


def _format_name(item: Mapping[str, Any]) -> str:
    return str(item.get("movkndDsplEnm") or item.get("movkndDsplNm") or "").strip()


def _format_time_display(raw_time: str) -> str:
    norm = normalize_time(raw_time)
    if len(norm) == 4:
        return f"{norm[:2]}:{norm[2:]}"
    return raw_time


def _is_dialog_alive(dialog: Any) -> bool:
    if getattr(dialog, "_closing", False):
        return False
    if hasattr(dialog, "winfo_exists"):
        try:
            return bool(dialog.winfo_exists())
        except Exception:
            return False
    return True


class CgvBookingDialog(ctk.CTkToplevel):
    """CGV-specific, data-driven theater/screening/seat selector with IMAX filtering and pre-open candidate support."""

    def __init__(
        self,
        parent,
        *,
        reservation_date: str,
        people: int,
        initial: Mapping[str, Any] | None,
        on_select: Callable[[dict[str, Any]], None],
    ) -> None:
        super().__init__(parent)
        self.on_select = on_select
        self.reservation_date = reservation_date
        self.people = max(1, min(int(people), 8))
        self.initial = dict(initial or {})
        self._closing = False
        self._request_generation = 0
        self._next_task_id = 0
        self._active_task_id: int | None = None
        self._active_task_done: Callable | None = None
        self._active_cancel_event: threading.Event | None = None
        self._pending_task: tuple[str, Callable[[threading.Event | None], Any], Callable[[Any], None]] | None = None
        self._ui_event_queue: queue.Queue = queue.Queue()
        self._task_thread_local = threading.local()
        self.client = CgvBrowserClient(log=self._browser_status)
        self.regions = ()
        self.sites = ()
        self.schedules: tuple[dict[str, Any], ...] = ()
        self.seats: tuple[CgvSeat, ...] = ()
        self.selected_region = ""
        self.selected_site = None
        self.selected_schedule: dict[str, Any] | None = None
        self.preferred_times: list[str] = list(
            self.initial.get("preferred_times")
            or ([self.initial.get("show_time")] if self.initial.get("show_time") else [])
        )
        self.reference_date = reservation_date
        self.reference_only = False
        self.current_seats: set[str] = set()
        self.seat_recommendations = {}
        self.current_guide = None
        self.priority_groups: list[tuple[str, ...]] = [
            group.seats
            for group in parse_seat_groups(str(self.initial.get("seats", "")), self.people)
        ]
        self._is_restoring_initial = bool(
            self.initial.get("site_no") or self.initial.get("movie")
        )

        self.title("CGV IMAX 예매 대상 선택")
        self.geometry("1060x720")
        self.minsize(940, 640)
        self.configure(fg_color=theme.CANVAS_COLOR)
        self.protocol("WM_DELETE_WINDOW", self._close_dialog)
        self.transient(parent.winfo_toplevel())
        self.grab_set()

        self._build_header()
        self._build_toolbar()
        self._build_content()
        self._build_footer()
        self._start_task(
            "CGV IMAX 지점 목록을 불러오고 있습니다...",
            lambda cancel_event: self.client.fetch_catalog(imax_only=True, cancel_event=cancel_event),
            self._catalog_loaded,
        )
        self.after(50, self._poll_task)

    def _is_alive(self) -> bool:
        return _is_dialog_alive(self)

    def _build_header(self) -> None:
        header = ctk.CTkFrame(self, fg_color="transparent")
        header.pack(fill="x", padx=theme.SPACE_5, pady=(theme.SPACE_4, theme.SPACE_2))
        ctk.CTkLabel(
            header,
            text="CGV IMAX 예매 대상",
            font=theme.FONT_DISPLAY,
            text_color=theme.TEXT_PRIMARY,
        ).pack(anchor="w")
        self.status_label = ctk.CTkLabel(
            header,
            text="실제 CGV 데이터를 연결하고 있습니다.",
            font=theme.FONT_LABEL,
            text_color=theme.TEXT_MUTE,
            anchor="w",
            justify="left",
        )
        self.status_label.pack(fill="x", pady=(theme.SPACE_1, 0))

    def _build_toolbar(self) -> None:
        toolbar = ctk.CTkFrame(
            self,
            fg_color=theme.SURFACE_COLOR,
            border_width=1,
            border_color=theme.HAIRLINE_COLOR,
            corner_radius=theme.ROUNDED_MD,
        )
        toolbar.pack(fill="x", padx=theme.SPACE_5, pady=(0, theme.SPACE_3))

        # Left: Target Date controls
        date_group = ctk.CTkFrame(toolbar, fg_color="transparent")
        date_group.pack(side="left", padx=theme.SPACE_3, pady=theme.SPACE_2)
        ctk.CTkLabel(
            date_group,
            text="목표 날짜",
            font=theme.FONT_HEADING,
            text_color=theme.TEXT_PRIMARY,
        ).pack(side="left", padx=(0, theme.SPACE_2))

        self.date_prev_btn = ctk.CTkButton(
            date_group,
            text="◀",
            width=28,
            height=theme.H_CONTROL,
            command=self._prev_date,
            fg_color=theme.ELEVATED_COLOR,
            hover_color=theme.CARD_COLOR,
            text_color=theme.TEXT_BODY,
            corner_radius=theme.ROUNDED_SM,
        )
        self.date_prev_btn.pack(side="left", padx=(0, 2))

        self.date_entry = ctk.CTkEntry(
            date_group,
            width=105,
            height=theme.H_CONTROL,
            fg_color=theme.ELEVATED_COLOR,
            border_color=theme.CONTROL_BORDER,
            text_color=theme.TEXT_PRIMARY,
            font=theme.FONT_BODY_MD,
            justify="center",
        )
        self.date_entry.insert(0, self.reservation_date)
        self.date_entry.pack(side="left", padx=2)
        self.date_entry.bind("<Return>", lambda _e: self._date_entry_committed())
        self.date_entry.bind("<FocusOut>", lambda _e: self._date_entry_committed())

        self.date_next_btn = ctk.CTkButton(
            date_group,
            text="▶",
            width=28,
            height=theme.H_CONTROL,
            command=self._next_date,
            fg_color=theme.ELEVATED_COLOR,
            hover_color=theme.CARD_COLOR,
            text_color=theme.TEXT_BODY,
            corner_radius=theme.ROUNDED_SM,
        )
        self.date_next_btn.pack(side="left", padx=(2, theme.SPACE_2))

        self.date_picker_btn = ctk.CTkButton(
            date_group,
            text="달력",
            width=48,
            height=theme.H_CONTROL,
            command=self._open_calendar_picker,
            fg_color=theme.ELEVATED_COLOR,
            hover_color=theme.CARD_COLOR,
            text_color=theme.TINT_INFO_FG,
            corner_radius=theme.ROUNDED_SM,
        )
        self.date_picker_btn.pack(side="left", padx=(0, theme.SPACE_4))

        # Middle: People controls
        people_group = ctk.CTkFrame(toolbar, fg_color="transparent")
        people_group.pack(side="left", padx=theme.SPACE_3, pady=theme.SPACE_2)
        ctk.CTkLabel(
            people_group,
            text="관람 인원",
            font=theme.FONT_HEADING,
            text_color=theme.TEXT_PRIMARY,
        ).pack(side="left", padx=(0, theme.SPACE_2))

        self.people_minus_btn = ctk.CTkButton(
            people_group,
            text="−",
            width=28,
            height=theme.H_CONTROL,
            command=self._decrement_people,
            fg_color=theme.ELEVATED_COLOR,
            hover_color=theme.CARD_COLOR,
            text_color=theme.TEXT_BODY,
            corner_radius=theme.ROUNDED_SM,
            font=theme.FONT_BODY_MD,
        )
        self.people_minus_btn.pack(side="left", padx=(0, 2))

        self.people_label = ctk.CTkLabel(
            people_group,
            text=f"{self.people}명",
            width=42,
            height=theme.H_CONTROL,
            fg_color=theme.ELEVATED_COLOR,
            text_color=theme.TEXT_PRIMARY,
            font=theme.FONT_BODY_SM,
            corner_radius=theme.ROUNDED_SM,
        )
        self.people_label.pack(side="left", padx=2)

        self.people_plus_btn = ctk.CTkButton(
            people_group,
            text="+",
            width=28,
            height=theme.H_CONTROL,
            command=self._increment_people,
            fg_color=theme.ELEVATED_COLOR,
            hover_color=theme.CARD_COLOR,
            text_color=theme.TEXT_BODY,
            corner_radius=theme.ROUNDED_SM,
            font=theme.FONT_BODY_MD,
        )
        self.people_plus_btn.pack(side="left", padx=(2, theme.SPACE_3))

        # Right: Schedule status badge
        self.target_type_badge = ctk.CTkLabel(
            toolbar,
            text="",
            font=theme.FONT_BODY_SM,
            text_color=theme.TEXT_MUTE,
            fg_color="transparent",
            corner_radius=theme.ROUNDED_SM,
            height=theme.H_BADGE,
        )
        self.target_type_badge.pack(side="right", padx=theme.SPACE_3, pady=theme.SPACE_2)

    def _panel(self, master, title: str):
        panel = ctk.CTkFrame(
            master,
            fg_color=theme.SURFACE_COLOR,
            border_width=1,
            border_color=theme.HAIRLINE_COLOR,
            corner_radius=theme.ROUNDED_LG,
        )
        ctk.CTkLabel(
            panel,
            text=title,
            font=theme.FONT_HEADING,
            text_color=theme.TEXT_PRIMARY,
            anchor="w",
        ).pack(fill="x", padx=theme.SPACE_3, pady=(theme.SPACE_3, theme.SPACE_2))
        return panel

    def _build_content(self) -> None:
        content = ctk.CTkFrame(self, fg_color="transparent")
        self.content = content
        content.pack(fill="both", expand=True, padx=theme.SPACE_5)
        content.columnconfigure(0, weight=2, minsize=180)
        content.columnconfigure(1, weight=3, minsize=260)
        content.columnconfigure(2, weight=8, minsize=500)
        content.rowconfigure(0, weight=1)

        site_panel = self._panel(content, "IMAX 지점")
        site_panel.grid(row=0, column=0, sticky="nsew", padx=(0, theme.SPACE_2))
        search_wrap = ctk.CTkFrame(site_panel, fg_color="transparent")
        search_wrap.pack(fill="x", padx=theme.SPACE_3, pady=(0, theme.SPACE_2))
        self.site_search = ctk.CTkEntry(
            search_wrap,
            placeholder_text="IMAX 지점 검색",
            fg_color=theme.ELEVATED_COLOR,
            border_color=theme.HAIRLINE_COLOR,
            text_color=theme.TEXT_PRIMARY,
            placeholder_text_color=theme.TEXT_MUTE,
            height=theme.H_CONTROL,
            corner_radius=theme.ROUNDED_MD,
        )
        self.site_search.pack(fill="x")
        self.site_search.bind("<KeyRelease>", lambda _event: self._render_sites())
        self.region_menu = ctk.CTkOptionMenu(
            site_panel,
            values=["전체"],
            command=self._region_changed,
            fg_color=theme.ELEVATED_COLOR,
            button_color=theme.ELEVATED_COLOR,
            button_hover_color=theme.CARD_COLOR,
            dropdown_fg_color=theme.SURFACE_COLOR,
            dropdown_hover_color=theme.CARD_COLOR,
            text_color=theme.TEXT_PRIMARY,
            height=theme.H_CONTROL,
            corner_radius=theme.ROUNDED_MD,
            anchor="w",
        )
        self.region_menu.pack(fill="x", padx=theme.SPACE_3, pady=(0, theme.SPACE_2))
        self.site_list = SafeScrollableFrame(
            site_panel, fg_color="transparent", corner_radius=0
        )
        self.site_list.pack(fill="both", expand=True, padx=theme.SPACE_2, pady=(0, theme.SPACE_2))

        schedule_panel = self._panel(content, "영화와 회차 / 시간 우선순위")
        schedule_panel.grid(row=0, column=1, sticky="nsew", padx=theme.SPACE_1)

        self.movie_var = ctk.StringVar(value="영화를 먼저 불러오세요")
        self.movie_menu = ctk.CTkOptionMenu(
            schedule_panel,
            variable=self.movie_var,
            values=["영화를 먼저 불러오세요"],
            command=self._movie_changed,
            fg_color=theme.ELEVATED_COLOR,
            button_color=theme.ELEVATED_COLOR,
            button_hover_color=theme.CARD_COLOR,
            dropdown_fg_color=theme.SURFACE_COLOR,
            dropdown_hover_color=theme.CARD_COLOR,
            text_color=theme.TEXT_PRIMARY,
            height=theme.H_CONTROL,
            corner_radius=theme.ROUNDED_MD,
            anchor="w",
        )
        self.movie_menu.pack(fill="x", padx=theme.SPACE_3, pady=(0, theme.SPACE_2))

        self.auditorium_var = ctk.StringVar(value="상영관을 먼저 불러오세요")
        self.auditorium_menu = ctk.CTkOptionMenu(
            schedule_panel,
            variable=self.auditorium_var,
            values=["상영관을 먼저 불러오세요"],
            command=self._auditorium_changed,
            fg_color=theme.ELEVATED_COLOR,
            button_color=theme.ELEVATED_COLOR,
            button_hover_color=theme.CARD_COLOR,
            dropdown_fg_color=theme.SURFACE_COLOR,
            dropdown_hover_color=theme.CARD_COLOR,
            text_color=theme.TEXT_PRIMARY,
            height=theme.H_CONTROL,
            corner_radius=theme.ROUNDED_MD,
            anchor="w",
        )
        self.auditorium_menu.pack(fill="x", padx=theme.SPACE_3, pady=(0, theme.SPACE_2))

        time_toolbar = ctk.CTkFrame(schedule_panel, fg_color="transparent")
        time_toolbar.pack(fill="x", padx=theme.SPACE_3, pady=(0, theme.SPACE_2))
        ctk.CTkLabel(
            time_toolbar,
            text="희망 시간 선택 (클릭 순서대로 우선순위)",
            font=theme.FONT_LABEL,
            text_color=theme.TEXT_MUTE,
        ).pack(side="left")
        ctk.CTkButton(
            time_toolbar,
            text="시간 초기화",
            command=self._clear_preferred_times,
            width=68,
            height=theme.H_GHOST,
            fg_color="transparent",
            hover_color=theme.CARD_COLOR,
            border_width=1,
            border_color=theme.CONTROL_BORDER,
            text_color=theme.TEXT_BODY,
            corner_radius=theme.ROUNDED_SM,
            font=theme.FONT_CAPTION,
        ).pack(side="right")

        self.schedule_list = SafeScrollableFrame(
            schedule_panel, fg_color="transparent", corner_radius=0
        )
        self.schedule_list.pack(fill="both", expand=True, padx=theme.SPACE_2, pady=(0, theme.SPACE_2))

        seat_panel = self._panel(content, "좌석 우선순위")
        seat_panel.grid(row=0, column=2, sticky="nsew", padx=(theme.SPACE_2, 0))
        self.seat_help = ctk.CTkLabel(
            seat_panel,
            text=(
                f"{self.people}석씩 선택해 우선순위를 추가하세요. "
                + (
                    "같은 열에서 붙어 있는 좌석만 한 묶음으로 저장됩니다. "
                    if self.people > 1 else ""
                )
                + "매진·판매 불가 좌석도 취소표 감시 대상으로 선택할 수 있습니다."
            ),
            font=theme.FONT_LABEL,
            text_color=theme.TEXT_MUTE,
            justify="left",
            wraplength=420,
            anchor="w",
        )
        self.seat_help.pack(fill="x", padx=theme.SPACE_3, pady=(0, theme.SPACE_2))
        guide = ctk.CTkFrame(
            seat_panel,
            fg_color=theme.TINT_RUNNING_BG,
            border_width=1,
            border_color="#6B5A18",
            corner_radius=theme.ROUNDED_MD,
        )
        guide.pack(fill="x", padx=theme.SPACE_3, pady=(0, theme.SPACE_2))
        guide_text = ctk.CTkFrame(guide, fg_color="transparent")
        guide_text.pack(side="left", fill="x", expand=True, padx=theme.SPACE_3, pady=theme.SPACE_2)
        self.guide_title_label = ctk.CTkLabel(
            guide_text,
            text="명당 가이드",
            font=theme.FONT_KR_TITLE,
            text_color=theme.ACCENT_YELLOW,
            anchor="w",
        )
        self.guide_title_label.pack(fill="x")
        self.guide_summary_label = ctk.CTkLabel(
            guide_text,
            text="회차를 고르면 실제 상영관 기준 추천을 표시합니다.",
            font=theme.FONT_KR_BODY,
            text_color=theme.TEXT_BODY,
            anchor="w",
            justify="left",
            wraplength=360,
        )
        self.guide_summary_label.pack(fill="x", pady=(theme.SPACE_1, 0))
        self.guide_detail_button = ctk.CTkButton(
            guide,
            text="자세히",
            command=self._show_seat_guide,
            state="disabled",
            width=58,
            height=theme.H_GHOST,
            fg_color="transparent",
            hover_color="#463C18",
            border_width=1,
            border_color="#806C1A",
            text_color=theme.ACCENT_YELLOW,
            corner_radius=theme.ROUNDED_SM,
            font=theme.FONT_KR_LABEL,
        )
        self.guide_detail_button.pack(side="right", padx=(0, theme.SPACE_2))
        self.load_seats_button = ctk.CTkButton(
            seat_panel,
            text="실제 좌석도 불러오기",
            command=self._load_seats,
            state="disabled",
            fg_color=theme.ELEVATED_COLOR,
            hover_color=theme.CARD_COLOR,
            border_width=1,
            border_color=theme.CONTROL_BORDER,
            text_color=theme.TEXT_PRIMARY,
            height=theme.H_BUTTON,
            corner_radius=theme.ROUNDED_MD,
        )
        self.load_seats_button.pack(fill="x", padx=theme.SPACE_3, pady=(0, theme.SPACE_2))
        self.auto_seat_var = ctk.StringVar(value="명당 자동 선택")
        self.auto_seat_modes = {"명당 자동 선택": ""}
        self.auto_seat_menu = ctk.CTkOptionMenu(
            seat_panel,
            variable=self.auto_seat_var,
            values=["명당 자동 선택"],
            command=self._auto_select_seats,
            state="disabled",
            fg_color=theme.TINT_RUNNING_BG,
            button_color="#574914",
            button_hover_color="#6B5A18",
            dropdown_fg_color=theme.SURFACE_COLOR,
            dropdown_hover_color=theme.CARD_COLOR,
            text_color=theme.ACCENT_YELLOW,
            height=theme.H_CONTROL,
            corner_radius=theme.ROUNDED_MD,
            anchor="w",
            font=theme.FONT_KR_LABEL,
        )
        self.auto_seat_menu.pack(fill="x", padx=theme.SPACE_3, pady=(0, theme.SPACE_2))
        self.seat_list = SafeScrollableFrame(
            seat_panel,
            fg_color=theme.CANVAS_COLOR,
            corner_radius=theme.ROUNDED_MD,
            border_width=1,
            border_color=theme.HAIRLINE_COLOR,
        )
        self.seat_list.pack(fill="both", expand=True, padx=theme.SPACE_3, pady=(0, theme.SPACE_2))
        self._render_seat_placeholder("회차를 선택한 뒤 실제 좌석도를 불러오세요.")

        actions = ctk.CTkFrame(seat_panel, fg_color="transparent")
        actions.pack(fill="x", padx=theme.SPACE_3, pady=(0, theme.SPACE_2))
        self.add_priority_button = ctk.CTkButton(
            actions,
            text="우선순위 추가",
            command=self._add_priority_group,
            state="disabled",
            fg_color=theme.ACCENT_BLUE,
            hover_color=theme.ACCENT_BLUE_HOVER,
            text_color=theme.TEXT_PRIMARY,
            height=theme.H_BUTTON,
            corner_radius=theme.ROUNDED_MD,
        )
        self.add_priority_button.pack(side="left", fill="x", expand=True)
        ctk.CTkButton(
            actions,
            text="초기화",
            command=self._clear_priorities,
            width=74,
            fg_color="transparent",
            hover_color=theme.CARD_COLOR,
            border_width=1,
            border_color=theme.CONTROL_BORDER,
            text_color=theme.TEXT_BODY,
            height=theme.H_BUTTON,
            corner_radius=theme.ROUNDED_MD,
        ).pack(side="left", padx=(theme.SPACE_2, 0))
        self.priority_label = ctk.CTkLabel(
            seat_panel,
            text="",
            font=theme.FONT_LABEL,
            text_color=theme.TEXT_BODY,
            justify="left",
            anchor="w",
        )
        self.priority_label.pack(fill="x", padx=theme.SPACE_3, pady=(0, theme.SPACE_3))
        self._render_priorities()

    def _build_footer(self) -> None:
        footer = ctk.CTkFrame(self, fg_color="transparent")
        footer.pack(fill="x", padx=theme.SPACE_5, pady=theme.SPACE_4)
        ctk.CTkButton(
            footer,
            text="취소",
            command=self._close_dialog,
            width=100,
            fg_color="transparent",
            hover_color=theme.CARD_COLOR,
            border_width=1,
            border_color=theme.CONTROL_BORDER,
            text_color=theme.TEXT_BODY,
            height=theme.H_BUTTON,
            corner_radius=theme.ROUNDED_MD,
        ).pack(side="left")
        self.confirm_button = ctk.CTkButton(
            footer,
            text="선택 완료",
            command=self._confirm,
            state="disabled",
            fg_color=theme.ACCENT_WHITE,
            hover_color=theme.TEXT_BODY,
            text_color=theme.TEXT_DARK,
            height=theme.H_BUTTON,
            corner_radius=theme.ROUNDED_MD,
        )
        self.confirm_button.pack(side="right", fill="x", expand=True, padx=(theme.SPACE_3, 0))

    def _is_alive(self) -> bool:
        if getattr(self, "_closing", False):
            return False
        if hasattr(self, "winfo_exists"):
            try:
                return bool(self.winfo_exists())
            except Exception:
                return False
        return True

    def _close_dialog(self) -> None:
        if getattr(self, "_closing", False):
            return
        self._closing = True
        self._request_generation += 1
        self._pending_task = None
        if getattr(self, "_active_cancel_event", None) is not None:
            self._active_cancel_event.set()
        try:
            self.grab_release()
        except Exception:
            pass
        try:
            self.destroy()
        except Exception:
            pass

    def _prev_date(self) -> None:
        try:
            current = datetime.strptime(self.reservation_date, "%Y-%m-%d").date()
            today = datetime.now().date()
            if current > today:
                self._change_date((current - timedelta(days=1)).isoformat())
        except ValueError:
            pass

    def _next_date(self) -> None:
        try:
            current = datetime.strptime(self.reservation_date, "%Y-%m-%d").date()
            self._change_date((current + timedelta(days=1)).isoformat())
        except ValueError:
            pass

    def _open_calendar_picker(self) -> None:
        from ui.reservation_form import DatePickerDialog

        DatePickerDialog(self, self.reservation_date, self._change_date)

    def _date_entry_committed(self) -> None:
        raw = self.date_entry.get().strip()
        try:
            parsed = datetime.strptime(raw, "%Y-%m-%d").date()
            self._change_date(parsed.isoformat())
        except ValueError:
            self.date_entry.delete(0, "end")
            self.date_entry.insert(0, self.reservation_date)

    def _change_date(self, new_date: str) -> None:
        if getattr(self, "_closing", False):
            return
        if new_date == self.reservation_date:
            return
        self._is_restoring_initial = False
        self.reservation_date = new_date
        self.date_entry.delete(0, "end")
        self.date_entry.insert(0, new_date)
        self._request_generation += 1
        generation = self._request_generation

        self.schedules = ()
        self.selected_schedule = None
        self.preferred_times.clear()
        self.priority_groups.clear()
        self.seats = ()
        self.current_seats.clear()
        self.seat_recommendations = {}
        self.auto_seat_var.set("명당 자동 선택")
        self.auto_seat_menu.configure(values=["명당 자동 선택"], state="disabled")
        self.movie_var.set("시간표를 불러오는 중...")
        self.movie_menu.configure(values=["시간표를 불러오는 중..."])
        self.auditorium_var.set("상영관을 먼저 불러오세요")
        self.auditorium_menu.configure(values=["상영관을 먼저 불러오세요"])
        self.target_type_badge.configure(text="")
        self._render_schedules()
        self._render_seat_placeholder("회차를 선택한 뒤 실제 좌석도를 불러오세요.")
        self.load_seats_button.configure(state="disabled")
        self._update_seat_guide()
        self._update_confirm_state()

        if self.selected_site:
            site_no = self.selected_site.site_no
            req_date = str(new_date)
            self._start_task(
                f"{self.selected_site.label}의 {new_date} 시간표 및 사전선택 후보를 조회하고 있습니다...",
                lambda cancel_event, s=site_no, d=req_date: self.client.fetch_schedule_with_reference(
                    s, d, cancel_event=cancel_event
                ),
                lambda result, g=generation: self._schedule_loaded(result, generation=g),
            )

    def _decrement_people(self) -> None:
        if self.people > 1:
            self._set_people(self.people - 1)

    def _increment_people(self) -> None:
        if self.people < 8:
            self._set_people(self.people + 1)

    def _set_people(self, new_people: int) -> None:
        if new_people == self.people:
            return
        self.people = max(1, min(new_people, 8))
        self.people_label.configure(text=f"{self.people}명")

        # Invalidate incompatible stored seat priority groups
        self.priority_groups = [
            group for group in self.priority_groups
            if len(group) == self.people and is_contiguous_seat_group(group, self.people)
        ]
        self.current_seats.clear()

        self.seat_help.configure(
            text=(
                f"{self.people}석씩 선택해 우선순위를 추가하세요. "
                + (
                    "같은 열에서 붙어 있는 좌석만 한 묶음으로 저장됩니다. "
                    if self.people > 1 else ""
                )
                + "매진·판매 불가 좌석도 취소표 감시 대상으로 선택할 수 있습니다."
            )
        )
        if self.seats:
            options = self._auto_seat_options()
            self.auto_seat_modes = dict(options)
            self.auto_seat_menu.configure(values=list(options), state="normal")
            self.auto_seat_var.set(next(iter(options)))
            self._render_seats()
        self._render_priorities()
        self._update_confirm_state()

    def _launch_task(
        self,
        status: str,
        func: Callable[[threading.Event | None], Any],
        done: Callable[[Any], None],
    ) -> None:
        self._next_task_id += 1
        task_id = self._next_task_id
        cancel_event = threading.Event()

        self._active_task_id = task_id
        self._active_task_done = done
        self._active_cancel_event = cancel_event

        if hasattr(self, "status_label") and hasattr(self.status_label, "configure"):
            self.status_label.configure(text=status, text_color=theme.TINT_INFO_FG)

        def worker(tid: int, ce: threading.Event, f: Callable[[threading.Event | None], Any]) -> None:
            self._task_thread_local.task_id = tid
            try:
                result = f(ce)
            except CgvRequestCancelled:
                self._ui_event_queue.put(("cancelled", tid, None, None))
            except Exception as exc:
                self._ui_event_queue.put(("result", tid, None, str(exc)))
            else:
                self._ui_event_queue.put(("result", tid, result, None))

        threading.Thread(
            target=worker,
            args=(task_id, cancel_event, func),
            name=f"CgvDataDialog-{task_id}",
            daemon=True,
        ).start()

    def _start_task(
        self,
        status: str,
        func: Callable[[threading.Event | None], Any],
        done: Callable[[Any], None],
    ) -> None:
        if getattr(self, "_closing", False):
            return

        if getattr(self, "_active_task_id", None) is not None:
            self._pending_task = (status, func, done)
            if getattr(self, "_active_cancel_event", None) is not None:
                self._active_cancel_event.set()
            if hasattr(self, "status_label") and hasattr(self.status_label, "configure"):
                self.status_label.configure(text=status, text_color=theme.TINT_INFO_FG)
            return

        self._launch_task(status, func, done)

    def _browser_status(self, message: str, level: str = "info") -> None:
        task_id = getattr(self._task_thread_local, "task_id", None)
        self._ui_event_queue.put(("progress", task_id, message, level))

    def _handle_task_error(self, error_message: str) -> None:
        if getattr(self, "_closing", False):
            return
        if hasattr(self, "status_label") and hasattr(self.status_label, "configure"):
            self.status_label.configure(text=error_message, text_color=theme.TINT_ERROR_FG)
        self.schedules = ()
        self.selected_schedule = None
        if hasattr(self, "movie_var") and hasattr(self.movie_var, "set"):
            self.movie_var.set("시간표를 불러오지 못했습니다")
        if hasattr(self, "movie_menu") and hasattr(self.movie_menu, "configure"):
            self.movie_menu.configure(values=["시간표를 불러오지 못했습니다"])
        if hasattr(self, "auditorium_var") and hasattr(self.auditorium_var, "set"):
            self.auditorium_var.set("상영관을 먼저 불러오세요")
        if hasattr(self, "auditorium_menu") and hasattr(self.auditorium_menu, "configure"):
            self.auditorium_menu.configure(values=["상영관을 먼저 불러오세요"])
        self._render_schedules()
        self._render_seat_placeholder("시간표를 불러오지 못했습니다. 다시 시도해주세요.")
        self._update_seat_guide()
        self._update_confirm_state()

    def _poll_task(self) -> None:
        if getattr(self, "_closing", False):
            return

        while True:
            try:
                event = self._ui_event_queue.get_nowait()
            except queue.Empty:
                break

            kind, task_id, payload, extra = event

            if task_id is not None and task_id != getattr(self, "_active_task_id", None):
                # Discard events from obsolete or cancelled workers
                continue

            if kind == "progress":
                message = payload
                level = extra
                color = {
                    "success": theme.TINT_SUCCESS_FG,
                    "warning": theme.ACCENT_YELLOW,
                    "error": theme.TINT_ERROR_FG,
                }.get(level, theme.TINT_INFO_FG)
                if hasattr(self, "status_label") and hasattr(self.status_label, "configure"):
                    self.status_label.configure(text=message, text_color=color)

            elif kind == "cancelled":
                self._finish_active_task(cancelled=True)

            elif kind == "result":
                result = payload
                error = extra
                self._finish_active_task(result=result, error=error)

        if hasattr(self, "after") and not getattr(self, "_closing", False):
            self.after(50, self._poll_task)

    def _finish_active_task(
        self,
        *,
        result: Any = None,
        error: str | None = None,
        cancelled: bool = False,
    ) -> None:
        done = self._active_task_done

        self._active_task_id = None
        self._active_task_done = None
        self._active_cancel_event = None

        if self._pending_task is not None:
            next_status, next_func, next_done = self._pending_task
            self._pending_task = None
            self._launch_task(next_status, next_func, next_done)
            return

        if cancelled:
            return

        if error:
            self._handle_task_error(error)
        elif done:
            try:
                done(result)
            except Exception as exc:
                self._handle_task_error(str(exc))

    def _catalog_loaded(self, snapshot) -> None:
        self.regions = snapshot.regions
        self.sites = snapshot.sites
        region_names = ["전체"] + [f"{region.name}  {region.count}" for region in self.regions]
        self.region_menu.configure(values=region_names)
        self.region_menu.set("전체")
        self.status_label.configure(
            text=f"CGV IMAX 지점 {len(self.sites)}개를 불러왔습니다.",
            text_color=theme.TINT_SUCCESS_FG,
        )
        self._render_sites()
        initial_no = str(self.initial.get("site_no", ""))
        if initial_no:
            site = next((item for item in self.sites if item.site_no == initial_no), None)
            if site:
                self._select_site(site, user_initiated=False)
        elif self.sites:
            # Default to first IMAX site if none selected
            self._select_site(self.sites[0], user_initiated=False)

    def _region_changed(self, value: str) -> None:
        self.selected_region = ""
        clean = re.sub(r"\s+\d+$", "", value).strip()
        region = next((item for item in self.regions if item.name == clean), None)
        if region:
            self.selected_region = region.code
        self._render_sites()

    def _render_sites(self) -> None:
        for child in self.site_list.winfo_children():
            child.destroy()
        query = self.site_search.get().strip().casefold()
        filtered = [
            site for site in self.sites
            if (not self.selected_region or site.region_code == self.selected_region)
            and (not query or query in site.label.casefold())
        ]
        if not filtered:
            ctk.CTkLabel(
                self.site_list,
                text="검색 결과가 없습니다.",
                font=theme.FONT_LABEL,
                text_color=theme.TEXT_MUTE,
            ).pack(pady=theme.SPACE_4)
            return
        for site in filtered:
            selected = self.selected_site and self.selected_site.site_no == site.site_no
            ctk.CTkButton(
                self.site_list,
                text=site.label,
                command=lambda value=site: self._select_site(value, user_initiated=True),
                anchor="w",
                fg_color=theme.TINT_INFO_BG if selected else "transparent",
                hover_color=theme.CARD_COLOR,
                text_color=theme.TINT_INFO_FG if selected else theme.TEXT_BODY,
                height=theme.H_CONTROL,
                corner_radius=theme.ROUNDED_SM,
            ).pack(fill="x", pady=1)

    def _select_site(self, site, *, user_initiated: bool = True) -> None:
        if getattr(self, "_closing", False):
            return
        if user_initiated:
            self._is_restoring_initial = False
            self.preferred_times.clear()
            self.priority_groups.clear()
        self.selected_site = site
        self.selected_schedule = None
        self.schedules = ()
        self.seats = ()
        self.seat_recommendations = {}
        self.current_seats.clear()
        self.auto_seat_var.set("명당 자동 선택")
        self.auto_seat_menu.configure(values=["명당 자동 선택"], state="disabled")
        self.movie_var.set("시간표를 불러오는 중...")
        self.movie_menu.configure(values=["시간표를 불러오는 중..."])
        self.auditorium_var.set("상영관을 먼저 불러오세요")
        self.auditorium_menu.configure(values=["상영관을 먼저 불러오세요"])
        self.target_type_badge.configure(text="")
        self._render_sites()
        self._render_schedules()
        self._render_seat_placeholder("회차를 선택한 뒤 실제 좌석도를 불러오세요.")
        self._update_seat_guide()
        self.load_seats_button.configure(state="disabled")
        self._update_confirm_state()
        self._request_generation += 1
        generation = self._request_generation
        site_no = site.site_no
        req_date = str(self.reservation_date)
        self._start_task(
            f"{site.label}의 시간표 및 사전선택 후보를 조회하고 있습니다...",
            lambda cancel_event, s=site_no, d=req_date: self.client.fetch_schedule_with_reference(
                s, d, cancel_event=cancel_event
            ),
            lambda result, g=generation: self._schedule_loaded(result, generation=g),
        )

    def _schedule_loaded(self, result, *, generation: int | None = None) -> None:
        if getattr(self, "_closing", False):
            return
        if generation is not None and generation != getattr(self, "_request_generation", None):
            return
        schedules, reference_date, reference_only = result
        self.schedules = tuple(schedules)
        self.reference_date = reference_date
        self.reference_only = bool(reference_only)

        # Separate real vs preopen templates
        real_count = sum(not item.get("_pengucroPreopen") for item in self.schedules)
        template_count = sum(bool(item.get("_pengucroPreopen")) for item in self.schedules)

        movies = sorted({_movie_name(item) for item in self.schedules if _movie_name(item)})
        if hasattr(self, "movie_menu") and hasattr(self.movie_menu, "configure"):
            self.movie_menu.configure(values=movies or ["표시할 영화가 없습니다"])
        initial_movie = str(getattr(self, "initial", {}).get("movie", ""))
        is_initial_mov = getattr(self, "_is_restoring_initial", False) and initial_movie in movies
        chosen_movie = initial_movie if is_initial_mov else (movies[0] if movies else "")
        if hasattr(self, "movie_var") and hasattr(self.movie_var, "set"):
            self.movie_var.set(chosen_movie)
        if hasattr(self, "movie_menu") and hasattr(self.movie_menu, "set"):
            self.movie_menu.set(chosen_movie or "표시할 영화가 없습니다")
        if hasattr(self, "_movie_changed"):
            self._movie_changed(chosen_movie, user_initiated=not is_initial_mov)

        if self.reference_only:
            self.status_label.configure(
                text=(
                    f"목표 날짜는 아직 미오픈입니다. 최근 공개 일정({reference_date}) 기준으로 후보를 구성했습니다."
                ),
                text_color=theme.ACCENT_YELLOW,
            )
            self.target_type_badge.configure(
                text=f"미오픈 · 최근 공개 일정 기준 ({reference_date})",
                text_color=theme.ACCENT_YELLOW,
                fg_color=theme.TINT_RUNNING_BG,
            )
        elif real_count > 0:
            open_count = sum(int(item.get("frSeatCnt", 0) or 0) > 0 for item in self.schedules if not item.get("_pengucroPreopen"))
            if open_count > 0:
                self.status_label.configure(
                    text=f"{self.selected_site.label} · 실제 회차 {real_count}개",
                    text_color=theme.TINT_SUCCESS_FG,
                )
                self.target_type_badge.configure(
                    text=f"실제 회차 오픈 ({self.reservation_date})",
                    text_color=theme.TINT_SUCCESS_FG,
                    fg_color=theme.TINT_INFO_BG,
                )
            else:
                self.status_label.configure(
                    text=f"{self.selected_site.label} · 회차 선공개(예매 대기) {real_count}개",
                    text_color=theme.TINT_INFO_FG,
                )
                self.target_type_badge.configure(
                    text=f"선공개 회차 · 예매 대기 ({self.reservation_date})",
                    text_color=theme.TINT_INFO_FG,
                    fg_color=theme.TINT_INFO_BG,
                )
        self._render_schedules()

    @staticmethod
    def _auditorium_option(item: Mapping[str, Any]) -> str:
        auditorium = _auditorium_name(item)
        format_text = _format_name(item)
        return f"{auditorium} · {format_text}" if format_text else auditorium

    def _movie_changed(self, _value: str = "", *, user_initiated: bool = True) -> None:
        if user_initiated:
            self._is_restoring_initial = False
            self.preferred_times.clear()
            self.priority_groups.clear()
            self.selected_schedule = None
            self.seats = ()
            self.current_seats.clear()
            self.seat_recommendations = {}
            self.auto_seat_var.set("명당 자동 선택")
            self.auto_seat_menu.configure(values=["명당 자동 선택"], state="disabled")
            self._render_seat_placeholder("회차를 선택한 뒤 실제 좌석도를 불러오세요.")
            self._update_seat_guide()

        movie = self.movie_var.get()
        options = sorted(
            {
                self._auditorium_option(item)
                for item in self.schedules
                if _movie_name(item) == movie and self._auditorium_option(item)
            }
        )
        initial_auditorium = str(self.initial.get("auditorium", "")).strip()
        initial_format = str(self.initial.get("format", "")).strip()

        chosen = ""
        is_initial_aud = False

        if self._is_restoring_initial and initial_auditorium:
            # 1. Exact match for auditorium + format if format is stored
            if initial_format:
                exact_target = f"{initial_auditorium} · {initial_format}"
                if exact_target in options:
                    chosen = exact_target
                    is_initial_aud = True
                else:
                    matching = next(
                        (
                            opt for opt in options
                            if opt.startswith(initial_auditorium) and initial_format in opt
                        ),
                        "",
                    )
                    if matching:
                        chosen = matching
                        is_initial_aud = True

            # 2. Legacy fallback: auditorium-only match if format is missing or not matched
            if not chosen:
                matching = next(
                    (opt for opt in options if opt.startswith(initial_auditorium)),
                    "",
                )
                if matching:
                    chosen = matching
                    is_initial_aud = True

        if not chosen:
            chosen = options[0] if options else ""

        self.auditorium_menu.configure(values=options or ["표시할 상영관이 없습니다"])
        self.auditorium_var.set(chosen)
        if hasattr(self, "auditorium_menu") and hasattr(self.auditorium_menu, "set"):
            self.auditorium_menu.set(chosen or "표시할 상영관이 없습니다")
        self._auditorium_changed(chosen, user_initiated=not is_initial_aud)

    def _auditorium_changed(self, _value: str = "", *, user_initiated: bool = True) -> None:
        if user_initiated:
            self._is_restoring_initial = False
            self.preferred_times.clear()
            self.priority_groups.clear()
            self.selected_schedule = None
            self.seats = ()
            self.current_seats.clear()
            self.seat_recommendations = {}
            self.auto_seat_var.set("명당 자동 선택")
            self.auto_seat_menu.configure(values=["명당 자동 선택"], state="disabled")
            self._render_seat_placeholder("회차를 선택한 뒤 실제 좌석도를 불러오세요.")
            self._update_seat_guide()

        self.selected_schedule = None
        self.load_seats_button.configure(state="disabled")
        self._render_schedules()
        self._update_confirm_state()
        if not user_initiated:
            self._is_restoring_initial = False

    def _clear_preferred_times(self) -> None:
        self.preferred_times.clear()
        self.selected_schedule = None
        self.load_seats_button.configure(state="disabled")
        self._render_schedules()
        self._update_confirm_state()

    def _render_schedules(self) -> None:
        for child in self.schedule_list.winfo_children():
            child.destroy()
        movie = self.movie_var.get()
        auditorium = self.auditorium_var.get()
        items = [
            item for item in self.schedules
            if _movie_name(item) == movie and self._auditorium_option(item) == auditorium
        ]
        if not items:
            ctk.CTkLabel(
                self.schedule_list,
                text="지점을 선택하면 실제 영화와 회차가 표시됩니다.",
                font=theme.FONT_LABEL,
                text_color=theme.TEXT_MUTE,
                wraplength=280,
                justify="left",
            ).pack(pady=theme.SPACE_4)
            return

        for item in sorted(items, key=lambda value: normalize_time(value.get("scnsrtTm"))):
            raw_time = normalize_time(item.get("scnsrtTm"))
            time_text = _format_time_display(raw_time)
            auditorium_text = _auditorium_name(item)
            format_text = _format_name(item)
            is_preopen = bool(item.get("_pengucroPreopen"))
            try:
                remaining = int(item.get("frSeatCnt", 0) or 0)
            except (TypeError, ValueError):
                remaining = 0

            observed_dates = item.get("_pengucroObservedDates", ())
            observed_count = len(observed_dates) if isinstance(observed_dates, (tuple, list)) else 0

            if is_preopen:
                dates_info = f"최근 {observed_count}일 관측" if observed_count > 0 else "최근 일정 기준"
                status_label = f"미오픈 ({dates_info}) · 사전선택 대기"
                status_color = theme.ACCENT_YELLOW
            elif remaining > 0:
                status_label = f"실제 회차 · 잔여 {remaining}석"
                status_color = theme.TINT_SUCCESS_FG
            else:
                seat_reference_date = str(item.get("_pengucroSeatReferenceDate", ""))
                ref_info = f" · 좌석 기준 {seat_reference_date}" if seat_reference_date else ""
                status_label = f"선공개 회차{ref_info} · 예매 대기 가능"
                status_color = theme.TINT_INFO_FG

            is_selected = time_text in self.preferred_times
            priority_badge = ""
            if is_selected:
                priority_index = self.preferred_times.index(time_text) + 1
                priority_badge = f"[{priority_index}순위] "

            btn_text = (
                f"{priority_badge}{time_text}  {auditorium_text}\n"
                f"{format_text} · {status_label}"
            )
            ctk.CTkButton(
                self.schedule_list,
                text=btn_text,
                command=lambda value=item: self._toggle_schedule_time(value),
                anchor="w",
                fg_color=theme.TINT_INFO_BG if is_selected else theme.ELEVATED_COLOR,
                hover_color=theme.CARD_COLOR,
                text_color=theme.TINT_INFO_FG if is_selected else theme.TEXT_BODY,
                height=48,
                corner_radius=theme.ROUNDED_MD,
                border_width=1 if is_selected else 0,
                border_color=theme.ACCENT_BLUE if is_selected else theme.CONTROL_BORDER,
            ).pack(fill="x", pady=theme.SPACE_1)

    def _toggle_schedule_time(self, item: dict[str, Any]) -> None:
        raw_time = normalize_time(item.get("scnsrtTm"))
        time_text = _format_time_display(raw_time)

        if time_text in self.preferred_times:
            self.preferred_times.remove(time_text)
            if not self.preferred_times:
                self.selected_schedule = None
        else:
            self.preferred_times.append(time_text)
            self.selected_schedule = item

        self._render_schedules()

        if self.preferred_times:
            times_display = " → ".join(self.preferred_times)
            self.status_label.configure(
                text=f"선택한 시간 우선순위: {times_display}",
                text_color=theme.TINT_SUCCESS_FG,
            )
            self.load_seats_button.configure(state="normal")
            if not self.seats:
                if self.reference_only or (self.selected_schedule and self.selected_schedule.get("_pengucroPreopen")):
                    placeholder = (
                        f"시간 우선순위({times_display}) 선택 완료.\n"
                        "상영관의 최근 좌석도를 불러와 좌석 우선순위를 지정하세요."
                    )
                else:
                    placeholder = f"시간 우선순위({times_display}) 선택 완료. 좌석도를 불러오세요."
                self._render_seat_placeholder(placeholder)
        else:
            self.load_seats_button.configure(state="disabled")
            self._render_seat_placeholder("회차 또는 희망 시간을 선택한 뒤 실제 좌석도를 불러오세요.")

        self._update_seat_guide()
        self._update_confirm_state()

    @classmethod
    def _seat_reference_schedule(cls, self) -> dict[str, Any]:
        selected = getattr(self, "selected_schedule", None) or {}
        embedded_reference = selected.get("_pengucroSeatReference")
        if isinstance(embedded_reference, Mapping) and embedded_reference:
            return dict(embedded_reference)
        try:
            if int(selected.get("frSeatCnt", 0) or 0) > 0 and not selected.get("_pengucroPreopen"):
                return selected
        except (TypeError, ValueError):
            pass
        screen_no = str(selected.get("scnsNo", ""))
        schedules = getattr(self, "schedules", ())
        return next(
            (
                item for item in schedules
                if str(item.get("scnsNo", "")) == screen_no
                and int(item.get("frSeatCnt", 0) or 0) > 0
                and not item.get("_pengucroPreopen")
            ),
            selected,
        )

    def _load_seats(self) -> None:
        if getattr(self, "_closing", False):
            return
        if not self.selected_schedule and not self.preferred_times:
            return
        reference = self._seat_reference_schedule(self)
        people = int(self.people)
        self._request_generation += 1
        generation = self._request_generation
        self._start_task(
            "실제 CGV 좌석도를 여는 중입니다. 로그인 안내가 뜨면 열린 Chrome에서 로그인해주세요.",
            lambda cancel_event, r=reference, p=people: self.client.fetch_seat_map(
                r, p, cancel_event=cancel_event
            ),
            lambda seats, g=generation: self._seats_loaded(seats, generation=g),
        )

    def _seats_loaded(self, seats, *, generation: int | None = None) -> None:
        if getattr(self, "_closing", False):
            return
        if generation is not None and generation != getattr(self, "_request_generation", None):
            return
        self.seats = tuple(seats)
        schedule = self.selected_schedule or {}
        self.seat_recommendations = recommend_cgv_seats(
            self.seats,
            site_no=self.selected_site.site_no if self.selected_site else "",
            auditorium=_auditorium_name(schedule),
            format_name=_format_name(schedule),
        )
        unavailable = sum(not seat.available for seat in self.seats)
        self.status_label.configure(
            text=(
                f"실제 좌석 {len(self.seats)}석 · 현재 판매 불가 {unavailable}석 · "
                "모든 물리 좌석을 우선순위로 선택할 수 있습니다."
            ),
            text_color=theme.TINT_SUCCESS_FG,
        )
        self._update_seat_guide()
        options = self._auto_seat_options()
        self.auto_seat_modes = dict(options)
        self.auto_seat_menu.configure(values=list(options), state="normal")
        self.auto_seat_var.set(next(iter(options)))
        self._render_seats()
        self.after_idle(self._fit_window_to_seat_map)

    def _auto_seat_options(self) -> dict[str, str]:
        if self.current_guide and self.current_guide.dedicated:
            return {
                "명당 자동 선택": "",
                f"균형 최우선 · H열 중앙 {self.people}석": "balanced",
                f"몰입형 · F–G열 중앙 {self.people}석": "immersive",
                f"편안형 · I–J열 중앙 {self.people}석": "comfortable",
                f"추천 등급순 · 중앙 {self.people}석": "best",
            }
        return {
            "명당 자동 선택": "",
            f"최우선 중앙 명당 {self.people}석": "best",
            f"추천 중앙 구역 {self.people}석": "recommended",
            f"취향 추천 구역 {self.people}석": "preference",
        }

    def _auto_select_seats(self, label: str) -> None:
        mode = self.auto_seat_modes.get(label, "")
        if not mode or not self.seats:
            return
        group = choose_recommended_seat_group(
            self.seats,
            self.seat_recommendations,
            self.people,
            mode=mode,
            excluded=self.priority_groups,
        )
        if not group:
            self.current_seats.clear()
            self.add_priority_button.configure(state="disabled")
            self.status_label.configure(
                text=f"{label}에 맞는 {self.people}석 연속 좌석 묶음을 찾지 못했습니다.",
                text_color=theme.ACCENT_YELLOW,
            )
            self._render_seats()
            return
        self.current_seats = set(group)
        self.add_priority_button.configure(state="normal")
        self.status_label.configure(
            text=(
                f"{label} 기준으로 {', '.join(group)} 좌석을 골랐습니다. "
                "확인 후 우선순위 추가를 누르세요."
            ),
            text_color=theme.TINT_SUCCESS_FG,
        )
        self._render_seats()

    def _fit_window_to_seat_map(self) -> None:
        if not self.seats:
            return
        columns = seat_layout_columns(self.seats)
        map_width = (max(columns.values(), default=0) + 2) * 24 + 74
        row_count = len({seat.row for seat in self.seats})
        screen_width = max(1024, self.winfo_screenwidth())
        screen_height = max(720, self.winfo_screenheight())
        desired_width = min(screen_width - 32, max(1060, 500 + min(map_width, 1120)))
        desired_height = min(screen_height - 56, max(720, 330 + row_count * 25))
        x = max(8, (screen_width - desired_width) // 2)
        y = max(8, (screen_height - desired_height) // 2)
        self.geometry(f"{desired_width}x{desired_height}+{x}+{y}")

    def _update_seat_guide(self) -> None:
        schedule = self.selected_schedule
        if not schedule or not self.selected_site:
            self.current_guide = None
            if hasattr(self, "guide_title_label"):
                self.guide_title_label.configure(text="명당 가이드")
                self.guide_summary_label.configure(
                    text="회차를 고르면 실제 상영관 기준 추천을 표시합니다."
                )
                self.guide_detail_button.configure(state="disabled")
            return
        self.current_guide = build_seat_guide(
            site_no=self.selected_site.site_no,
            auditorium=_auditorium_name(schedule),
            format_name=_format_name(schedule),
        )
        self.guide_title_label.configure(text=self.current_guide.title)
        suffix = " · 실제 좌석 중앙값 적용" if self.seats else ""
        self.guide_summary_label.configure(text=f"{self.current_guide.summary}{suffix}")
        self.guide_detail_button.configure(state="normal")

    def _show_seat_guide(self) -> None:
        guide = self.current_guide
        if guide is None:
            return
        dialog = ctk.CTkToplevel(self)
        dialog.title(guide.title)
        dialog.geometry("620x430")
        dialog.minsize(560, 390)
        dialog.configure(fg_color=theme.CANVAS_COLOR)
        dialog.transient(self)
        dialog.grab_set()
        ctk.CTkLabel(
            dialog,
            text=guide.title,
            font=theme.FONT_KR_DISPLAY,
            text_color=theme.TEXT_PRIMARY,
            anchor="w",
        ).pack(fill="x", padx=theme.SPACE_5, pady=(theme.SPACE_5, theme.SPACE_2))
        ctk.CTkLabel(
            dialog,
            text=guide.summary,
            font=theme.FONT_KR_TITLE,
            text_color=theme.ACCENT_YELLOW,
            anchor="w",
            justify="left",
            wraplength=560,
        ).pack(fill="x", padx=theme.SPACE_5)
        detail_frame = ctk.CTkFrame(
            dialog,
            fg_color=theme.SURFACE_COLOR,
            border_width=1,
            border_color=theme.HAIRLINE_COLOR,
            corner_radius=theme.ROUNDED_LG,
        )
        detail_frame.pack(fill="both", expand=True, padx=theme.SPACE_5, pady=theme.SPACE_4)
        for detail in guide.details:
            ctk.CTkLabel(
                detail_frame,
                text=f"• {detail}",
                font=theme.FONT_KR_BODY,
                text_color=theme.TEXT_BODY,
                anchor="w",
                justify="left",
                wraplength=530,
            ).pack(fill="x", padx=theme.SPACE_4, pady=(theme.SPACE_2, 0))
        ctk.CTkLabel(
            detail_frame,
            text="관람 후기 기반 참고 정보이며 영화 화면비와 개인 취향에 따라 달라질 수 있습니다.",
            font=theme.FONT_KR_LABEL,
            text_color=theme.TEXT_MUTE,
            anchor="w",
            wraplength=530,
        ).pack(fill="x", padx=theme.SPACE_4, pady=(theme.SPACE_3, theme.SPACE_1))
        source_row = ctk.CTkFrame(detail_frame, fg_color="transparent")
        source_row.pack(fill="x", padx=theme.SPACE_4, pady=(0, theme.SPACE_3))
        for label, url in guide.sources:
            ctk.CTkButton(
                source_row,
                text=label,
                command=lambda target=url: webbrowser.open(target),
                height=theme.H_GHOST,
                fg_color=theme.ELEVATED_COLOR,
                hover_color=theme.CARD_COLOR,
                border_width=1,
                border_color=theme.CONTROL_BORDER,
                text_color=theme.TINT_INFO_FG,
                corner_radius=theme.ROUNDED_SM,
                font=theme.FONT_KR_LABEL,
            ).pack(side="left", padx=(0, theme.SPACE_1))
        ctk.CTkButton(
            dialog,
            text="닫기",
            command=dialog.destroy,
            fg_color=theme.ACCENT_WHITE,
            hover_color=theme.TEXT_BODY,
            text_color=theme.TEXT_DARK,
            height=theme.H_BUTTON,
            corner_radius=theme.ROUNDED_MD,
        ).pack(fill="x", padx=theme.SPACE_5, pady=(0, theme.SPACE_5))

    def _render_seat_placeholder(self, text: str) -> None:
        for child in self.seat_list.winfo_children():
            child.destroy()
        ctk.CTkLabel(
            self.seat_list,
            text=text,
            font=theme.FONT_LABEL,
            text_color=theme.TEXT_MUTE,
            wraplength=400,
            justify="center",
        ).pack(expand=True, padx=theme.SPACE_3, pady=theme.SPACE_5)

    def _render_seats(self) -> None:
        scroll_x = 0.0
        previous_scroller = getattr(self, "_seat_map_scroller", None)
        if previous_scroller is not None:
            try:
                scroll_x = previous_scroller._parent_canvas.xview()[0]
            except Exception:
                pass
        for child in self.seat_list.winfo_children():
            child.destroy()
        rows: dict[str, list[CgvSeat]] = defaultdict(list)
        for seat in self.seats:
            rows[seat.row].append(seat)
        row_names = sorted(rows, key=seat_row_sort_key)
        columns = seat_layout_columns(self.seats)
        max_column = max(columns.values(), default=0)
        map_height = max(190, 92 + len(row_names) * 25)
        self._seat_map_scroller = SafeScrollableFrame(
            self.seat_list,
            orientation="horizontal",
            height=map_height,
            fg_color=theme.CANVAS_COLOR,
            corner_radius=0,
        )
        self._seat_map_scroller.pack(fill="x", expand=True, padx=theme.SPACE_1, pady=theme.SPACE_1)
        map_frame = ctk.CTkFrame(self._seat_map_scroller, fg_color=theme.CANVAS_COLOR)
        map_frame.pack(fill="y", padx=theme.SPACE_2, pady=(theme.SPACE_1, theme.SPACE_2))
        map_frame.grid_columnconfigure(0, minsize=32)
        for column in range(1, max_column + 2):
            map_frame.grid_columnconfigure(column, minsize=24)

        ctk.CTkLabel(
            map_frame,
            text="SCREEN",
            font=theme.FONT_CAPTION,
            text_color="#9DA0A6",
            fg_color="#35363A",
            corner_radius=theme.ROUNDED_SM,
            height=17,
        ).grid(
            row=0, column=1, columnspan=max(1, max_column + 1), sticky="ew",
            padx=(theme.SPACE_3, theme.SPACE_3), pady=(0, theme.SPACE_2),
        )
        ctk.CTkLabel(
            map_frame,
            text="하늘색 현재 예매 가능 · 짙은 회색 판매 불가 · 분홍 선택 · 테두리 명당 등급",
            font=theme.FONT_CAPTION,
            text_color=theme.TEXT_MUTE,
            anchor="w",
        ).grid(
            row=1, column=0, columnspan=max_column + 2, sticky="w",
            pady=(0, theme.SPACE_2),
        )
        for row_index, row_name in enumerate(row_names, start=2):
            ctk.CTkLabel(
                map_frame,
                text=row_name,
                width=28,
                height=21,
                font=theme.FONT_BODY_SM,
                text_color="#D6D7DA",
            ).grid(row=row_index, column=0, padx=(0, 3), pady=1)
            for seat in sorted(rows[row_name], key=lambda value: value.number):
                selected = seat.label in self.current_seats
                recommendation = self.seat_recommendations.get(seat.label)
                tier = recommendation.tier if recommendation else ""
                tier_border = {
                    "best": theme.ACCENT_YELLOW,
                    "recommended": theme.TINT_INFO_FG,
                    "preference": theme.TINT_SUCCESS_FG,
                }.get(tier, "#616267")
                ctk.CTkButton(
                    map_frame,
                    text=str(seat.number),
                    width=22,
                    height=20,
                    command=lambda value=seat: self._toggle_seat(value),
                    fg_color=(
                        "#FF5A64" if selected else
                        "#45A8DD" if seat.available else "#45464A"
                    ),
                    hover_color="#FF737B" if selected else "#62B7E4",
                    border_width=1,
                    border_color="#FF8F96" if selected else tier_border,
                    text_color=(
                        theme.TEXT_PRIMARY if (selected or seat.available) else "#B8B9BD"
                    ),
                    corner_radius=4,
                    font=theme.FONT_CAPTION,
                ).grid(
                    row=row_index,
                    column=columns.get(seat.label, seat.number) + 1,
                    padx=1,
                    pady=1,
                )

        def restore_scroll() -> None:
            try:
                self._seat_map_scroller._parent_canvas.xview_moveto(scroll_x)
            except Exception:
                pass

        self.after_idle(restore_scroll)

    def _toggle_seat(self, seat: CgvSeat) -> None:
        if seat.label in self.current_seats:
            self.current_seats.remove(seat.label)
        elif len(self.current_seats) < self.people:
            if can_extend_contiguous_seat_group(
                self.current_seats, seat.label, self.people
            ) and can_extend_physical_seat_group(
                self.seats, self.current_seats, seat.label, self.people
            ):
                self.current_seats.add(seat.label)
            else:
                self.status_label.configure(
                    text=(
                        f"{self.people}명 예매는 같은 열에서 붙어 있는 좌석만 "
                        "한 우선순위로 선택할 수 있습니다."
                    ),
                    text_color=theme.ACCENT_YELLOW,
                )
                return
        self.auto_seat_var.set("명당 자동 선택")
        self.add_priority_button.configure(
            state=(
                "normal"
                if is_contiguous_seat_group(self.current_seats, self.people)
                else "disabled"
            )
        )
        self._render_seats()

    def _add_priority_group(self) -> None:
        if not is_contiguous_seat_group(self.current_seats, self.people):
            return
        group = tuple(
            sorted(
                self.current_seats,
                key=lambda value: (re.sub(r"\d", "", value), int(re.sub(r"\D", "", value) or 0)),
            )
        )
        if group not in self.priority_groups:
            self.priority_groups.append(group)
        self.current_seats.clear()
        self.auto_seat_var.set("명당 자동 선택")
        self.add_priority_button.configure(state="disabled")
        self._render_seats()
        self._render_priorities()

    def _clear_priorities(self) -> None:
        self.priority_groups.clear()
        self.current_seats.clear()
        self.auto_seat_var.set("명당 자동 선택")
        self.add_priority_button.configure(state="disabled")
        if self.seats:
            self._render_seats()
        self._render_priorities()

    def _render_priorities(self) -> None:
        if self.priority_groups:
            text = "  ·  ".join(
                f"{index}. {', '.join(group)}"
                for index, group in enumerate(self.priority_groups, start=1)
            )
        else:
            text = "아직 저장한 좌석 우선순위가 없습니다."
        self.priority_label.configure(
            text=text,
            text_color=theme.TEXT_BODY if self.priority_groups else theme.TEXT_MUTE,
        )
        self._update_confirm_state()

    def _update_confirm_state(self) -> None:
        movie = self.movie_var.get() if hasattr(self, "movie_var") else ""
        auditorium = self.auditorium_var.get() if hasattr(self, "auditorium_var") else ""
        has_valid_movie = bool(
            movie
            and movie not in ("영화를 먼저 불러오세요", "시간표를 불러오는 중...", "표시할 영화가 없습니다")
        )
        has_valid_auditorium = bool(
            auditorium
            and auditorium not in ("상영관을 먼저 불러오세요", "표시할 상영관이 없습니다")
        )
        ready = bool(
            self.selected_site
            and has_valid_movie
            and has_valid_auditorium
            and (self.selected_schedule or self.preferred_times)
            and self.priority_groups
        )
        if hasattr(self, "confirm_button"):
            self.confirm_button.configure(state="normal" if ready else "disabled")

    def _confirm(self) -> None:
        if not self.selected_site or not self.priority_groups:
            return
        movie = self.movie_var.get()
        auditorium_opt = self.auditorium_var.get()
        auditorium = auditorium_opt.split(" · ")[0] if " · " in auditorium_opt else auditorium_opt
        format_name = auditorium_opt.split(" · ")[1] if " · " in auditorium_opt else ""

        preferred_times = list(self.preferred_times)
        show_time = preferred_times[0] if preferred_times else ""

        is_preopen = bool(
            self.reference_only
            or (self.selected_schedule and self.selected_schedule.get("_pengucroPreopen"))
        )

        region_name = next(
            (region.name for region in self.regions if region.code == self.selected_site.region_code),
            "",
        )
        result = {
            "site_no": self.selected_site.site_no,
            "site_name": self.selected_site.label,
            "region": region_name,
            "date": self.reservation_date,
            "people": self.people,
            "movie": movie,
            "auditorium": auditorium,
            "format": format_name,
            "show_time": show_time,
            "preferred_times": preferred_times,
            "is_preopen": is_preopen,
            "reference_date": self.reference_date,
            "reference_only": self.reference_only,
            "scns_no": "" if is_preopen else str((self.selected_schedule or {}).get("scnsNo", "")),
            "seats": " | ".join(",".join(group) for group in self.priority_groups),
            "seat_groups": [list(group) for group in self.priority_groups],
        }
        self.on_select(result)
        self._close_dialog()
