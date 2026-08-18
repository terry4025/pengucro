from __future__ import annotations

import re

import customtkinter as ctk

import ui.theme as theme
from engines.cgv_client import is_contiguous_seat_group, normalize_seat_name
from ui.cgv_booking_dialog import CgvBookingDialog as BaseCgvBookingDialog


def parse_manual_seat_priorities(raw: str, people: int) -> list[tuple[str, ...]]:
    """Parse user-entered CGV seat groups without requiring a live seat map.

    Examples for two people:
      C8,C9
      C8 C9 | D8 D9
      C8,C9; D8,D9

    Each group must contain exactly ``people`` seats in one contiguous row.
    Physical-seat existence is intentionally not required here because this
    feature is specifically for unpublished dates whose seat map is not yet
    available. The booking engine still resolves the labels against the real
    seat map when the target screening opens.
    """

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
    """CGV selector with pre-open manual seat entry and streamlined controls."""

    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self._install_manual_seat_controls()
        self._refresh_manual_seat_ui()

    def _install_manual_seat_controls(self) -> None:
        seat_panel = self.load_seats_button.master

        self.load_seats_button.configure(text="좌석도 불러오기 · 실제 배치 확인")
        self.add_priority_button.configure(text="선택한 좌석 우선순위 추가")
        self.seat_help.configure(
            text=(
                f"{self.people}석씩 우선순위를 저장하세요. 좌석도가 아직 열리지 않은 날짜도 "
                "아래 빠른 입력으로 C8,C9처럼 미리 지정할 수 있습니다. "
                "실제 좌석도가 열리면 같은 우선순위를 그대로 검증·사용합니다."
            )
        )

        self.manual_seat_frame = ctk.CTkFrame(
            seat_panel,
            fg_color=theme.ELEVATED_COLOR,
            border_width=1,
            border_color=theme.CONTROL_BORDER,
            corner_radius=theme.ROUNDED_MD,
        )
        # CustomTkinter wrappers do not always expose the same Tk packing parent
        # as the logical widget. The load button is a known direct child and is
        # already packed, so anchor the new controls after that stable sibling.
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

    def _remove_last_priority(self) -> None:
        if not self.priority_groups:
            return
        removed = self.priority_groups.pop()
        self.status_label.configure(
            text=f"마지막 우선순위 {', '.join(removed)}를 삭제했습니다.",
            text_color=theme.TEXT_MUTE,
        )
        if self.seats:
            self._render_seats()
        self._render_priorities()

    def _render_priorities(self) -> None:
        BaseCgvBookingDialog._render_priorities(self)
        self._refresh_manual_seat_ui()

    def _set_people(self, new_people: int) -> None:
        # Keep the method compatible with the repository's unbound
        # SimpleNamespace state tests; zero-argument super() rejects those mocks.
        BaseCgvBookingDialog._set_people(self, new_people)
        self._refresh_manual_seat_ui()
