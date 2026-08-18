"""UI package runtime wiring.

Keep historical import paths stable while installing the small production
runtime layers used by the CGV selector and reservation form.
"""

from engines.cgv_browser_client_runtime import CgvBrowserClient as _RuntimeCgvBrowserClient

# Keep engines.cgv_browser_client.CgvBrowserClient itself untouched so its base
# unit tests remain deterministic. The selector module resolves CgvBrowserClient
# from its own module globals at runtime, so inject the persistent-tab/slot-one
# implementation only there.
from ui import cgv_booking_dialog as _cgv_booking_dialog  # noqa: E402

_cgv_booking_dialog.CgvBrowserClient = _RuntimeCgvBrowserClient

from ui.cgv_booking_dialog_runtime import CgvBookingDialog as _RuntimeCgvBookingDialog  # noqa: E402

_cgv_booking_dialog.CgvBookingDialog = _RuntimeCgvBookingDialog

from ui import reservation_form as _reservation_form  # noqa: E402
from ui.reservation_form_runtime import ReservationForm as _RuntimeReservationForm  # noqa: E402

_reservation_form.ReservationForm = _RuntimeReservationForm

__all__ = []
