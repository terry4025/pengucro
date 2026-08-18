from __future__ import annotations

from ui.reservation_form import ReservationForm as BaseReservationForm


class ReservationForm(BaseReservationForm):
    """Reservation form with durable autosave bindings for user-entered fields.

    The base form already stores data below %LOCALAPPDATA%/Pengucro, but some
    plain entry edits could remain only in Tk memory until another control
    happened to trigger save_config().  A release/update restart during that
    window made the new build look as if it had reset the values.  Bind the
    fields users expect to survive upgrades directly to the existing debounced
    auto-save path.
    """

    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self._install_durable_autosave_bindings()

    def _install_durable_autosave_bindings(self) -> None:
        for entry in (
            getattr(self, "name_entry", None),
            getattr(self, "phone_entry", None),
            getattr(self, "people_entry", None),
        ):
            if entry is None:
                continue
            try:
                entry.bind("<KeyRelease>", self._autosave_entry_event, add="+")
                entry.bind("<FocusOut>", self._autosave_entry_event, add="+")
            except Exception:
                continue

    def _autosave_entry_event(self, _event=None) -> None:
        if getattr(self, "_is_initializing", False):
            return
        self.auto_save()
