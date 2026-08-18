"""UI package runtime wiring.

CGV has a thin runtime layer for session reuse and pre-open manual seat entry.
Install it here before ``ui.cgv_booking_dialog`` is imported by the reservation
form, so existing call sites can continue importing the historical module path.
"""

from engines import cgv_browser_client as _cgv_browser_client
from engines.cgv_browser_client_runtime import CgvBrowserClient as _RuntimeCgvBrowserClient

_cgv_browser_client.CgvBrowserClient = _RuntimeCgvBrowserClient

from ui import cgv_booking_dialog as _cgv_booking_dialog  # noqa: E402
from ui.cgv_booking_dialog_runtime import CgvBookingDialog as _RuntimeCgvBookingDialog  # noqa: E402

_cgv_booking_dialog.CgvBookingDialog = _RuntimeCgvBookingDialog

__all__ = []
