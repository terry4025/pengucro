from __future__ import annotations

from typing import Any

import ui.theme as theme
from ui.cgv_booking_dialog_controls import CgvBookingDialog as ControlsCgvBookingDialog


class CgvBookingDialog(ControlsCgvBookingDialog):
    """Final CGV selector with a seat-map-independent recommendation preference.

    The concrete seat numbers can only be calculated after CGV exposes a real
    seat layout.  The *preference* (balanced/best/recommended/etc.) does not need
    that layout, so keep it as booking metadata and resolve it against the real
    target-date seats when the booking engine sees them.
    """

    def __init__(self, *args, **kwargs) -> None:
        initial = kwargs.get("initial") or {}
        self.auto_seat_preference = str(initial.get("auto_seat_mode", "") or "").strip()
        self.auto_seat_preference_label = str(
            initial.get("auto_seat_label", "") or ""
        ).strip()
        super().__init__(*args, **kwargs)
        self._refresh_preopen_auto_controls()

    def _clear_auto_preference(self) -> None:
        self.auto_seat_preference = ""
        self.auto_seat_preference_label = ""

    def _label_for_auto_mode(self, mode: str, options: dict[str, str]) -> str:
        mode = str(mode or "").strip()
        if not mode:
            return next(iter(options), "명당 자동 선택")
        return next(
            (label for label, value in options.items() if str(value) == mode),
            getattr(self, "auto_seat_preference_label", "")
            or next(iter(options), "명당 자동 선택"),
        )

    def _refresh_preopen_auto_controls(self) -> None:
        menu = getattr(self, "auto_seat_menu", None)
        variable = getattr(self, "auto_seat_var", None)
        if menu is None or variable is None:
            return

        movie = self.movie_var.get() if hasattr(self, "movie_var") else ""
        auditorium = self.auditorium_var.get() if hasattr(self, "auditorium_var") else ""
        has_target = bool(
            getattr(self, "selected_site", None)
            and movie
            and movie not in {
                "영화를 먼저 불러오세요",
                "시간표를 불러오는 중...",
                "표시할 영화가 없습니다",
            }
            and auditorium
            and auditorium not in {"상영관을 먼저 불러오세요", "표시할 상영관이 없습니다"}
            and (getattr(self, "selected_schedule", None) or getattr(self, "preferred_times", None))
        )

        if not has_target:
            menu.configure(values=["명당 자동 선택"], state="disabled")
            variable.set("명당 자동 선택")
            return

        options = dict(self._auto_seat_options())
        self.auto_seat_modes = options
        menu.configure(values=list(options), state="normal")
        label = CgvBookingDialog._label_for_auto_mode(
            self, getattr(self, "auto_seat_preference", ""), options
        )
        variable.set(label)
        if getattr(self, "auto_seat_preference", ""):
            self.auto_seat_preference_label = label

    def _update_seat_guide(self) -> None:
        # Keep these wrapper methods callable as unbound functions with the
        # repository's SimpleNamespace dialog fixtures.  zero-argument super()
        # requires ``self`` to be an actual subclass instance and broke the
        # historical tests once this final runtime layer was installed.
        ControlsCgvBookingDialog._update_seat_guide(self)
        CgvBookingDialog._refresh_preopen_auto_controls(self)

    def _select_site(self, site, *, user_initiated: bool = True) -> None:
        if user_initiated:
            CgvBookingDialog._clear_auto_preference(self)
        ControlsCgvBookingDialog._select_site(
            self, site, user_initiated=user_initiated
        )
        CgvBookingDialog._refresh_preopen_auto_controls(self)

    def _movie_changed(self, value: str = "", *, user_initiated: bool = True) -> None:
        if user_initiated:
            CgvBookingDialog._clear_auto_preference(self)
        ControlsCgvBookingDialog._movie_changed(
            self, value, user_initiated=user_initiated
        )
        CgvBookingDialog._refresh_preopen_auto_controls(self)

    def _auditorium_changed(self, value: str = "", *, user_initiated: bool = True) -> None:
        if user_initiated:
            CgvBookingDialog._clear_auto_preference(self)
        ControlsCgvBookingDialog._auditorium_changed(
            self, value, user_initiated=user_initiated
        )
        CgvBookingDialog._refresh_preopen_auto_controls(self)

    def _set_people(self, new_people: int) -> None:
        ControlsCgvBookingDialog._set_people(self, new_people)
        CgvBookingDialog._refresh_preopen_auto_controls(self)

    def _clear_preferred_times(self) -> None:
        ControlsCgvBookingDialog._clear_preferred_times(self)
        CgvBookingDialog._refresh_preopen_auto_controls(self)

    def _auto_select_seats(self, label: str) -> None:
        mode = str(self.auto_seat_modes.get(label, "") or "").strip()
        if not mode:
            CgvBookingDialog._clear_auto_preference(self)
            self._update_confirm_state()
            return

        self.auto_seat_preference = mode
        self.auto_seat_preference_label = str(label)

        if not self.seats:
            self.current_seats.clear()
            self.add_priority_button.configure(state="disabled")
            self.status_label.configure(
                text=(
                    f"{label}을 저장했습니다. 실제 회차가 열리면 실제 좌석 배치에서 "
                    f"{self.people}석 연속 명당을 계산해 좌석 우선순위 뒤에 적용합니다."
                ),
                text_color=theme.TINT_SUCCESS_FG,
            )
            self._update_confirm_state()
            return

        # With a real map present, keep the mature preview behavior while also
        # retaining the mode as a runtime fallback if manually saved groups are
        # unavailable on the target screening.
        ControlsCgvBookingDialog._auto_select_seats(self, label)
        self._update_confirm_state()

    def _clear_priorities(self) -> None:
        CgvBookingDialog._clear_auto_preference(self)
        ControlsCgvBookingDialog._clear_priorities(self)
        CgvBookingDialog._refresh_preopen_auto_controls(self)

    def _update_confirm_state(self) -> None:
        movie = self.movie_var.get() if hasattr(self, "movie_var") else ""
        auditorium = self.auditorium_var.get() if hasattr(self, "auditorium_var") else ""
        has_valid_movie = bool(
            movie
            and movie not in (
                "영화를 먼저 불러오세요",
                "시간표를 불러오는 중...",
                "표시할 영화가 없습니다",
            )
        )
        has_valid_auditorium = bool(
            auditorium
            and auditorium not in (
                "상영관을 먼저 불러오세요",
                "표시할 상영관이 없습니다",
            )
        )
        has_seat_strategy = bool(
            getattr(self, "priority_groups", None)
            or getattr(self, "auto_seat_preference", "")
        )
        ready = bool(
            getattr(self, "selected_site", None)
            and has_valid_movie
            and has_valid_auditorium
            and (
                getattr(self, "selected_schedule", None)
                or getattr(self, "preferred_times", None)
            )
            and has_seat_strategy
        )
        if hasattr(self, "confirm_button"):
            self.confirm_button.configure(state="normal" if ready else "disabled")

    def _confirm(self) -> None:
        if not self.selected_site:
            return
        if not self.priority_groups and not self.auto_seat_preference:
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
            (
                region.name
                for region in self.regions
                if region.code == self.selected_site.region_code
            ),
            "",
        )
        result: dict[str, Any] = {
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
            "auto_seat_mode": self.auto_seat_preference,
            "auto_seat_label": self.auto_seat_preference_label,
        }
        self.on_select(result)
        self._close_dialog()
