"""Read-only client for Naver Booking's GraphQL API.

Why this exists
---------------
The engine used to learn everything by scraping the rendered React page: it swept
``document.querySelectorAll('button, a, li')`` on every loop turn and matched time
strings with a regex. That is slow, it breaks whenever Naver ships a new build, and
it cannot see a slot that has not been rendered yet.

Everything the engine actually needs is served by the same GraphQL endpoint the
page itself calls, and it answers *without a login*:

``hourlySchedule``
    Per-slot ``slotId`` / ``scheduleId`` / ``stock`` / sale flags for a date range.
    This is the availability signal the polling loop watches.

``bizItem``
    ``currentDateTime`` -- the server's own clock, with real millisecond precision
    (measured: ``...:18.812Z``, ``...:19.310Z``, ``...:20.271Z`` across successive
    calls) -- plus ``bookableSettingJson`` describing the open schedule.

Measured facts behind the design
--------------------------------
* The old REST path ``api.booking.naver.com/v3.0/.../schedules`` is dead. It answers
  ``403 {"errorCode":"NotAccessibleUrl"}`` with or without ``Referer``/``Origin``,
  which is why ``fetch_naver_slots`` had been silently returning nothing.
* GraphQL introspection is disabled, so ``ScheduleParams`` was confirmed by calling
  it: ``businessId``, ``bizItemId``, ``startDateTime``, ``endDateTime`` is accepted.
* ``stock`` is total capacity and ``bookingCount`` is how many are booked, so a
  slot is free when ``bookingCount < stock``. This was established by lining the
  API up against the rendered page for every slot on two dates, 18 in all, and the
  correlation is exact:

      page 매진  ->  stock 1, bookingCount 1
      page 가능  ->  stock 1, bookingCount 0

  Reading ``stock`` alone as "remaining" is the trap here, and it is easy to fall
  into: sample a range while everything happens to be booked and every slot reads
  ``stock: 1``, which looks like "one seat free" but means "capacity one, and it is
  taken". An engine built on that submits into 매진 slots forever.
* ``stock: 0`` appears separately, for a slot the owner has blocked outright
  (2026-08-08 18:00 on the sample item). Subtracting ``bookingCount`` covers that
  case too, so no special handling is needed.
* ``occupiedBookingCount`` read 0 on every slot observed; it is subtracted as well,
  which can only be more conservative.
* Dates outside the open window simply have no entries. The sample item returned
  slots through 2026-08-01 and nothing for 08-02..08-07. A date appearing in the
  response *is* the signal that it opened.
"""

from __future__ import annotations

import json
import logging
import re
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Iterable

import requests


logger = logging.getLogger(__name__)

KST = timezone(timedelta(hours=9))

GRAPHQL_URL = "https://m.booking.naver.com/graphql"
DEFAULT_SERVICE_ID = "12"
REQUEST_TIMEOUT = 8.0

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/150.0.0.0 Safari/537.36"
)

# Only the fields the engine reads. Asking for `prices`/`seatGroups` as the page
# does would roughly double the response for data we never look at.
HOURLY_SCHEDULE_QUERY = """query hourlySchedule($scheduleParams: ScheduleParams) {
  schedule(input: $scheduleParams) {
    bizItemSchedule {
      hourly {
        id
        name
        slotId
        scheduleId
        detailScheduleId
        unitStartDateTime
        unitStartTime
        stock
        bookingCount
        occupiedBookingCount
        unitStock
        unitBookingCount
        isBusinessDay
        isSaleDay
        isUnitSaleDay
        isUnitBusinessDay
        isHoliday
        minBookingCount
        maxBookingCount
        saleStartDateTime
        saleEndDateTime
        duration
        desc
        seatGroups {
          color
          maxPrice
          name
          remainStock
        }
        prices {
          groupName
          isDefault
          price
          priceId
          scheduleId
          priceTypeCode
          name
          normalPrice
          desc
          order
          groupOrder
          slotId
          agencyKey
          bookingCount
          isImp
          saleStartDateTime
          saleEndDateTime
        }
      }
    }
  }
}"""

# Naver's request page does not decide pre-payment from ``isNPayUsed`` alone.
# The authoritative flag belongs to the selected slot and is fetched by the
# official page as soon as a time is selected.  ``null`` is deliberately treated
# as false by that page (pre-payment); true means the booking completes without
# opening the immediate Npay checkout.
SLOT_PAYMENT_QUERY = """query Slot($slotSeatInput: SlotSeatParams) {
  slotSeat(input: $slotSeatInput) {
    slot {
      id
      isPostPayment
    }
  }
}"""

# bookableSettingJson and friends are JSON scalars: selecting subfields on them is
# a hard GraphQL error ("must not have a selection since type JSON has no
# subfields"), so they are requested bare.
BIZ_ITEM_QUERY = """query bizItem($input: BizItemParams) {
  bizItem(input: $input) {
    bizItemId
    name
    currentDateTime
    stock
    isClosedBooking
    isClosedBookingUser
    isImp
    bookableSettingJson
    bookingCountSettingJson
    customFormJson
    isNPayUsed
    isPeriodFixed
    isSeatUsed
    addressJson
    bookingConfirmCode
    paymentSettingJson
    resources { resourceUrl }
  }
}"""

BUSINESS_QUERY = """query business($input: BusinessParams) {
  business(input: $input) {
    businessId
    name
    serviceName
    isImp
    isPhoneAuthenticationRequired
    customFormJson
    bookingConfirmCode
    businessTypeId
    bookingTimeUnitCode
    addressJson
    translationStatusJson
    nPayRegStatusCode
    uncompletedBookingProcessCode
    uncompletedBookingRefundRate
    refundPolicy
    businessResources { resourceUrl }
    agencies { agencyId }
    rawNames { name serviceName }
  }
}"""

ACCOUNT_QUERY = """query account {
  account {
    userId
    isLoggedIn
    nickname
    csrfToken
    isSmsAlarm
  }
}"""

# The booking request itself. Recovered from the page's own lazily loaded chunk
# (``...bizItem_EntranceTimeAlert...chunk.js``); see
# ``reference/naver/submit_booking.md`` for how, and for the probe results that
# establish what the server checks and in which order.
SUBMIT_BOOKING_MUTATION = """mutation submitBooking($input: SubmitBookingParams) {
  submitBooking(input: $input) {
    bookingId
    provider
    url
  }
}"""

# Refusal codes handled by Naver's current booking-page bundle, grouped by what
# the engine should do next. The complete generated payload has passed live
# GraphQL schema validation without login; authenticated resolver behavior has
# deliberately not been probed because even a "safe" target could create a
# booking if its state changed.
SUBMIT_REFUSED_CODES = frozenset({
    "RT25", "RT37", "RT47", "RT71", "RT77", "BOOKING_NOT_AVAILABLE",
    "STALE_DATA", "EXCEEDED_AGENCY_BOOKING_LIMIT",
})
SUBMIT_DUPLICATE_CODES = frozenset({"Duplicated", "DUPLICATED_BOOKING"})
SUBMIT_NOT_OPEN_CODES = frozenset({"BizItem is not opened."})
SUBMIT_AUTH_CODES = frozenset({"UNAUTHENTICATED", "Authentication failed"})
# Naver's own "this looks automated" judgement. Treated as a hard stop for the API
# path: retrying it is the one thing that could turn a missed booking into a
# flagged account.
SUBMIT_ABUSE_CODES = frozenset({"RT98"})
SUBMIT_PAYLOAD_CODES = frozenset({
    "INVALID_CUSTOM_FORM_INPUT", "BAD_USER_INPUT", "BAD_REQUEST",
    "GRAPHQL_VALIDATION_FAILED",
})



class NaverApiError(RuntimeError):
    """A GraphQL call failed in a way the caller cannot paper over."""


def parse_ids(url: str) -> tuple[str, str, str] | None:
    """Pull (service_id, business_id, biz_item_id) out of a booking URL."""
    if not url:
        return None
    match = re.search(
        r"booking\.naver\.com/booking/(?P<service>\d+)/bizes/(?P<business>\d+)"
        r"(?:/items/(?P<item>\d+))?",
        url,
    )
    if not match:
        return None
    item = match.group("item")
    if not item:
        # Without a bizItemId there is no schedule to poll; the caller has to
        # resolve the theme first.
        return None
    return match.group("service") or DEFAULT_SERVICE_ID, match.group("business"), item


def _as_int(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _parse_dt(value: Any) -> datetime | None:
    """Parse the several shapes Naver uses, always returning KST-aware."""
    if not value or not isinstance(value, str):
        return None
    text = value.strip()
    try:
        if text.endswith("Z"):
            return datetime.fromisoformat(text[:-1]).replace(
                tzinfo=timezone.utc).astimezone(KST)
        parsed = datetime.fromisoformat(text.replace(" ", "T"))
    except ValueError:
        return None
    return parsed.astimezone(KST) if parsed.tzinfo else parsed.replace(tzinfo=KST)


def _as_json(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    if isinstance(value, str):
        try:
            loaded = json.loads(value)
        except (TypeError, ValueError):
            return {}
        return loaded if isinstance(loaded, dict) else {}
    return {}


@dataclass(frozen=True)
class NaverSlot:
    """One bookable time on one date."""

    slot_id: str
    schedule_id: str
    detail_schedule_id: str
    composite_id: str
    start: datetime
    stock: int
    booking_count: int
    occupied: int
    is_business_day: bool
    is_sale_day: bool
    is_unit_business_day: bool
    is_unit_sale_day: bool
    is_holiday: bool
    min_booking_count: int
    max_booking_count: int
    sale_start: datetime | None
    sale_end: datetime | None

    @property
    def date_str(self) -> str:
        return self.start.strftime("%Y-%m-%d")

    @property
    def time_str(self) -> str:
        return self.start.strftime("%H:%M")

    @property
    def remaining(self) -> int:
        """Seats still free: capacity minus what is booked or held.

        Verified against the rendered page for 18 slots across two dates. A 매진
        slot reports ``stock 1 / bookingCount 1`` and a free one ``stock 1 /
        bookingCount 0``, so ``stock`` on its own says nothing about availability.
        """
        return max(0, self.stock - max(0, self.booking_count) - max(0, self.occupied))

    def blocked_reason(self, now: datetime | None = None) -> str | None:
        """Why this slot cannot be booked, or None when it can."""
        if self.remaining <= 0:
            return "정원 마감"
        if not (self.is_business_day and self.is_unit_business_day):
            return "휴무일"
        if not (self.is_sale_day and self.is_unit_sale_day):
            return "판매 중지"
        moment = now or datetime.now(KST)
        if self.sale_start and moment < self.sale_start:
            return f"판매 시작 전 ({self.sale_start:%Y-%m-%d %H:%M})"
        if self.sale_end and moment > self.sale_end:
            return "판매 종료"
        return None

    def is_open(self, now: datetime | None = None) -> bool:
        return self.blocked_reason(now) is None

    @classmethod
    def from_payload(cls, payload: dict[str, Any]) -> "NaverSlot | None":
        start = _parse_dt(payload.get("unitStartTime")) or _parse_dt(
            payload.get("unitStartDateTime"))
        if start is None:
            return None
        return cls(
            slot_id=str(payload.get("slotId") or ""),
            schedule_id=str(payload.get("scheduleId") or ""),
            detail_schedule_id=str(payload.get("detailScheduleId") or ""),
            composite_id=str(payload.get("id") or ""),
            start=start,
            stock=_as_int(payload.get("stock")),
            booking_count=_as_int(payload.get("bookingCount")),
            occupied=_as_int(payload.get("occupiedBookingCount")),
            is_business_day=bool(payload.get("isBusinessDay", True)),
            is_sale_day=bool(payload.get("isSaleDay", True)),
            is_unit_business_day=bool(payload.get("isUnitBusinessDay", True)),
            is_unit_sale_day=bool(payload.get("isUnitSaleDay", True)),
            is_holiday=bool(payload.get("isHoliday", False)),
            min_booking_count=_as_int(payload.get("minBookingCount"), 1),
            max_booking_count=_as_int(payload.get("maxBookingCount"), 1),
            sale_start=_parse_dt(payload.get("saleStartDateTime")),
            sale_end=_parse_dt(payload.get("saleEndDateTime")),
        )


@dataclass(frozen=True)
class NaverItemMeta:
    """The item-level facts worth checking before we start polling."""

    name: str
    server_time: datetime | None
    is_closed_booking: bool
    is_closed_for_user: bool
    open_at: datetime | None
    is_opened: bool
    uses_open_schedule: bool
    is_paused: bool
    custom_form: list[dict[str, Any]]

    def hard_block(self) -> str | None:
        """A reason polling is pointless, or None."""
        if self.is_closed_booking or self.is_closed_for_user:
            return "이 상품은 현재 예약이 닫혀 있습니다."
        if self.is_paused:
            return "판매가 일시 중지된 상품입니다."
        return None


@dataclass(frozen=True)
class NaverAccount:
    """Who the browser session belongs to, and the token the mutation needs."""

    is_logged_in: bool
    csrf_token: str
    is_sms_alarm: bool
    user_id: str = ""
    nickname: str = ""


class SubmitOutcome:
    """How a direct ``submitBooking`` attempt ended.

    The engine branches on these rather than on message text: ``NOT_OPEN`` means
    fire again in a moment, while ``REFUSED``/``DUPLICATED`` are ambiguous until
    the authenticated booking list is reconciled. ``ABUSE``/``PAYLOAD`` mean stop
    using the API path and let the page do it.
    """

    SUCCESS = "success"
    NOT_OPEN = "notopen"
    DUPLICATED = "duplicated"
    REFUSED = "refused"
    AUTH = "auth"
    ABUSE = "abuse"
    PAYLOAD = "payload"
    UNKNOWN = "unknown"
    ERROR = "error"


@dataclass(frozen=True)
class SubmitResult:
    outcome: str
    code: str = ""
    message: str = ""
    booking_id: str = ""
    url: Any = None

    @property
    def detail(self) -> str:
        parts = [part for part in (self.code, self.message) if part]
        return " · ".join(dict.fromkeys(parts)) or self.outcome


def classify_submit_error(code: str, message: str, reason: str = "") -> str:
    """Map a server refusal onto one of the ``SubmitOutcome`` values."""
    # Resolver-specific reasons are more informative than GraphQL's generic
    # BAD_USER_INPUT/BAD_REQUEST wrapper. Inspect the message first and do not let
    # that wrapper hide RT98 or the exact not-open refusal.
    for token in (reason, message, code):
        text = (token or "").strip()
        if not text:
            continue
        if text in SUBMIT_ABUSE_CODES:
            return SubmitOutcome.ABUSE
        if text in SUBMIT_NOT_OPEN_CODES:
            return SubmitOutcome.NOT_OPEN
        if text in SUBMIT_AUTH_CODES:
            return SubmitOutcome.AUTH
        if text in SUBMIT_DUPLICATE_CODES:
            return SubmitOutcome.DUPLICATED
        if text in SUBMIT_REFUSED_CODES:
            return SubmitOutcome.REFUSED
    for token in (code, message, reason):
        text = (token or "").strip()
        if text in SUBMIT_PAYLOAD_CODES:
            return SubmitOutcome.PAYLOAD
    haystack = f"{code} {message} {reason}"
    if "not opened" in haystack:
        return SubmitOutcome.NOT_OPEN
    if "Authentication" in haystack or "UNAUTHENTICATED" in haystack:
        return SubmitOutcome.AUTH
    return SubmitOutcome.ERROR


class NaverBookingApi:
    """Thin GraphQL client for public reads and classified booking responses."""

    def __init__(
        self,
        business_id: str,
        biz_item_id: str,
        service_id: str = DEFAULT_SERVICE_ID,
        session: requests.Session | None = None,
        log=None,
        timeout: float = REQUEST_TIMEOUT,
    ) -> None:
        self.business_id = str(business_id)
        self.biz_item_id = str(biz_item_id)
        self.service_id = str(service_id or DEFAULT_SERVICE_ID)
        self.log = log
        # Every call is bounded. An unbounded POST here would park a worker
        # thread forever and keep the engine's is_running flag stuck on.
        self.timeout = float(timeout) if timeout else REQUEST_TIMEOUT
        self._owns_session = session is None
        self.session = session or requests.Session()
        self.session.headers.setdefault("User-Agent", USER_AGENT)
        self.session.headers.setdefault("Accept-Language", "ko")
        self.last_rtt: float | None = None

    # -- plumbing -----------------------------------------------------------
    @property
    def item_url(self) -> str:
        return (f"https://m.booking.naver.com/booking/{self.service_id}"
                f"/bizes/{self.business_id}/items/{self.biz_item_id}")

    def _headers(self) -> dict[str, str]:
        return {
            "Content-Type": "application/json",
            "Origin": "https://m.booking.naver.com",
            "Referer": self.item_url,
        }

    def _post(self, operation: str, query: str, variables: dict[str, Any]) -> dict[str, Any]:
        payload = {"operationName": operation, "query": query, "variables": variables}
        before = time.monotonic()
        try:
            response = self.session.post(
                GRAPHQL_URL, json=payload, headers=self._headers(),
                timeout=self.timeout,
            )
        except requests.RequestException as exc:
            raise NaverApiError(f"네이버 API 연결 실패: {exc}") from exc
        after = time.monotonic()
        self.last_rtt = after - before

        try:
            body = response.json()
        except ValueError as exc:
            raise NaverApiError(
                f"네이버 API 응답을 해석하지 못했습니다 (HTTP {response.status_code})"
            ) from exc

        errors = body.get("errors")
        if errors:
            message = str((errors[0] or {}).get("message", ""))[:200]
            raise NaverApiError(f"네이버 API 오류: {message}")
        data = body.get("data")
        if not isinstance(data, dict):
            raise NaverApiError("네이버 API가 빈 응답을 반환했습니다.")
        # Anchoring the clock needs the midpoint of the request window, so it is
        # carried alongside the payload rather than re-measured by the caller.
        data["__rtt_window__"] = (before, after)
        return data

    def _post_body(
        self, operation: str, query: str, variables: dict[str, Any]
    ) -> dict[str, Any]:
        """POST and hand back the whole GraphQL body, errors included.

        ``_post`` turns an ``errors`` array into an exception, which is right for
        reads but wrong for the booking mutation: the refusal *code* is the useful
        part and it only exists in ``extensions``.
        """
        payload = {"operationName": operation, "query": query, "variables": variables}
        before = time.monotonic()
        try:
            response = self.session.post(
                GRAPHQL_URL, json=payload, headers=self._headers(),
                timeout=self.timeout,
            )
        except requests.RequestException as exc:
            raise NaverApiError(f"네이버 API 연결 실패: {exc}") from exc
        self.last_rtt = time.monotonic() - before
        try:
            body = response.json()
        except ValueError as exc:
            raise NaverApiError(
                f"네이버 API 응답을 해석하지 못했습니다 (HTTP {response.status_code})"
            ) from exc
        return body if isinstance(body, dict) else {}

    def attach_cookies(self, cookies) -> int:
        """Copy a browser's Naver cookies onto this session.

        The mutation needs the login the browser holds. Only naver.com cookies are
        taken, and their values are never logged.
        """
        added = 0
        for cookie in cookies or []:
            name = cookie.get("name")
            value = cookie.get("value")
            domain = cookie.get("domain") or ""
            if not name or value is None or "naver" not in domain:
                continue
            try:
                self.session.cookies.set(
                    name, value, domain=domain, path=cookie.get("path") or "/"
                )
                added += 1
            except Exception:
                continue
        return added

    def replace_cookies(self, cookies) -> int:
        """Replace only Naver cookies, preserving unrelated session cookies.

        Account switching can leave the requests cookie jar holding identifiers
        from the previous browser login.  Clearing that subset before copying the
        active Chrome jar guarantees subsequent authenticated reads describe the
        account the user most recently selected.
        """
        for cookie in list(self.session.cookies):
            if "naver" not in (cookie.domain or ""):
                continue
            try:
                self.session.cookies.clear(cookie.domain, cookie.path, cookie.name)
            except (KeyError, ValueError):
                continue
        return self.attach_cookies(cookies)

    def close(self) -> None:
        if self._owns_session:
            try:
                self.session.close()
            except Exception:
                pass

    # -- reads --------------------------------------------------------------
    def fetch_slots_raw(
        self, date_from: str, date_to: str | None = None
    ) -> list[dict[str, Any]]:
        """Raw hourly records between two ``YYYY-MM-DD`` dates, inclusive."""
        end = date_to or date_from
        data = self._post("hourlySchedule", HOURLY_SCHEDULE_QUERY, {
            "scheduleParams": {
                "businessId": self.business_id,
                "bizItemId": self.biz_item_id,
                "startDateTime": f"{date_from}T00:00:00",
                "endDateTime": f"{end}T23:59:59",
            },
        })
        schedule = (data.get("schedule") or {}).get("bizItemSchedule") or {}
        hourly = schedule.get("hourly") or []
        return [entry for entry in hourly if isinstance(entry, dict)]

    def fetch_slots(self, date_from: str, date_to: str | None = None) -> list[NaverSlot]:
        """Slots between two ``YYYY-MM-DD`` dates, inclusive."""
        hourly = self.fetch_slots_raw(date_from, date_to)
        slots = [NaverSlot.from_payload(entry) for entry in hourly if entry]
        return sorted((slot for slot in slots if slot), key=lambda item: item.start)

    def fetch_slot_raw(self, date_str: str, time_str: str) -> dict[str, Any] | None:
        """The page's complete hourly record for one date and ``HH:MM``."""
        wanted = (time_str or "")[:5]
        for entry in self.fetch_slots_raw(date_str):
            slot = NaverSlot.from_payload(entry)
            if slot is not None and slot.time_str == wanted:
                return entry
        return None

    def fetch_slot_post_payment(self, slot_id: str) -> bool | None:
        """Return the official selected-slot payment timing.

        The GraphQL field is nullable.  Naver's own client interprets both
        ``null`` and ``false`` as immediate payment, so a successful response
        containing either value returns ``False`` here.  ``None`` is reserved for
        an incomplete response where the timing could not be resolved.
        """
        if not slot_id:
            return None
        data = self._post("Slot", SLOT_PAYMENT_QUERY, {
            "slotSeatInput": {
                "businessId": self.business_id,
                "bizItemId": self.biz_item_id,
                "slotId": str(slot_id),
            },
        })
        slot_seat = data.get("slotSeat") or {}
        slot = slot_seat.get("slot") if isinstance(slot_seat, dict) else None
        if not isinstance(slot, dict) or "isPostPayment" not in slot:
            return None
        return slot.get("isPostPayment") is True

    def find_slot(self, date_str: str, time_str: str) -> NaverSlot | None:
        """The slot for one date and ``HH:MM``, or None when it does not exist yet."""
        wanted = (time_str or "")[:5]
        for slot in self.fetch_slots(date_str):
            if slot.time_str == wanted:
                return slot
        return None

    def resolve_target_open_at(
        self, date_str: str, meta: NaverItemMeta
    ) -> datetime | None:
        """Translate the item's rolling open marker to one reservation date.

        For rolling calendars Naver's ``bookableSettingJson.openDateTime`` is the
        opening moment of the *latest date currently published*, not the opening
        moment of every date a user may type.  The public schedule response tells
        us which date that marker belongs to: take its latest published day and
        shift the marker by the calendar-day distance to ``date_str``.

        ``isOpened`` alone cannot distinguish a one-shot announcement from a
        rolling calendar observed early.  Published schedule history is the
        discriminator: history means rolling; no history keeps Naver's raw value.
        Any lookup/parsing failure also keeps Naver's raw value.
        """
        announced = meta.open_at
        if (
            announced is None
            or not meta.uses_open_schedule
        ):
            return announced

        try:
            target_day = datetime.strptime(date_str, "%Y-%m-%d").date()
        except (TypeError, ValueError):
            return announced

        server_day = (
            meta.server_time.date()
            if meta.server_time is not None
            else datetime.now(KST).date()
        )
        # Include enough history to survive sparse/weekly calendars and include
        # the target itself when it is already open. GraphQL simply omits closed
        # future days, so the maximum returned date remains the rolling anchor.
        date_from = min(server_day, target_day) - timedelta(days=35)
        date_to = max(server_day, target_day) + timedelta(days=35)
        try:
            published = self.fetch_slots(date_from.isoformat(), date_to.isoformat())
        except NaverApiError:
            return announced
        published_days = {slot.start.date() for slot in published if slot is not None}
        if not published_days:
            return announced

        # If the requested date is already in Naver's schedule response, the
        # announced marker already applies.  Using a later published day as the
        # anchor would move this target into the past (the live Channel 27 case
        # moved 2026-08-17 nine days early because 2026-08-26 was also visible).
        if target_day in published_days:
            return announced

        anchor_day = max(published_days)
        projected = announced + timedelta(days=(target_day - anchor_day).days)
        # A rolling projection may move an unopened future date later, but it
        # must never authorize a submit before Naver's explicit opening marker.
        return max(announced, projected)

    def fetch_item_meta(self) -> NaverItemMeta:
        data = self._post("bizItem", BIZ_ITEM_QUERY, {
            "input": {
                "businessId": self.business_id,
                "bizItemId": self.biz_item_id,
                "lang": "ko",
            },
        })
        item = data.get("bizItem") or {}
        bookable = _as_json(item.get("bookableSettingJson"))
        custom_form = item.get("customFormJson")
        return NaverItemMeta(
            name=str(item.get("name") or ""),
            server_time=_parse_dt(item.get("currentDateTime")),
            is_closed_booking=bool(item.get("isClosedBooking")),
            is_closed_for_user=bool(item.get("isClosedBookingUser")),
            open_at=_parse_dt(bookable.get("openDateTime")),
            is_opened=bool(bookable.get("isOpened", True)),
            uses_open_schedule=bool(bookable.get("isUseOpen")),
            is_paused=bool(bookable.get("isPaused")),
            custom_form=custom_form if isinstance(custom_form, list) else [],
        )

    def fetch_business_form(self) -> list[dict[str, Any]]:
        """The extra questions asked on the request page.

        The participant-count dropdown lives here, not on the bizItem, for the
        sample business. Knowing the exact option strings up front removes the
        selector guessing the engine used to do.
        """
        business = self.fetch_business()
        form = business.get("customFormJson")
        return form if isinstance(form, list) else []

    def fetch_business(self) -> dict[str, Any]:
        """The raw business record, which is most of the booking payload."""
        try:
            data = self._post("business", BUSINESS_QUERY, {
                "input": {
                    "businessId": self.business_id,
                    "lang": "ko",
                    "isOwner": False,
                },
            })
        except NaverApiError:
            return {}
        business = data.get("business")
        return business if isinstance(business, dict) else {}

    def fetch_biz_item_raw(self) -> dict[str, Any]:
        """The raw bizItem record, for the fields the payload needs verbatim."""
        try:
            data = self._post("bizItem", BIZ_ITEM_QUERY, {
                "input": {
                    "businessId": self.business_id,
                    "bizItemId": self.biz_item_id,
                    "lang": "ko",
                },
            })
        except NaverApiError:
            return {}
        item = data.get("bizItem")
        return item if isinstance(item, dict) else {}

    def fetch_account(self) -> NaverAccount:
        """Login state plus the ``csrfToken`` the booking mutation carries."""
        try:
            data = self._post("account", ACCOUNT_QUERY, {})
        except NaverApiError:
            return NaverAccount(False, "", False)
        account = data.get("account")
        if not isinstance(account, dict):
            return NaverAccount(False, "", False)
        return NaverAccount(
            is_logged_in=bool(account.get("isLoggedIn")),
            csrf_token=str(account.get("csrfToken") or ""),
            is_sms_alarm=bool(account.get("isSmsAlarm")),
            user_id=str(account.get("userId") or ""),
            nickname=str(account.get("nickname") or ""),
        )

    # -- the booking request ------------------------------------------------
    def submit_booking(self, params: dict[str, Any]) -> SubmitResult:
        """Send one booking request and classify the answer.

        This is the only write in the file. It is a single POST, so everything
        expensive -- business and item metadata, the answered custom form, the
        account token -- can be assembled before the opening moment and the moment
        itself costs one round trip (measured 86-97 ms) instead of a page load.
        """
        try:
            body = self._post_body("submitBooking", SUBMIT_BOOKING_MUTATION,
                                   {"input": params})
        except NaverApiError as exc:
            return SubmitResult(SubmitOutcome.ERROR, message=str(exc)[:160])

        errors = body.get("errors") or []
        if errors:
            first = errors[0] if isinstance(errors[0], dict) else {}
            extensions = first.get("extensions") or {}
            code = str(extensions.get("code") or "")
            message = str(first.get("message") or "")
            reason = str(extensions.get("reason") or "")
            return SubmitResult(
                classify_submit_error(code, message, reason),
                code=code or message,
                message=reason or message,
            )

        booking = ((body.get("data") or {}).get("submitBooking")) or {}
        booking_id = str(booking.get("bookingId") or "")
        if booking_id:
            return SubmitResult(
                SubmitOutcome.SUCCESS, booking_id=booking_id, url=booking.get("url")
            )
        return SubmitResult(SubmitOutcome.ERROR, message="예약번호가 비어 있습니다")

    def is_logged_in(self) -> bool | None:
        """True/False when the endpoint answers, None when it cannot be told."""
        try:
            data = self._post("account", ACCOUNT_QUERY, {})
        except NaverApiError:
            return None
        account = data.get("account")
        if not isinstance(account, dict):
            return None
        return bool(account.get("isLoggedIn"))


class NaverServerClock:
    """Naver's clock, anchored to ``time.monotonic()``.

    Unlike keyescape -- where only a whole-second ``Date`` header is available and
    the second boundary has to be caught by polling -- ``bizItem.currentDateTime``
    carries milliseconds. Live measurements on 2026-08-30 showed that this field
    advances with response completion rather than request arrival: anchoring it
    to the request midpoint therefore made the local server clock run roughly
    half an RTT ahead. Startup uses one sample; the final pre-open refresh retains
    the quickest response-completion sample to reduce queueing noise.

    The anchor is monotonic on purpose: an NTP correction part-way through a run
    cannot shift what the engine believes the server time to be.
    """

    def __init__(self, api: NaverBookingApi, log=None) -> None:
        self.api = api
        self.log = log
        self._anchor_monotonic: float | None = None
        self._anchor_server: float | None = None
        self.last_offset: float | None = None
        self.last_precision: float | None = None

    @property
    def synced(self) -> bool:
        return self._anchor_server is not None

    def now(self) -> float:
        if self._anchor_server is None or self._anchor_monotonic is None:
            return time.time()
        return self._anchor_server + (time.monotonic() - self._anchor_monotonic)

    def now_kst(self) -> datetime:
        return datetime.fromtimestamp(self.now(), KST)

    def seconds_until(self, target_epoch: float) -> float:
        return target_epoch - self.now()

    def sync(self, announce: bool = False) -> bool:
        return self._sync_samples(1, announce=announce)

    def sync_precise(self, sample_count: int = 3, announce: bool = False) -> bool:
        """Anchor to the lowest-RTT read-only sample near an opening."""
        return self._sync_samples(
            max(2, min(int(sample_count or 3), 5)), announce=announce
        )

    def _sync_samples(self, sample_count: int, *, announce: bool) -> bool:
        samples: list[tuple[float, float, float]] = []
        last_error: Exception | None = None
        missing_server_time = False
        for _index in range(max(1, int(sample_count))):
            before = time.monotonic()
            try:
                meta = self.api.fetch_item_meta()
            except NaverApiError as exc:
                last_error = exc
                continue
            after = time.monotonic()
            if meta.server_time is None:
                missing_server_time = True
                continue
            samples.append((after - before, after, meta.server_time.timestamp()))

        if not samples:
            if announce and self.log:
                if missing_server_time:
                    self.log(
                        "[경고] 서버 시간 필드가 비어 있습니다. 로컬 시계로 진행합니다.",
                        "warning",
                    )
                else:
                    suffix = f" ({last_error})" if last_error else ""
                    self.log(
                        f"[경고] 네이버 서버 시간을 읽지 못했습니다. "
                        f"로컬 시계로 진행합니다.{suffix}",
                        "warning",
                    )
            return False

        round_trip, response_end, server_epoch = min(samples, key=lambda row: row[0])
        self._anchor_server = server_epoch
        self._anchor_monotonic = response_end
        # RTT and sample disagreement are estimates, not a measured arrival
        # error. Never cap a slow/noisy clock at an artificial 25 ms precision.
        mappings = [server - end for _rtt, end, server in samples]
        spread = max(mappings) - min(mappings)
        self.last_precision = max(round_trip / 2, spread / 2)
        self.last_offset = self._anchor_server - (
            time.time() - (time.monotonic() - response_end)
        )
        if announce and self.log:
            self.log(
                f"서버 시간 동기화 완료 · 로컬 시계와 차이 {self.last_offset:+.2f}초 · "
                f"표본 기준 불확실성 약 {self.last_precision * 1000:.0f}ms",
                "success",
            )
            if abs(self.last_offset) > 5:
                self.log(
                    f"[경고] 이 PC의 시계가 서버보다 {abs(self.last_offset):.0f}초 "
                    f"{'느립니다' if self.last_offset > 0 else '빠릅니다'}. "
                    "이후 시각 표시는 서버 시간을 기준으로 합니다.",
                    "warning",
                )
        return True


def participant_option(form: Iterable[dict[str, Any]], people: str) -> tuple[str, str] | None:
    """Match a people count against a custom form's SELECT options.

    Returns ``(question_title, option_value)`` so the caller can target the exact
    control and the exact option text instead of guessing at ``"{n}인"``.
    """
    wanted = str(people or "").strip()
    if not wanted:
        return None
    digits = re.sub(r"\D", "", wanted)
    for question in form or []:
        if not isinstance(question, dict):
            continue
        if str(question.get("type", "")).upper() != "SELECT":
            continue
        options = question.get("options")
        if not isinstance(options, list):
            continue
        for option in options:
            if not isinstance(option, dict):
                continue
            value = str(option.get("value") or "")
            if not value:
                continue
            if value == wanted or (digits and re.sub(r"\D", "", value) == digits):
                return str(question.get("title") or ""), value
    return None
