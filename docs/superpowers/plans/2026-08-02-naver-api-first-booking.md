# Naver API-first Booking Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the Naver opening-time page reload with a prepared, authenticated `submitBooking` GraphQL request executed inside the logged-in Playwright page.

**Architecture:** A focused `engines/naver_submit.py` module builds and validates `SubmitBookingParams`, classifies browser-fetch responses, and owns the page-context transport. `NaverEngine` prepares this path after login, uses it first at opening/cancellation time, and keeps the existing page flow as a per-run fallback. The public API client gains the full schedule fields required by the page's current booking-state builder.

**Tech Stack:** Python 3.14, asyncio, dataclasses, requests, Playwright async API, pytest.

## Global Constraints

- Never send a booking mutation in developer mode.
- Never log or persist cookies, CSRF tokens, complete payloads, names, phone numbers, or email addresses.
- Support only non-seat, non-period-fixed, non-Naver-Pay-direct EPISODE products in the first direct-submit implementation.
- Retry only `BizItem is not opened.`, at most three total mutation attempts and within 350ms.
- Disable direct submit for the run on authentication, payload, abuse (`RT98`), or transport errors.
- Preserve the existing browser submission path as fallback.
- Do not stage or commit the already-modified production files because they contain pre-existing user changes.

---

### Task 1: Full schedule record and payload builder

**Files:**
- Create: `engines/naver_submit.py`
- Modify: `engines/naver_api.py:79-106`
- Test: `tests/test_naver_submit.py`
- Test: `tests/test_naver_api.py`

**Interfaces:**
- Consumes: `NaverAccount`, raw business/bizItem dictionaries, a raw hourly schedule dictionary, and the existing reservation-data mapping.
- Produces: `NaverSubmitPreparation`, `NaverSubmitPayloadBuilder.prepare`, `NaverBookingApi.fetch_slot_raw(date, time)`.

- [ ] **Step 1: Write failing tests for the full slot record**

```python
def test_fetch_slot_raw_keeps_page_booking_fields(monkeypatch):
    api, calls = api_with(monkeypatch, {
        "data": {"schedule": {"bizItemSchedule": {"hourly": [{
            "name": "", "slotId": "1331382668",
            "unitStartDateTime": "2026-08-08T05:30:00Z",
            "duration": None, "desc": "",
            "minBookingCount": 1, "maxBookingCount": 1,
            "prices": [{"priceId": "8895079", "price": 33000,
                        "name": "1인", "isImp": True}],
        }]}}}
    })
    slot = api.fetch_slot_raw("2026-08-08", "14:30")
    assert slot["slotId"] == "1331382668"
    assert slot["prices"][0]["price"] == 33000
```

- [ ] **Step 2: Run the slot test and verify RED**

Run: `py -3 -m pytest tests/test_naver_api.py::test_fetch_slot_raw_keeps_page_booking_fields -q`

Expected: FAIL because `fetch_slot_raw` does not exist.

- [ ] **Step 3: Add the current Naver bundle's full hourly fields**

Add `name`, `duration`, `desc`, `seatGroups`, and the complete `prices` selection from the live `hourlySchedule` query to `HOURLY_SCHEDULE_QUERY`. Add:

```python
def fetch_slots_raw(self, date_from: str, date_to: str | None = None) -> list[dict[str, Any]]:
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
    return [entry for entry in (schedule.get("hourly") or [])
            if isinstance(entry, dict)]

def fetch_slot_raw(self, date_str: str, time_str: str) -> dict[str, Any] | None:
    wanted = (time_str or "")[:5]
    for entry in self.fetch_slots_raw(date_str):
        slot = NaverSlot.from_payload(entry)
        if slot is not None and slot.time_str == wanted:
            return entry
    return None
```

`fetch_slots()` must continue returning `NaverSlot` objects by mapping the raw list so existing polling behavior is unchanged.

- [ ] **Step 4: Run the slot test and verify GREEN**

Run: `py -3 -m pytest tests/test_naver_api.py -q`

Expected: all Naver API tests pass.

- [ ] **Step 5: Write failing payload-builder tests**

Use literal business, item, slot, account, and reservation fixtures. Assert consumer behavior:

```python
def test_builder_produces_episode_submit_input():
    prepared = NaverSubmitPayloadBuilder().prepare(
        business=BUSINESS, biz_item=BIZ_ITEM, slot=SLOT,
        account=NaverAccount(True, "csrf-secret", False),
        reservation={"name": "홍길동", "phone": "010-1234-5678",
                     "people": "3", "reservationDate": "2026-08-08",
                     "reservationTime": "14:30"},
    )
    assert prepared.ready is True
    assert prepared.payload["slotId"] == "1331382668"
    assert prepared.payload["startMinute"] == 870
    assert prepared.payload["price"] == 33000
    assert prepared.payload["priceTypeJson"][0]["bookingCount"] == 1
    assert prepared.payload["phone"] == "01012345678"
    assert prepared.payload["customFormInputJson"][0]["value"] == "3인"
```

Add separate tests for missing login/CSRF, mismatched slot time, missing required custom form, seat/period/payment products, and secret-safe failure reasons.

- [ ] **Step 6: Run payload tests and verify RED**

Run: `py -3 -m pytest tests/test_naver_submit.py -q`

Expected: FAIL because `engines.naver_submit` does not exist.

- [ ] **Step 7: Implement the minimal payload model and builder**

Create:

```python
@dataclass(frozen=True)
class NaverSubmitPreparation:
    ready: bool
    payload: dict[str, Any] = field(default_factory=dict)
    reason: str = ""
    slot_id: str = ""

class NaverSubmitPayloadBuilder:
    TERMS_VERSION = "20251030"

    def prepare(
        self, *, business: dict[str, Any], biz_item: dict[str, Any],
        slot: dict[str, Any], account: NaverAccount,
        reservation: Mapping[str, Any],
    ) -> NaverSubmitPreparation:
        if not account.is_logged_in or not account.csrf_token:
            return NaverSubmitPreparation(False, reason="로그인 또는 CSRF 확인 실패")
        if biz_item.get("isSeatUsed") or biz_item.get("isPeriodFixed"):
            return NaverSubmitPreparation(False, reason="좌석제/기간제 직접 제출 미지원")
        payload = self._build_supported_episode(
            business, biz_item, slot, account, reservation)
        missing = [key for key in (
            "businessId", "bizItemId", "slotId", "csrfToken",
            "startDateTime", "name", "phone",
        ) if not payload.get(key)]
        if missing:
            return NaverSubmitPreparation(
                False, reason="필수 제출 필드 누락: " + ", ".join(missing))
        return NaverSubmitPreparation(
            True, payload=payload, slot_id=str(payload["slotId"]))
```

Match the current Naver bundle's `ln()` payload for supported EPISODE products:

- use `rawNames.name/serviceName`;
- preserve address/translation/payment JSON values;
- create one selected default price with `bookingCount=1`;
- calculate `bizItemPrice` and `price`;
- set `startDateTime/endDateTime` to ISO UTC and `startMinute/endMinute` to KST minutes;
- copy the matching SELECT custom form and set `value/originalValue`;
- set `termsVersion`, `globalTimezone`, `bookingCondition`, `isAdminBooking`;
- exclude Python-only `None` fields only when JavaScript JSON serialization would omit `undefined`; retain explicit `null` fields from the page builder.

- [ ] **Step 8: Run payload tests and verify GREEN**

Run: `py -3 -m pytest tests/test_naver_submit.py tests/test_naver_api.py -q`

Expected: all tests pass.

---

### Task 2: Logged-in browser GraphQL transport

**Files:**
- Modify: `engines/naver_submit.py`
- Test: `tests/test_naver_submit.py`

**Interfaces:**
- Consumes: a Playwright page and a prepared payload.
- Produces: `NaverBrowserSubmitter.fetch_account()`, `NaverBrowserSubmitter.submit(payload)`, each returning domain models without secrets in errors.

- [ ] **Step 1: Write failing browser-transport tests**

Use a fake page whose `evaluate(script, arg)` executes at the test boundary and records only operation names. Cover:

```python
@pytest.mark.asyncio
async def test_browser_submitter_classifies_success():
    page = FakePage({"data": {"submitBooking": {
        "bookingId": "999888", "url": "/my/bookings/999888"}}})
    result = await NaverBrowserSubmitter(page).submit({"slotId": "1331382668"})
    assert result.outcome == SubmitOutcome.SUCCESS
    assert result.booking_id == "999888"

@pytest.mark.asyncio
async def test_browser_submitter_redacts_payload_from_transport_errors():
    page = RaisingPage("csrf-secret 01012345678")
    result = await NaverBrowserSubmitter(page).submit(
        {"csrfToken": "csrf-secret", "phone": "01012345678"})
    assert "csrf-secret" not in result.detail
    assert "01012345678" not in result.detail
```

Also test `RT98`, authentication, payload error, not-open, malformed body, and account-query parsing.

- [ ] **Step 2: Run browser tests and verify RED**

Run: `py -3 -m pytest tests/test_naver_submit.py -q`

Expected: FAIL because `NaverBrowserSubmitter` does not exist.

- [ ] **Step 3: Implement the page-context transport**

Add constant JavaScript functions that call same-origin `/graphql` with:

```javascript
{
  method: "POST",
  credentials: "include",
  headers: {"Content-Type": "application/json"},
  body: JSON.stringify({
    operationName: "submitBooking",
    query: SUBMIT_BOOKING_MUTATION,
    variables: {input: payload}
  })
}
```

Return only the parsed JSON body and HTTP status. Catch exceptions in Python and return a fixed, secret-free transport summary. Reuse `classify_submit_error()` and `SubmitResult`.

- [ ] **Step 4: Run browser tests and verify GREEN**

Run: `py -3 -m pytest tests/test_naver_submit.py -q`

Expected: all submit-module tests pass.

---

### Task 3: API preparation in `NaverEngine`

**Files:**
- Modify: `engines/naver_engine.py:372-580`
- Test: `tests/test_naver_engine.py`

**Interfaces:**
- Consumes: `NaverSubmitPayloadBuilder`, `NaverBrowserSubmitter`, engine API and browser page.
- Produces: engine fields `_api_submitter`, `_api_preparation`, `_api_submit_enabled` and `_prepare_api_submit`.

- [ ] **Step 1: Write failing preparation tests**

Cover ready, unsupported, developer-mode, and refresh cases:

```python
@pytest.mark.asyncio
async def test_prepare_api_submit_arms_direct_path():
    engine = make_engine()
    engine.api = FakePreparationApi(BUSINESS, BIZ_ITEM, SLOT)
    engine._page = AccountPage(ACCOUNT_BODY)
    await engine._prepare_api_submit(RESERVATION, dev_mode=False)
    assert engine._api_submit_enabled is True
    assert engine._api_preparation.slot_id == "1331382668"

@pytest.mark.asyncio
async def test_developer_mode_prepares_but_never_arms_transport():
    engine = make_engine()
    engine.api = FakePreparationApi(BUSINESS, BIZ_ITEM, SLOT)
    engine._page = AccountPage(ACCOUNT_BODY)
    await engine._prepare_api_submit(RESERVATION, dev_mode=True)
    assert engine._api_submit_enabled is False
    assert "개발자" in "\n".join(engine.logged_messages)
```

- [ ] **Step 2: Run preparation tests and verify RED**

Run: `py -3 -m pytest tests/test_naver_engine.py -q`

Expected: FAIL because `_prepare_api_submit` and API state fields do not exist.

- [ ] **Step 3: Implement preparation after browser login**

Initialize API state in `__init__`. After `_open_browser()` has confirmed login:

1. create `NaverBrowserSubmitter(self._page)`;
2. fetch browser account;
3. fetch raw business, raw item, and raw target slot in worker threads;
4. build and validate preparation;
5. log either `API 직접 제출 준비 완료` or a secret-free browser-fallback reason.

Preparation must occur even before the published opening because the schedule API already exposes the future slot. If the raw slot is absent, leave API disabled and refresh preparation when the slot first appears.

- [ ] **Step 4: Run preparation tests and verify GREEN**

Run: `py -3 -m pytest tests/test_naver_engine.py -q`

Expected: all engine tests pass.

---

### Task 4: API-first strike and fallback state machine

**Files:**
- Modify: `engines/naver_engine.py:585-801`
- Modify: `engines/naver_engine.py:1026-1120`
- Test: `tests/test_naver_engine.py`

**Interfaces:**
- Consumes: prepared payload and `SubmitOutcome`.
- Produces: `_submit_api_first`, `_strike_at_open` API branch, cancellation-path API branch, and per-run fallback state.

- [ ] **Step 1: Write failing strike tests**

Add behavior tests:

```python
@pytest.mark.asyncio
async def test_open_strike_uses_api_without_page_reload():
    engine = armed_engine([SubmitResult(SubmitOutcome.SUCCESS,
                                        booking_id="999888")])
    engine._goto_item = fail_if_called
    engine._submit = fail_if_called
    outcome, detail = await engine._strike_at_open(
        "2026-08-08", "14:30", RESERVATION, False)
    assert outcome == "success"
    assert "999888" in detail

@pytest.mark.asyncio
async def test_not_open_retries_but_abuse_disables_api_and_falls_back():
    engine = armed_engine([
        SubmitResult(SubmitOutcome.NOT_OPEN),
        SubmitResult(SubmitOutcome.ABUSE, code="RT98"),
    ])
    browser_submit_calls = 0
    async def browser_submit(*args):
        nonlocal browser_submit_calls
        browser_submit_calls += 1
        return "taken", "화면 매진"
    engine._submit = browser_submit
    await engine._strike_at_open(
        "2026-08-08", "14:30", RESERVATION, False)
    assert engine.api_submit_calls == 2
    assert engine._api_submit_enabled is False
    assert browser_submit_calls == 1
```

Add separate tests for refused/taken, auth, payload, transport error, developer mode, and the 350ms/three-attempt caps.

- [ ] **Step 2: Run strike tests and verify RED**

Run: `py -3 -m pytest tests/test_naver_engine.py -q`

Expected: FAIL because the strike still reloads the page.

- [ ] **Step 3: Implement direct-submit outcome mapping**

Add:

```python
async def _submit_api_first(self, reservation_data) -> tuple[str, str]:
    deadline = time.monotonic() + self.API_NOT_OPEN_WINDOW_SECONDS
    for attempt in range(self.API_SUBMIT_MAX_ATTEMPTS):
        result = await self._api_submitter.submit(
            self._api_preparation.payload)
        if result.outcome == SubmitOutcome.SUCCESS:
            return "success", result.booking_id or str(result.url or "")
        if result.outcome == SubmitOutcome.NOT_OPEN:
            if attempt + 1 < self.API_SUBMIT_MAX_ATTEMPTS \
                    and time.monotonic() < deadline:
                continue
            return "notready", result.detail
        if result.outcome == SubmitOutcome.REFUSED:
            return "taken", result.detail
        self._disable_api_submit(result.detail)
        return "fallback", result.detail
    return "notready", "오픈 응답 제한 초과"

def _disable_api_submit(self, reason: str) -> None:
    self._api_submit_enabled = False
    self._api_preparation = None
    self.log(
        f"[경고] API 직접 제출 비활성화 · {reason[:120]} · "
        "브라우저 제출로 전환합니다.",
        "warning",
    )
```

Map `SubmitOutcome` to engine outcomes:

- `SUCCESS -> success`
- `NOT_OPEN -> internal bounded retry`
- `REFUSED -> taken`
- `AUTH/PAYLOAD/ABUSE/ERROR -> disable API then call browser _submit`

At opening, skip `_goto_item()` entirely while API is armed. For a cancellation detected by polling, use the same direct path before browser fallback. Refresh the raw slot and rebuild only outside the strike critical section.

- [ ] **Step 4: Add final pre-open clock sync and bounded latency compensation**

At the armed strike:

- sync once when the opening boundary is within five seconds;
- calculate one-way estimate as `clamp(last_rtt / 2, 0.010, 0.100)`;
- begin the first fetch at `open_at - one_way`;
- log send offset and measured response RTT without payload data.

- [ ] **Step 5: Run engine tests and verify GREEN**

Run: `py -3 -m pytest tests/test_naver_engine.py tests/test_naver_submit.py tests/test_naver_api.py -q`

Expected: all Naver tests pass.

---

### Task 5: Documentation and complete verification

**Files:**
- Modify: `README.md:3-7`
- Modify: `reference/naver/submit_booking.md`
- Test: all tests

**Interfaces:**
- Consumes: final observable engine behavior.
- Produces: user-facing distinction between lookup API, direct-submit-ready API, and browser fallback.

- [ ] **Step 1: Update user-facing documentation**

Document:

- API-first direct submission is limited to supported non-seat/non-payment EPISODE products;
- logs explicitly say when browser fallback is active;
- developer mode never sends `submitBooking`;
- direct submission removes rendering delay but cannot guarantee a reservation.

Correct the evidence mismatch in `reference/naver/submit_booking.md`: retain unauthenticated probe facts as such and add the current bundle extraction date and exact supported verification level.

- [ ] **Step 2: Run focused tests**

Run: `py -3 -m pytest tests/test_naver_api.py tests/test_naver_submit.py tests/test_naver_engine.py -q`

Expected: all focused tests pass.

- [ ] **Step 3: Run the complete suite**

Run: `py -3 -m pytest -q`

Expected: all tests pass with zero failures.

- [ ] **Step 4: Run syntax and diff checks**

Run:

```powershell
py -3 -m py_compile engines/naver_api.py engines/naver_submit.py engines/naver_engine.py
git diff --check -- engines/naver_api.py engines/naver_submit.py engines/naver_engine.py tests/test_naver_api.py tests/test_naver_submit.py tests/test_naver_engine.py README.md reference/naver/submit_booking.md
```

Expected: both commands exit with status 0.

- [ ] **Step 5: Run live non-destructive checks**

Use the public sample item to verify:

- full schedule query returns the 14:30 slot and prices;
- business/item queries return builder-required keys;
- unauthenticated `submitBooking` still returns authentication refusal.

Do not attach browser cookies and do not submit an authenticated mutation.

- [ ] **Step 6: Review the final diff**

Confirm that only planned Naver files and their tests/docs changed during this implementation, and that no secret values appear in the diff.
