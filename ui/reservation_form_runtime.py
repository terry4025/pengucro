from __future__ import annotations

from dataclasses import replace

from ui.reservation_form import ReservationForm as BaseReservationForm


class ReservationForm(BaseReservationForm):
    """Preserve structured CGV seat priorities and pre-open auto-seat strategy."""

    @staticmethod
    def _synthetic_validation_group(people: int) -> list[str]:
        """Legacy base validation requires concrete seats; never persist this group."""
        count = max(1, min(int(people), 8))
        return [f"A{index}" for index in range(1, count + 1)]

    def _render_cgv_selection_summary(self):
        selection = getattr(self, "cgv_selection", {})
        auto_label = str(selection.get("auto_seat_label", "") or "").strip()
        seats = str(selection.get("seats", "") or "").strip()
        if not auto_label or seats:
            return BaseReservationForm._render_cgv_selection_summary(self)

        original = self.cgv_selection
        preview = dict(original)
        preview["seats"] = f"자동 · {auto_label}"
        self.cgv_selection = preview
        try:
            return BaseReservationForm._render_cgv_selection_summary(self)
        finally:
            self.cgv_selection = original

    def get_reservation_data(self):
        try:
            uses_cgv = bool(self._site_uses_cgv())
        except Exception:
            uses_cgv = False

        original_selection = getattr(self, "cgv_selection", {})
        selection = dict(original_selection or {}) if uses_cgv else {}
        auto_mode = str(selection.get("auto_seat_mode", "") or "").strip()
        auto_label = str(selection.get("auto_seat_label", "") or "").strip()
        raw_seats = str(selection.get("seats", "") or "").strip()
        synthetic_validation = bool(uses_cgv and auto_mode and not raw_seats)

        # The historical base form rejects CGV requests without an explicit seat
        # string. Auto-seat is resolved only after the target-date real seat map
        # exists, so supply a temporary contiguous group solely to pass that old
        # UI validation contract. The placeholder is stripped from the returned
        # request immediately below and can never reach the booking engine.
        if synthetic_validation:
            try:
                people = int(selection.get("people") or self.people_entry.get().strip() or "2")
            except Exception:
                people = 2
            # Use the class directly so the repository's duck-typed form tests
            # can call this method on SimpleNamespace fixtures too.
            group = ReservationForm._synthetic_validation_group(people)
            temporary = dict(selection)
            temporary["seats"] = ",".join(group)
            temporary["seat_groups"] = [group]
            self.cgv_selection = temporary

        try:
            request, message, threads, is_async = BaseReservationForm.get_reservation_data(self)
        finally:
            if synthetic_validation:
                self.cgv_selection = original_selection

        if request is None or not uses_cgv:
            return request, message, threads, is_async

        raw_groups = selection.get("seat_groups", ())
        structured: list[list[str]] = []
        if isinstance(raw_groups, (list, tuple)):
            for raw_group in raw_groups:
                if not isinstance(raw_group, (list, tuple)):
                    continue
                group = [str(seat or "").strip() for seat in raw_group]
                group = [seat for seat in group if seat]
                if group:
                    structured.append(group)

        metadata = dict(request.engine_metadata or {})
        cgv = dict(metadata.get("cgv", {}) or {})
        if structured:
            cgv["seat_groups"] = structured
        else:
            cgv.pop("seat_groups", None)

        if auto_mode:
            cgv["auto_seat_mode"] = auto_mode
            cgv["auto_seat_label"] = auto_label
        else:
            cgv.pop("auto_seat_mode", None)
            cgv.pop("auto_seat_label", None)

        if synthetic_validation:
            # Critical invariant: the A1.. placeholder existed only inside the
            # base form call and must not escape into reservation metadata.
            cgv["seats"] = ""

        cgv["priority_rotation_mode"] = "fast" if selection.get("priority_rotation_mode") == "fast" else "strict"
        from engines.cgv_preopen_matching import normalize_preopen_time_drift
        cgv["preopen_time_drift_minutes"] = normalize_preopen_time_drift(selection.get("preopen_time_drift_minutes"))
        metadata["cgv"] = cgv
        request = replace(request, engine_metadata=metadata)
        return request, message, threads, is_async
