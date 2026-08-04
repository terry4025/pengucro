from __future__ import annotations
import pytest
from datetime import datetime, timezone, timedelta
from unittest.mock import MagicMock
from engines.time_slot_fetchers import (
    fetch_any_time_slots,
    fetch_zeroworld_slots,
    fetch_keyescape_slots,
    fetch_jigubyeol_slots,
    fetch_doomescape_slots,
    fetch_naver_slots
)

class MockResponse:
    def __init__(self, status_code, content_or_text, is_json=False):
        self.status_code = status_code
        if is_json:
            self._json = content_or_text
            self.content = b""
            self.text = ""
        else:
            self._json = None
            if isinstance(content_or_text, bytes):
                self.content = content_or_text
                self.text = content_or_text.decode("utf-8")
            else:
                self.content = content_or_text.encode("utf-8")
                self.text = content_or_text

    def json(self):
        if self._json is not None:
            return self._json
        raise ValueError("Not JSON")

    def raise_for_status(self):
        if self.status_code >= 400:
            raise Exception("HTTP Error")


def test_fetch_zeroworld_slots(monkeypatch):
    import requests
    # 가상의 제로월드 시간표 HTML 응답
    mock_html = """
    <html>
        <body>
            <a href="javascript:fun_theme_time_select('1234')">12:00 (예약가능)</a>
            <button class="disable">14:00 (예약불가)</button>
        </body>
    </html>
    """
    monkeypatch.setattr(requests, "post", lambda *args, **kwargs: MockResponse(200, mock_html))
    
    slots = fetch_zeroworld_slots(
        base_url="https://zeroworld.com",
        branch_id="1",
        theme_id="10",
        date_str="2026-07-28",
        engine_options={},
        timeout=5.0
    )
    
    assert len(slots) == 2
    assert slots[0].time == "12:00"
    assert slots[0].available is True
    assert slots[1].time == "14:00"
    assert slots[1].available is False


def test_fetch_keyescape_slots(monkeypatch):
    import requests
    # 키이스케이프 JSON 응답 모킹
    mock_json = {
        "status": True,
        "data": [
            {"num": "111", "hh": "10", "mm": "30", "enable": "Y"},
            {"num": "222", "hh": "13", "mm": "0", "enable": "N"}
        ]
    }
    monkeypatch.setattr(requests, "post", lambda *args, **kwargs: MockResponse(200, mock_json, is_json=True))
    
    slots = fetch_keyescape_slots(
        base_url="https://keyescape.com",
        branch_id="1",
        theme_id="99",
        date_str="2026-07-28",
        timeout=5.0
    )
    
    assert len(slots) == 2
    assert slots[0].time == "10:30"
    assert slots[0].slot_id == "111"
    assert slots[0].available is True
    assert slots[1].time == "13:00"
    assert slots[1].slot_id == "222"
    assert slots[1].available is False


def test_fetch_jigubyeol_slots(monkeypatch):
    import requests
    # 지구별방탈출 가상 HTML 응답 모킹 (실제 play33 버튼 형태)
    mock_html = """
    <html>
        <body>
            <button type="button">
                예약가능
                <span>11:30</span>
            </button>
            <button disabled="" type="button">
                예약불가
                <span>13:00</span>
            </button>
        </body>
    </html>
    """
    monkeypatch.setattr(requests, "get", lambda *args, **kwargs: MockResponse(200, mock_html))
    
    slots = fetch_jigubyeol_slots(
        base_url="https://jigubyeol.com",
        branch_id="1",
        theme_id="5",
        date_str="2026-07-28",
        timeout=5.0
    )
    
    assert len(slots) == 2
    assert slots[0].time == "11:30"
    assert slots[0].available is True
    assert slots[1].time == "13:00"
    assert slots[1].available is False


def test_fetch_naver_slots(monkeypatch):
    """Slots now come from the GraphQL hourlySchedule query.

    The old REST endpoint (api.booking.naver.com/v3.0/.../schedules) returns
    403 NotAccessibleUrl, so this fetcher was returning nothing at all.
    Availability is ``bookingCount < stock``: verified against the rendered page,
    a 매진 slot reports stock 1 / bookingCount 1 and a free one stock 1 /
    bookingCount 0.
    """
    import requests

    def slot(minute_text, booked):
        return {
            "id": f"456_1_{minute_text}",
            "slotId": f"slot-{minute_text}",
            "scheduleId": "999",
            "detailScheduleId": None,
            "unitStartDateTime": None,
            "unitStartTime": f"2026-07-28 {minute_text}:00",
            "stock": 1,
            "bookingCount": booked,
            "occupiedBookingCount": 0,
            "unitStock": 1,
            "unitBookingCount": booked,
            "isBusinessDay": True,
            "isSaleDay": True,
            "isUnitSaleDay": True,
            "isUnitBusinessDay": True,
            "isHoliday": False,
            "minBookingCount": 1,
            "maxBookingCount": 1,
            "saleStartDateTime": None,
            "saleEndDateTime": None,
        }

    payload = {
        "data": {
            "schedule": {
                "bizItemSchedule": {
                    "hourly": [slot("15:30", 1), slot("10:00", 0)],
                }
            }
        }
    }
    captured = {}

    def fake_post(self, url, **kwargs):
        captured["url"] = url
        captured["variables"] = kwargs.get("json", {}).get("variables")
        captured["timeout"] = kwargs.get("timeout")
        return MockResponse(200, payload, is_json=True)

    monkeypatch.setattr(requests.Session, "post", fake_post)

    slots = fetch_naver_slots(
        url="https://booking.naver.com/booking/12/bizes/123/items/456",
        date_str="2026-07-28",
        timeout=5.0,
    )

    assert captured["url"] == "https://m.booking.naver.com/graphql"
    assert captured["timeout"] == 5.0
    assert captured["variables"]["scheduleParams"]["businessId"] == "123"
    assert captured["variables"]["scheduleParams"]["bizItemId"] == "456"

    assert len(slots) == 2
    assert slots[0].time == "10:00"
    assert slots[0].available is True
    assert slots[0].slot_id == "slot-10:00"
    assert slots[1].time == "15:30"
    assert slots[1].available is False


def test_fetch_naver_slots_without_item_id_returns_empty(monkeypatch):
    """No bizItemId means there is no schedule to ask about."""
    import engines.site_parser as sp

    monkeypatch.setattr(sp, "normalize_naver_url", lambda url: url)
    assert fetch_naver_slots(
        url="https://booking.naver.com/booking/12/bizes/123",
        date_str="2026-07-28",
        timeout=5.0,
    ) == []


def test_fetch_naver_slots_uses_same_weekday_template_when_date_is_closed(monkeypatch):
    """A disabled Naver calendar day has no hourly records of its own.

    The picker must still offer its timetable by copying the most recent schedule
    for the same weekday.  These are selectable template times, not claims that
    the still-closed target date is already bookable.
    """
    import engines.naver_api as naver_api

    kst = timezone(timedelta(hours=9))

    def payload(time_text, slot_id):
        return {
            "id": slot_id,
            "slotId": slot_id,
            "scheduleId": "schedule",
            "detailScheduleId": None,
            "unitStartTime": time_text,
            "stock": 1,
            "bookingCount": 1,
            "occupiedBookingCount": 0,
            "isBusinessDay": True,
            "isSaleDay": True,
            "isUnitSaleDay": True,
            "isUnitBusinessDay": True,
            "isHoliday": False,
            "minBookingCount": 1,
            "maxBookingCount": 1,
            "saleStartDateTime": None,
            "saleEndDateTime": None,
        }

    class FakeApi:
        def __init__(self, *_args, **_kwargs):
            self.calls = []

        def fetch_slots(self, date_from, date_to=None):
            self.calls.append((date_from, date_to))
            if date_from == "2026-08-09" and date_to is None:
                return []
            return [
                naver_api.NaverSlot.from_payload(payload("2026-08-01 09:40:00", "sat")),
                naver_api.NaverSlot.from_payload(payload("2026-08-02 13:45:00", "sun-1")),
                naver_api.NaverSlot.from_payload(payload("2026-08-02 15:00:00", "sun-2")),
            ]

        def fetch_item_meta(self):
            return naver_api.NaverItemMeta(
                name="버디",
                server_time=datetime(2026, 8, 2, 18, 0, tzinfo=kst),
                is_closed_booking=False,
                is_closed_for_user=False,
                open_at=datetime(2026, 8, 1, 22, 0, tzinfo=kst),
                is_opened=True,
                uses_open_schedule=True,
                is_paused=False,
                custom_form=[],
            )

        def close(self):
            pass

    monkeypatch.setattr(naver_api, "NaverBookingApi", FakeApi)

    slots = fetch_naver_slots(
        "https://booking.naver.com/booking/12/bizes/1325520/items/6446475",
        "2026-08-09",
        5.0,
    )

    assert [slot.time for slot in slots] == ["13:45", "15:00"]
    assert all(slot.available is False for slot in slots)
    assert all(slot.estimated is True for slot in slots)
    assert all(slot.source_date == "2026-08-02" for slot in slots)


def test_fetch_any_time_slots_routing(monkeypatch):
    import engines.time_slot_fetchers as tsf
    
    # 각 내부 호출용 fetcher 모킹
    mock_zero = MagicMock(return_value=["zeroworld"])
    mock_key = MagicMock(return_value=["keyescape"])
    mock_jigu = MagicMock(return_value=["jigubyeol"])
    mock_doom = MagicMock(return_value=["doomescape"])
    mock_nav = MagicMock(return_value=["naver"])
    
    monkeypatch.setattr(tsf, "fetch_zeroworld_slots", mock_zero)
    monkeypatch.setattr(tsf, "fetch_keyescape_slots", mock_key)
    monkeypatch.setattr(tsf, "fetch_jigubyeol_slots", mock_jigu)
    monkeypatch.setattr(tsf, "fetch_doomescape_slots", mock_doom)
    monkeypatch.setattr(tsf, "fetch_naver_slots", mock_nav)
    
    # 1. Zeroworld 라우팅 테스트
    config = {"engine_id": "zeroworld_shin", "base_url": "https://zero.com"}
    res = fetch_any_time_slots(config, "1", "2", "2026-07-28")
    assert res == ["zeroworld"]
    
    # 2. Keyescape 라우팅 테스트
    config = {"engine_id": "keyescape", "base_url": "https://key.com"}
    res = fetch_any_time_slots(config, "1", "2", "2026-07-28")
    assert res == ["keyescape"]
    
    # 3. Jigubyeol 라우팅 테스트
    config = {"engine_id": "jigubyeol", "base_url": "https://jigu.com"}
    res = fetch_any_time_slots(config, "1", "2", "2026-07-28")
    assert res == ["jigubyeol"]
    
    # 4. Doomescape 라우팅 테스트
    config = {"engine_id": "doomescape", "base_url": "https://doom.com"}
    res = fetch_any_time_slots(config, "1", "2", "2026-07-28")
    assert res == ["doomescape"]
    
    # 5. Naver 라우팅 테스트
    config = {"engine_id": "naver", "url": "https://booking.naver.com/..."}
    res = fetch_any_time_slots(config, "1", "2", "2026-07-28")
    assert res == ["naver"]


def test_fetch_doomescape_slots(monkeypatch):
    import requests
    # 둠이스케이프 HTML 모킹 (sinbiweb 스타일의 tm_box 구성)
    mock_html = """
    <div class="tm_box">
        <p class="name">Rendering</p>
        <a href="?go=rev.make&s_zizum=3&rev_days=2026-07-28&theme_time_num=101">
            <span class="num">12:30</span>
            <span class="txt">예약가능</span>
        </a>
        <a href="?go=rev.make&s_zizum=3&rev_days=2026-07-28&theme_time_num=102">
            <span class="num">14:00</span>
            <span class="txt">예약마감</span>
        </a>
    </div>
    """
    monkeypatch.setattr(requests, "get", lambda *args, **kwargs: MockResponse(200, mock_html, is_json=False))
    
    slots = fetch_doomescape_slots(
        base_url="https://doomescape.com",
        branch_id="3",
        theme_id="8", # Rendering
        date_str="2026-07-28",
        timeout=5.0
    )
    
    assert len(slots) == 2
    assert slots[0].time == "12:30"
    assert slots[0].slot_id == "101"
    assert slots[0].available is True
    assert slots[1].time == "14:00"
    assert slots[1].slot_id == "102"
    assert slots[1].available is False
