from __future__ import annotations

import re

import customtkinter as ctk

import ui.theme as theme
from engines.cgv_client import is_contiguous_seat_group, normalize_seat_name
from ui.cgv_booking_dialog import CgvBookingDialog as BaseCgvBookingDialog


MAX_VISIBLE_PRIORITY_CHIPS = 6


def parse_manual_seat_priorities(raw: str, people: int) -> list[tuple[str, ...]]:
    """Parse user-entered CGV seat groups without requiring a live seat map."""

    count = max(1, min(int(people), 8))
    text = str(raw or "").strip()
    if not text:
        return []

    chunks = [part.strip() for part in re.split(r"[|;\n]+", text) if part.strip()]
    parsed: list[tuple[str, ...]] = []
    for chunk in chunks:
        tokens = [token for token in re.split(r"[\s,]+", chunk) if token]
        if len(tokens) != count:
            raise ValueError(f"좌석 묶음마다 정확히 {count}석을 입력해주세요.")

        seats: list[str] = []
        for token in tokens:
            match = re.fullmatch(r"([A-Za-z가-힣]+)\s*[-_]?\s*0*([0-9]+)", token.strip())
            if not match:
                raise ValueError(f"좌석 형식을 확인해주세요: {token}")
            row, number = match.groups()
            if row.isascii():
                row = row.upper()
            seats.append(normalize_seat_name(f"{row}{int(number)}"))

        group = tuple(seats)
        if len(set(group)) != count:
            raise ValueError("같은 좌석을 한 묶음에 중복 입력할 수 없습니다.")
        if not is_contiguous_seat_group(group, count):
            raise ValueError(
                f"{count}명 예매는 같은 열의 연속 좌석으로 입력해주세요."
            )
        parsed.append(group)

    return parsed


class CgvBookingDialog(BaseCgvBookingDialog):
    """CGV selector with large seat map, visible priorities and manual seats."""

    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self._expand_dialog_for_seat_map()
        self._hide_visual_seat_guide()
        self._install_manual_seat_controls()
        self._install_priority_summary()
        self._refresh_manual_seat_ui()
        self._render_priority_summary()

    def _expand_dialog_for_seat_map(self) -> None:
        try:
            self.update_idletasks()
            geometry = str(self.geometry())
            match = re.fullmatch(r"(\d+)x(\d+)\+(-?\d+)\+(-?\d+)", geometry)
            if not match:
                return
            width, height, pos_x, pos_y = (int(value) for value in match.groups())
            screen_height = int(self.winfo_screenheight())
            target_height = min(760, max(height, screen_height - 60))
            if target_height <= height:
                return
            lift = (target_height - height) // 2
            self.geometry(
                f"{width}x{target_height}+{max(0, pos_x)}+{max(0, pos_y - lift)}"
            )
            self.minsize(900, 560)
        except Exception:
            pass

    def _hide_visual_seat_guide(self) -> None:
        title = getattr(self, "guide_title_label", None)
        guide_text = getattr(title, "master", None)
        guide = getattr(guide_text, "master", None)
        if guide is not None:
            try:
                guide.pack_forget()
            except Exception:
                pass

    @classmethod
    def _seat_reference_schedule(cls, self):
        """Use the exact target-day schedule whenever it is already open.

        Historical seat references are only a pre-open aid. The base selector
        preferred an embedded previous-day reference even when the selected
        target-day row had a real ``scnsNo``, which made 2026-08-24 open a
        2026-08-23 seat page. Real schedules must always win; only synthetic
        pre-open rows may fall back to the historical layout reference.
        """

        selected = getattr(self, "selected_schedule", None) or {}
        if selected and not bool(selected.get("_pengucroPreopen")):
            if str(selected.get("scnsNo", "") or ""):
                return selected
        return BaseCgvBookingDialog._seat_reference_schedule(self)

    def _install_manual_seat_controls(self) -> None:
        seat_panel = self.load_seats_button.master

        self.load_seats_button.configure(text="좌석도 불러오기 · 실제 배치 확인")
        self.add_priority_button.configure(text="선택한 좌석 우선순위 추가")
        self.seat_help.configure(
            text=(
                f"{self.people}석씩 우선순위를 저장하세요. 미오픈 날짜는 빠른 입력을 사용할 수 있고, "
                "실제 회차가 열리면 선택한 순서대로 자동 확인합니다."
            )
        )

        self.manual_seat_frame = ctk.CTkFrame(
            seat_panel,
            fg_color=theme.ELEVATED_COLOR,
            border_width=1,
            border_color=theme.CONTROL_BORDER,
            corner_radius=theme.ROUNDED_MD,
        )
        self.manual_seat_frame.pack(
            fill="x",
            padx=theme.SPACE_3,
            pady=(0, theme.SPACE_1),
            after=self.load_seats_button,
        )

        title_row = ctk.CTkFrame(self.manual_seat_frame, fg_color="transparent")
        title_row.pack(fill="x", padx=theme.SPACE_2, pady=(theme.SPACE_2, theme.SPACE_1))
        ctk.CTkLabel(
            title_row,
            text="빠른 좌석 입력",
            font=theme.FONT_HEADING,
            text_color=theme.TEXT_PRIMARY,
            anchor="w",
        ).pack(side="left")
        self.manual_seat_badge = ctk.CTkLabel(
            title_row,
            text="좌석도 없이 사용 가능",
            font=theme.FONT_CAPTION,
            text_color=theme.TINT_SUCCESS_FG,
            fg_color=theme.TINT_SUCCESS_BG,
            corner_radius=theme.ROUNDED_PILL,
            padx=theme.SPACE_2,
            pady=2,
        )
        self.manual_seat_badge.pack(side="right")

        self.manual_seat_hint = ctk.CTkLabel(
            self.manual_seat_frame,
            text="",
            font=theme.FONT_LABEL,
            text_color=theme.TEXT_MUTE,
            anchor="w",
            justify="left",
        )
        self.manual_seat_hint.pack(fill="x", padx=theme.SPACE_2, pady=(0, theme.SPACE_1))

        input_row = ctk.CTkFrame(self.manual_seat_frame, fg_color="transparent")
        input_row.pack(fill="x", padx=theme.SPACE_2, pady=(0, theme.SPACE_2))
        self.manual_seat_entry = ctk.CTkEntry(
            input_row,
            placeholder_text="",
            fg_color=theme.SURFACE_COLOR,
            border_color=theme.HAIRLINE_COLOR,
            text_color=theme.TEXT_PRIMARY,
            placeholder_text_color=theme.TEXT_MUTE,
            height=theme.H_CONTROL,
            corner_radius=theme.ROUNDED_MD,
        )
        self.manual_seat_entry.pack(side="left", fill="x", expand=True)
        self.manual_seat_entry.bind("<Return>", lambda _event: self._add_manual_priorities())

        self.manual_add_button = ctk.CTkButton(
            input_row,
            text="추가",
            command=self._add_manual_priorities,
            width=72,
            height=theme.H_CONTROL,
            fg_color=theme.ACCENT_BLUE,
            hover_color=theme.ACCENT_BLUE_HOVER,
            text_color=theme.TEXT_PRIMARY,
            corner_radius=theme.ROUNDED_MD,
        )
        self.manual_add_button.pack(side="left", padx=(theme.SPACE_1, 0))

        utility_row = ctk.CTkFrame(self.manual_seat_frame, fg_color="transparent")
        utility_row.pack(fill="x", padx=theme.SPACE_2, pady=(0, theme.SPACE_2))
        self.priority_count_label = ctk.CTkLabel(
            utility_row,
            text="",
            font=theme.FONT_LABEL,
            text_color=theme.TEXT_TERTIARY,
            anchor="w",
        )
        self.priority_count_label.pack(side="left", fill="x", expand=True)
        self.remove_last_priority_button = ctk.CTkButton(
            utility_row,
            text="마지막 우선순위 삭제",
            command=self._remove_last_priority,
            width=126,
            height=theme.H_GHOST,
            fg_color="transparent",
            hover_color=theme.CARD_COLOR,
            border_width=1,
            border_color=theme.CONTROL_BORDER,
            text_color=theme.TEXT_BODY,
            corner_radius=theme.ROUNDED_SM,
            font=theme.FONT_CAPTION,
        )
        self.remove_last_priority_button.pack(side="right")

    def _install_priority_summary(self) -> None:
        """Keep saved priorities visible without shrinking the seat viewport."""

        seat_panel = self.auto_seat_menu.master
        try:
            self.priority_label.pack_forget()
        except Exception:
            pass

        self.priority_summary_frame = ctk.CTkFrame(
            seat_panel,
            fg_color=theme.SURFACE_COLOR,
            border_width=1,
            border_color=theme.HAIRLINE_COLOR,
            corner_radius=theme.ROUNDED_MD,
        )
        self.priority_summary_frame.pack(
            fill="x",
            padx=theme.SPACE_3,
            pady=(0, theme.SPACE_1),
            before=self.auto_seat_menu,
        )

        header = ctk.CTkFrame(self.priority_summary_frame, fg_color="transparent")
        header.pack(fill="x", padx=theme.SPACE_2, pady=(theme.SPACE_1, 0))
        ctk.CTkLabel(
            header,
            text="저장된 좌석 우선순위",
            font=theme.FONT_LABEL,
            text_color=theme.TEXT_MUTE,
            anchor="w",
        ).pack(side="left")
        self.priority_overflow_label = ctk.CTkLabel(
            header,
            text="",
            font=theme.FONT_CAPTION,
            text_color=theme.TEXT_TERTIARY,
            anchor="e",
        )
        self.priority_overflow_label.pack(side="right")

        self.priority_chip_grid = ctk.CTkFrame(
            self.priority_summary_frame,
            fg_color="transparent",
        )
        self.priority_chip_grid.pack(
            fill="x", padx=theme.SPACE_2, pady=(theme.SPACE_1, theme.SPACE_2)
        )
        for column in range(3):
            self.priority_chip_grid.columnconfigure(column, weight=1, uniform="priority")

    def _render_priority_summary(self) -> None:
        grid = getattr(self, "priority_chip_grid", None)
        if grid is None:
            return
        for child in grid.winfo_children():
            child.destroy()

        visible = list(self.priority_groups[:MAX_VISIBLE_PRIORITY_CHIPS])
        if not visible:
            ctk.CTkLabel(
                grid,
                text="아직 저장된 좌석 우선순위가 없습니다.",
                font=theme.FONT_LABEL,
                text_color=theme.TEXT_MUTE,
                anchor="w",
            ).grid(row=0, column=0, columnspan=3, sticky="ew", padx=2, pady=1)
        else:
            for index, group in enumerate(visible):
                ctk.CTkButton(
                    grid,
                    text=f"{index + 1}순위 · {','.join(group)}  ×",
                    command=lambda idx=index: self._remove_priority_at(idx),
                    height=theme.H_GHOST,
                    fg_color=theme.ELEVATED_COLOR,
                    hover_color=theme.CARD_COLOR,
                    border_width=1,
                    border_color=theme.CONTROL_BORDER,
                    text_color=theme.TEXT_BODY,
                    corner_radius=theme.ROUNDED_SM,
                    font=theme.FONT_CAPTION,
                    anchor="w",
                ).grid(
                    row=index // 3,
                    column=index % 3,
                    sticky="ew",
                    padx=2,
                    pady=2,
                )

        overflow = max(0, len(self.priority_groups) - MAX_VISIBLE_PRIORITY_CHIPS)
        self.priority_overflow_label.configure(
            text=f"+{overflow}개 더 있음" if overflow else f"총 {len(self.priority_groups)}개"
        )

    def _refresh_manual_seat_ui(self) -> None:
        if not hasattr(self, "manual_seat_entry"):
            return
        if self.people == 1:
            example = "예: C8  · 여러 순위: C8 | D8 | E8"
            placeholder = "예: C8 | D8 | E8"
        else:
            first = ",".join(f"C{8 + index}" for index in range(self.people))
            second = ",".join(f"D{8 + index}" for index in range(self.people))
            example = f"예: {first}  · 여러 순위: {first} | {second}"
            placeholder = f"예: {first} | {second}"
        self.manual_seat_hint.configure(
            text=f"{self.people}명 기준 · {example} · Enter로도 추가"
        )
        self.manual_seat_entry.configure(placeholder_text=placeholder)
        count = len(self.priority_groups)
        self.priority_count_label.configure(
            text=(
                f"현재 우선순위 {count}개 · 1번부터 순서대로 좌석을 찾습니다."
                if count
                else "저장된 우선순위 없음 · 좌석도 로딩 전에도 직접 입력할 수 있습니다."
            )
        )
        self.remove_last_priority_button.configure(
            state="normal" if count else "disabled"
        )

    def _add_manual_priorities(self) -> None:
        raw = self.manual_seat_entry.get().strip()
        if not raw:
            self.status_label.configure(
                text="추가할 좌석 번호를 입력해주세요.",
                text_color=theme.ACCENT_YELLOW,
            )
            return
        try:
            groups = parse_manual_seat_priorities(raw, self.people)
        except ValueError as exc:
            self.status_label.configure(text=str(exc), text_color=theme.ACCENT_YELLOW)
            return

        added = 0
        for group in groups:
            if group not in self.priority_groups:
                self.priority_groups.append(group)
                added += 1

        if added:
            self.manual_seat_entry.delete(0, "end")
            self.status_label.configure(
                text=(
                    f"좌석 우선순위 {added}개를 추가했습니다. "
                    "대상 회차가 열리면 실제 좌석 번호와 자동으로 대조합니다."
                ),
                text_color=theme.TINT_SUCCESS_FG,
            )
        else:
            self.status_label.configure(
                text="이미 같은 좌석 우선순위가 저장되어 있습니다.",
                text_color=theme.TEXT_MUTE,
            )
        self._render_priorities()

    def _remove_priority_at(self, index: int) -> None:
        if not (0 <= int(index) < len(self.priority_groups)):
            return
        removed = self.priority_groups.pop(int(index))
        self.status_label.configure(
            text=f"{int(index) + 1}순위 {', '.join(removed)}를 삭제했습니다.",
            text_color=theme.TEXT_MUTE,
        )
        if self.seats:
            self._render_seats()
        self._render_priorities()

    def _remove_last_priority(self) -> None:
        if not self.priority_groups:
            return
        self._remove_priority_at(len(self.priority_groups) - 1)

    def _render_priorities(self) -> None:
        BaseCgvBookingDialog._render_priorities(self)
        refresh = getattr(self, "_refresh_manual_seat_ui", None)
        if callable(refresh):
            refresh()
        render_summary = getattr(self, "_render_priority_summary", None)
        if callable(render_summary):
            render_summary()

    def _set_people(self, new_people: int) -> None:
        BaseCgvBookingDialog._set_people(self, new_people)
        refresh = getattr(self, "_refresh_manual_seat_ui", None)
        if callable(refresh):
            refresh()
