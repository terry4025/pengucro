from __future__ import annotations

from dataclasses import replace

from ui.reservation_form import ReservationForm as BaseReservationForm


class ReservationForm(BaseReservationForm):
    """Preserve CGV's structured seat priorities into the engine payload.

    The selector already returns ``seat_groups`` as an ordered list of lists,
    but the historical form only copied the legacy ``seats`` string into
    ``engine_metadata``. Keep both representations: the string remains backward
    compatible while the structured list is authoritative for multi-priority
    booking.
    """

    def get_reservation_data(self):
        # Keep compatibility with the repository's unbound SimpleNamespace
        # state tests. zero-argument super() rejects those mock objects even
        # though the base method itself only needs duck-typed form attributes.
        request, message, threads, is_async = BaseReservationForm.get_reservation_data(self)
        if request is None:
            return request, message, threads, is_async

        try:
            uses_cgv = bool(self._site_uses_cgv())
        except Exception:
            uses_cgv = False
        if not uses_cgv:
            return request, message, threads, is_async

        raw_groups = getattr(self, "cgv_selection", {}).get("seat_groups", ())
        structured: list[list[str]] = []
        if isinstance(raw_groups, (list, tuple)):
            for raw_group in raw_groups:
                if not isinstance(raw_group, (list, tuple)):
                    continue
                group = [str(seat or "").strip() for seat in raw_group]
                group = [seat for seat in group if seat]
                if group:
                    structured.append(group)

        if not structured:
            return request, message, threads, is_async

        metadata = dict(request.engine_metadata or {})
        cgv = dict(metadata.get("cgv", {}) or {})
        cgv["seat_groups"] = structured
        metadata["cgv"] = cgv
        request = replace(request, engine_metadata=metadata)
        return request, message, threads, is_async
