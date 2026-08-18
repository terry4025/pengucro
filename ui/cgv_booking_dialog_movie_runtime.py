from __future__ import annotations

import engines.cgv_browser_client as _browser_client_module
from engines.cgv_movie_identity import schedule_movie_name
from ui import cgv_booking_dialog as _base_dialog_module
from ui.cgv_booking_dialog_preopen_auto import CgvBookingDialog as PreopenAutoCgvBookingDialog


# Both the selector and the historical-reference aggregator resolve _movie_name
# from their module globals at call time.  Use the same canonical display title
# in both places so real schedules replace equivalent pre-open templates rather
# than appearing as a second movie whose name merely contains the format suffix.
_base_dialog_module._movie_name = schedule_movie_name
_browser_client_module._movie_name = schedule_movie_name


class CgvBookingDialog(PreopenAutoCgvBookingDialog):
    """Final selector using stable movie identity for real and pre-open dates."""

    pass
