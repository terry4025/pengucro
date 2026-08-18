from __future__ import annotations

import re

from engines.cgv_client import is_contiguous_seat_group
from engines.cgv_yongsan_preopen_presets import (
    is_yongsan_imax_target,
    yongsan_imax_preopen_groups,
)
import ui.theme as theme
from ui.cgv_booking_dialog_movie_runtime import (
    CgvBookingDialog as MovieIdentityCgvBookingDialog,
)


class CgvBookingDialog(MovieIdentityCgvBookingDialog):
    """Final selector with concrete Yongsan IMAX priorities before seat-map open.

    Other theaters keep the existing metadata-only pre-open auto-seat behavior.
    For Yongsan IMAX, the dedicated hard-coded center/row guide can stage a real
    N-seat label group immediately so the visible priority-add button works even
    before CGV publishes the target-date seat map.
    """

    def _yongsan_target_context(self) -> tuple[str, str, str]:
        site = getattr(self, "selected_site", None)
        site_no = str(getattr(site, "site_no", "") or "")
        auditorium_opt = ""
        variable = getattr(self, "auditorium_var", None)
        if variable is not None and hasattr(variable, "get"):
            try:
                auditorium_opt = str(variable.get() or "")
            except Exception:
                auditorium_opt = ""
        if " · " in auditorium_opt:
            auditorium, format_name = auditorium_opt.split(" · ", 1)
        else:
            auditorium, format_name = auditorium_opt, ""
        return site_no, auditorium, format_name

    def _reset_priority_add_button_text(self) -> None:
        button = getattr(self, "add_priority_button", None)
        if button is None or not hasattr(button, "configure"):
            return
        try:
            button.configure(text="좌석도 선택 추가")
        except Exception:
            pass

    @staticmethod
    def _seat_label_sort_key(value: str) -> tuple[str, int]:
        text = str(value or "")
        return (
            re.sub(r"\d", "", text),
            int(re.sub(r"\D", "", text) or 0),
        )

    def _auto_select_seats(self, label: str) -> None:
        # Preserve all existing behavior first: this stores auto_seat_preference
        # for pre-open dates and performs the mature real-map calculation when
        # seats are already loaded.
        MovieIdentityCgvBookingDialog._auto_select_seats(self, label)

        if getattr(self, "seats", None):
            CgvBookingDialog._reset_priority_add_button_text(self)
            return

        modes = getattr(self, "auto_seat_modes", {}) or {}
        mode = str(modes.get(label, "") or "").strip()
        if not mode:
            CgvBookingDialog._reset_priority_add_button_text(self)
            return

        site_no, auditorium, format_name = CgvBookingDialog._yongsan_target_context(self)
        if not is_yongsan_imax_target(site_no, auditorium, format_name):
            return

        people = max(1, min(int(getattr(self, "people", 1) or 1), 8))
        groups = yongsan_imax_preopen_groups(mode, people)
        existing = {
            tuple(str(seat) for seat in group)
            for group in (getattr(self, "priority_groups", None) or [])
        }
        group = next((candidate for candidate in groups if candidate not in existing), None)

        button = getattr(self, "add_priority_button", None)
        if group is None:
            if hasattr(self, "current_seats"):
                self.current_seats.clear()
            if button is not None and hasattr(button, "configure"):
                button.configure(text="명당 우선순위 추가", state="disabled")
            status = getattr(self, "status_label", None)
            if status is not None and hasattr(status, "configure"):
                status.configure(
                    text=(
                        f"{label}의 미오픈 중앙 후보는 모두 우선순위에 추가했습니다. "
                        "실제 회차가 열리면 명당 자동 선택 전략이 실제 좌석도로 한 번 더 검증됩니다."
                    ),
                    text_color=theme.ACCENT_YELLOW,
                )
            return

        self.current_seats = set(group)
        if button is not None and hasattr(button, "configure"):
            button.configure(text="명당 우선순위 추가", state="normal")
        status = getattr(self, "status_label", None)
        if status is not None and hasattr(status, "configure"):
            status.configure(
                text=(
                    f"{label} 미오픈 프리셋: {', '.join(group)} · "
                    "'명당 우선순위 추가'를 누르면 실제 좌석 번호 우선순위로 저장됩니다."
                ),
                text_color=theme.TINT_SUCCESS_FG,
            )
        CgvBookingDialog._update_confirm_state(self)

    def _add_priority_group(self) -> None:
        # When there is no seat map, do not call the base implementation because
        # it always re-renders the seat viewport and would replace the useful
        # pre-open placeholder with an empty map.  Only the priority chips need
        # to change here.
        if not bool(getattr(self, "seats", None)):
            current = set(getattr(self, "current_seats", set()) or set())
            people = max(1, min(int(getattr(self, "people", 1) or 1), 8))
            if not is_contiguous_seat_group(current, people):
                return

            group = tuple(
                sorted(current, key=CgvBookingDialog._seat_label_sort_key)
            )
            priorities = getattr(self, "priority_groups", None)
            if priorities is None:
                self.priority_groups = []
                priorities = self.priority_groups
            if group not in priorities:
                priorities.append(group)

            self.current_seats.clear()
            auto_var = getattr(self, "auto_seat_var", None)
            if auto_var is not None and hasattr(auto_var, "set"):
                auto_var.set("명당 자동 선택")
            button = getattr(self, "add_priority_button", None)
            if button is not None and hasattr(button, "configure"):
                button.configure(text="명당 우선순위 추가", state="disabled")

            renderer = getattr(self, "_render_priorities", None)
            if callable(renderer):
                renderer()
            else:
                CgvBookingDialog._update_confirm_state(self)

            status = getattr(self, "status_label", None)
            if status is not None and hasattr(status, "configure"):
                status.configure(
                    text=(
                        f"{len(priorities)}순위로 {', '.join(group)}를 추가했습니다. "
                        "같은 명당 모드를 다시 선택하면 다음 중앙 후보를 추가할 수 있습니다."
                    ),
                    text_color=theme.TINT_SUCCESS_FG,
                )
            return

        MovieIdentityCgvBookingDialog._add_priority_group(self)
        CgvBookingDialog._reset_priority_add_button_text(self)

    def _seats_loaded(self, seats, *, generation: int | None = None) -> None:
        MovieIdentityCgvBookingDialog._seats_loaded(
            self, seats, generation=generation
        )
        CgvBookingDialog._reset_priority_add_button_text(self)

    def _clear_priorities(self) -> None:
        MovieIdentityCgvBookingDialog._clear_priorities(self)
        CgvBookingDialog._reset_priority_add_button_text(self)
