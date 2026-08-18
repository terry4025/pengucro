from __future__ import annotations

import customtkinter as ctk

import ui.theme as theme
from engines.cgv_client import is_contiguous_seat_group
from ui.cgv_booking_dialog_runtime import CgvBookingDialog as RuntimeCgvBookingDialog


class CgvBookingDialog(RuntimeCgvBookingDialog):
    """Final CGV selector controls.

    The large seat-map layout intentionally gives most of the right panel to the
    seat viewport.  The historical ``우선순위 추가`` action lived underneath
    that expanding viewport, so after the layout enlargement users could click
    seats but could no longer reach the action that commits those seats to the
    priority list.  Keep the viewport large and move the commit action next to
    the always-visible manual-entry action instead.
    """

    def _install_manual_seat_controls(self) -> None:
        super()._install_manual_seat_controls()

        old_priority_button = getattr(self, "add_priority_button", None)
        old_actions = getattr(old_priority_button, "master", None)
        if old_priority_button is not None:
            try:
                old_priority_button.pack_forget()
            except Exception:
                pass

        # The old action row only contained the seat-map priority button and a
        # reset button.  Both actions are recreated in the always-visible manual
        # controls, so removing the row also returns a little more height to the
        # seat viewport.
        if old_actions is not None:
            try:
                old_actions.pack_forget()
            except Exception:
                pass
        self._legacy_priority_actions = old_actions

        input_row = self.manual_seat_entry.master
        self.seat_map_add_button = ctk.CTkButton(
            input_row,
            text="좌석도 선택 추가",
            command=self._add_priority_group,
            state=(
                "normal"
                if is_contiguous_seat_group(self.current_seats, self.people)
                else "disabled"
            ),
            width=126,
            height=theme.H_CONTROL,
            fg_color=theme.TINT_INFO_BG,
            hover_color=theme.CARD_COLOR,
            border_width=1,
            border_color=theme.TINT_INFO_FG,
            text_color=theme.TINT_INFO_FG,
            corner_radius=theme.ROUNDED_MD,
        )
        self.seat_map_add_button.pack(side="left", padx=(theme.SPACE_1, 0))

        # Base seat-map code enables/disables ``self.add_priority_button`` from
        # _toggle_seat(), _auto_select_seats(), _add_priority_group() and
        # _clear_priorities().  Point that existing contract at the visible
        # replacement so all mature selection behaviour stays unchanged.
        self.add_priority_button = self.seat_map_add_button

        utility_row = self.remove_last_priority_button.master
        self.clear_all_priority_button = ctk.CTkButton(
            utility_row,
            text="전체 초기화",
            command=self._clear_priorities,
            width=78,
            height=theme.H_GHOST,
            fg_color="transparent",
            hover_color=theme.CARD_COLOR,
            border_width=1,
            border_color=theme.CONTROL_BORDER,
            text_color=theme.TEXT_BODY,
            corner_radius=theme.ROUNDED_SM,
            font=theme.FONT_CAPTION,
        )
        self.clear_all_priority_button.pack(
            side="right",
            padx=(0, theme.SPACE_1),
            before=self.remove_last_priority_button,
        )

    def _add_priority_group(self) -> None:
        before = len(self.priority_groups)
        selected = tuple(sorted(self.current_seats))
        super()._add_priority_group()
        if len(self.priority_groups) > before:
            group = self.priority_groups[-1]
            self.status_label.configure(
                text=(
                    f"{len(self.priority_groups)}순위로 {', '.join(group)}를 추가했습니다. "
                    "좌석도에서 다음 묶음을 선택하면 계속 우선순위를 추가할 수 있습니다."
                ),
                text_color=theme.TINT_SUCCESS_FG,
            )
        elif selected:
            self.status_label.configure(
                text="이미 저장된 좌석 묶음이거나 현재 인원수에 맞는 연속 좌석이 아닙니다.",
                text_color=theme.ACCENT_YELLOW,
            )
