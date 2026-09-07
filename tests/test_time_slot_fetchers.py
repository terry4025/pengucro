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
from pengucro.storage import load_json

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


def test_fetch_keyescape_slots(monkeypatch, tmp_path):
    import requests
    monkeypatch.setenv("PENGUCRO_DATA_DIR", str(tmp_path))
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
        base_url="https://www.keyescape.com",
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


def test_fetch_keyescape_slots_uses_same_weekday_template_for_unopened_date(
    monkeypatch, tmp_path
):
    """A Saturday target must use the previous Saturday, not the nearer Friday."""
    import requests
    monkeypatch.setenv("PENGUCRO_DATA_DIR", str(tmp_path))

    calls = []

    def fake_post(_url, data, **_kwargs):
        calls.append(dict(data))
        if data["t"] == "get_theme_date":
            return MockResponse(200, {
                "status": True,
                "calendarData": {"today": "2026-08-09"},
            }, is_json=True)
        if data["t"] == "get_theme_info_list":
            return MockResponse(200, {
                "status": True,
                "data": [{
                    "info_num": "43", "theme_num": "65", "doing": 7,
                }],
            }, is_json=True)
        if data["date"] == "2026-08-15":
            return MockResponse(200, {
                "status": False, "msg": "예약 가능 한 날짜가 아닙니다.",
            }, is_json=True)
        if data["date"] == "2026-08-08":
            return MockResponse(200, {
                "status": True,
                "data": [
                    {
                        "num": "2219", "hh": "9", "mm": "50",
                        "enable": "Y", "gubun": "C",
                    },
                    {
                        "num": "2292", "hh": "10", "mm": "50",
                        "enable": "N", "gubun": "C",
                    },
                ],
            }, is_json=True)
        pytest.fail(f"unexpected date probe: {data}")

    monkeypatch.setattr(requests, "post", fake_post)

    slots = fetch_keyescape_slots(
        base_url="https://www.keyescape.com",
        branch_id="22",
        theme_id="43",
        date_str="2026-08-15",
        timeout=5.0,
    )

    assert [slot.time for slot in slots] == ["09:50", "10:50"]
    assert all(slot.estimated for slot in slots)
    assert all(slot.source_date == "2026-08-08" for slot in slots)
    assert all(slot.estimate_basis == "same_weekday" for slot in slots)
    assert all(slot.slot_id == "" for slot in slots)
    assert all(slot.available is False for slot in slots)
    probed_dates = [call.get("date") for call in calls if call["t"] == "get_theme_time"]
    assert probed_dates == ["2026-08-15", "2026-08-08"]
    cache = load_json("keyescape_slot_templates.json", {})
    saved = cache["entries"]["https://www.keyescape.com|22|65"][-1]
    assert saved["date"] == "2026-08-08"
    assert saved["group"] == "weekday_5"
    assert saved["gubun"] == "C"
    assert saved["slots"] == {"09:50": "2219", "10:50": "2292"}
    from engines.keyescape_engine import KeyescapeEngine

    engine = KeyescapeEngine(lambda *_args: None)
    assert engine._trusted_slot_from_cache(
        "2026-08-15", "09:50", "22", "65"
    ) == ("2219", ("2026-08-08",))


def test_fetch_jigubyeol_slots(monkeypatch, tmp_path):
    import requests
    monkeypatch.setenv("PENGUCRO_DATA_DIR", str(tmp_path))
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


def test_play33_unopened_redirect_uses_latest_published_day_type(monkeypatch, tmp_path):
    import requests
    import engines.time_slot_fetchers as module

    monkeypatch.setenv("PENGUCRO_DATA_DIR", str(tmp_path))
    monkeypatch.setattr(
        module, "_jigubyeol_reference_day", lambda: datetime(2026, 8, 30).date()
    )
    calls = []

    def fake_get(_url, *, params, allow_redirects, **_kwargs):
        requested = params["date"]
        calls.append((requested, allow_redirects))
        if requested == "2026-09-03":
            response = MockResponse(302, "")
            response.url = "https://play33.kr/reservation"
            return response
        if requested == "2026-09-02":
            response = MockResponse(
                200,
                """
                <button><span>11:00</span> 예약가능</button>
                <button><span>12:30</span> 예약가능</button>
                """,
            )
            response.url = "https://play33.kr/reservation?date=2026-09-02"
            return response
        raise AssertionError(f"unexpected probe: {requested}")

    monkeypatch.setattr(requests, "get", fake_get)

    slots = fetch_jigubyeol_slots(
        "https://play33.kr", "1", "18", "2026-09-03", 5.0
    )

    assert [slot.time for slot in slots] == ["11:00", "12:30"]
    assert all(slot.estimated for slot in slots)
    assert all(slot.available is False for slot in slots)
    assert all(slot.slot_id == "" for slot in slots)
    assert all(slot.source_date == "2026-09-02" for slot in slots)
    assert all(slot.estimate_basis == "same_day_type" for slot in slots)
    assert calls == [("2026-09-03", False), ("2026-09-02", False)]


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
    # The API uses the remaining total read budget after local preparation.
    assert captured["timeout"] == pytest.approx(5.0, abs=0.05)
    assert captured["timeout"] <= 5.0
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
    assert all(slot.estimate_basis == "same_weekday" for slot in slots)


@pytest.mark.parametrize("legacy_config", [
    {"engine_id": "zeroworld_laravel"},
    {"engine_id": "zeroworld_gu"},
    {"style": "zeroworld"},
])
def test_retired_zeroworld_never_uses_current_site_lookup(monkeypatch, legacy_config):
    import engines.time_slot_fetchers as tsf

    lookup = MagicMock(side_effect=AssertionError("retired sites must not make a lookup"))
    monkeypatch.setattr(tsf, "fetch_zeroworld_slots", lookup)
    with pytest.raises(ValueError, match="지원이 종료"):
        fetch_any_time_slots({**legacy_config, "url": "https://old.example"}, "1", "2", "2026-09-20")
    lookup.assert_not_called()


def test_fetch_any_time_slots_routing(monkeypatch):
    import engines.time_slot_fetchers as tsf
    
    # 각 내부 호출용 fetcher 모킹
    mock_zero = MagicMock(return_value=["zeroworld"])
    mock_key = MagicMock(return_value=["keyescape"])
    mock_jigu = MagicMock(return_value=["jigubyeol"])
    mock_doom = MagicMock(return_value=["doomescape"])
    mock_nav = MagicMock(return_value=["naver"])
    mock_dpsnnn = MagicMock(return_value=["dpsnnn"])
    
    monkeypatch.setattr(tsf, "fetch_zeroworld_slots", mock_zero)
    monkeypatch.setattr(tsf, "fetch_keyescape_slots", mock_key)
    monkeypatch.setattr(tsf, "fetch_jigubyeol_slots", mock_jigu)
    monkeypatch.setattr(tsf, "fetch_doomescape_slots", mock_doom)
    monkeypatch.setattr(tsf, "fetch_naver_slots", mock_nav)
    import engines.dpsnnn_engine as dpsnnn_module
    monkeypatch.setattr(dpsnnn_module, "fetch_dpsnnn_slots", mock_dpsnnn)
    
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

    # 6. 단편선 전용 엔진 라우팅 테스트
    config = {"engine_id": "dpsnnn", "base_url": "https://www.dpsnnn.com"}
    res = fetch_any_time_slots(config, "seongsu", "문장", "2026-07-28")
    assert res == ["dpsnnn"]


def test_fetch_doomescape_slots(monkeypatch, tmp_path):
    import requests
    monkeypatch.setenv("PENGUCRO_DATA_DIR", str(tmp_path))
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


def test_fetch_doomescape_slots_reuses_cached_same_weekday(monkeypatch, tmp_path):
    import requests

    monkeypatch.setenv("PENGUCRO_DATA_DIR", str(tmp_path))
    source_html = """
    <input name="rev_days" value="2026-08-08">
    <div class="tm_box">
        <p class="name">Rendering</p>
        <a href="?theme_time_num=801"><span class="num">12:30</span><span class="txt">예약가능</span></a>
        <a href="?theme_time_num=802"><span class="num">14:00</span><span class="txt">예약마감</span></a>
    </div>
    """
    unopened_html = '<input name="rev_days" value="2026-08-15"><div class="calendar">예약</div>'

    def fake_get(_url, *, params, **_kwargs):
        html = source_html if params["rev_days"] == "2026-08-08" else unopened_html
        return MockResponse(200, html)

    monkeypatch.setattr(requests, "get", fake_get)
    assert fetch_doomescape_slots(
        "https://doomescape.com", "3", "8", "2026-08-08", 5.0
    )

    slots = fetch_doomescape_slots(
        "https://doomescape.com", "3", "8", "2026-08-15", 5.0
    )

    assert [slot.time for slot in slots] == ["12:30", "14:00"]
    assert all(slot.estimated for slot in slots)
    assert all(slot.source_date == "2026-08-08" for slot in slots)
    assert all(slot.estimate_basis == "same_weekday" for slot in slots)


def test_doomescape_cache_prefers_same_weekday_over_nearer_weekday(monkeypatch, tmp_path):
    import requests
    import engines.time_slot_fetchers as module

    monkeypatch.setenv("PENGUCRO_DATA_DIR", str(tmp_path))
    monkeypatch.setattr(module, "_doomescape_reference_day", lambda: datetime(2026, 8, 8).date())

    def html_for(day, time_value):
        return f"""
        <input name="rev_days" value="{day}">
        <div class="tm_box"><p class="name">Rendering</p>
          <a href="?theme_time_num=1"><span class="num">{time_value}</span><span class="txt">예약가능</span></a>
        </div>
        """

    pages = {
        "2026-08-08": html_for("2026-08-08", "12:30"),  # Saturday
        "2026-08-14": html_for("2026-08-14", "11:00"),  # Friday, but nearer
        "2026-08-15": '<input name="rev_days" value="2026-08-15"><div>예약</div>',
    }
    def fake_get(_url, *, params, **_kwargs):
        requested = params["rev_days"]
        return MockResponse(
            200,
            pages.get(
                requested,
                f'<input name="rev_days" value="{requested}"><div>예약</div>',
            ),
        )

    monkeypatch.setattr(requests, "get", fake_get)
    fetch_doomescape_slots("https://doomescape.com", "3", "8", "2026-08-08", 5.0)
    fetch_doomescape_slots("https://doomescape.com", "3", "8", "2026-08-14", 5.0)

    slots = fetch_doomescape_slots(
        "https://doomescape.com", "3", "8", "2026-08-15", 5.0
    )

    assert [slot.time for slot in slots] == ["12:30"]
    assert slots[0].source_date == "2026-08-08"
    assert slots[0].estimate_basis == "same_weekday"


def test_fetch_doomescape_slots_rejects_server_error_page(monkeypatch, tmp_path):
    import requests

    monkeypatch.setenv("PENGUCRO_DATA_DIR", str(tmp_path))
    monkeypatch.setattr(
        requests,
        "get",
        lambda *_args, **_kwargs: MockResponse(200, "<html><h1>Service Unavailable</h1></html>"),
    )

    with pytest.raises(ValueError, match="서버 장애.*저장된 시간표가 없습니다"):
        fetch_doomescape_slots(
            "https://doomescape.com", "3", "8", "2026-08-15", 5.0
        )


def test_fetch_doomescape_slots_uses_cache_during_traffic_over(monkeypatch, tmp_path):
    import requests

    monkeypatch.setenv("PENGUCRO_DATA_DIR", str(tmp_path))
    source_html = """
    <input name="rev_days" value="2026-08-08">
    <div class="tm_box"><p class="name">허수아비</p>
      <a href="?theme_time_num=801"><span class="num">11:00</span><span class="txt">예약가능</span></a>
    </div>
    """
    traffic_html = "<html><title>:: 일일전송량 초과 안내 ::</title><body>트래픽 초과</body></html>"

    def fake_get(_url, *, params, **_kwargs):
        response = MockResponse(
            200,
            source_html if params["rev_days"] == "2026-08-08" else traffic_html,
        )
        response.url = (
            "https://doomescape.com/layout/res/home.php"
            if params["rev_days"] == "2026-08-08"
            else "http://www.nesolution.com/msg/traffic_over.aspx?gno=180014"
        )
        return response

    monkeypatch.setattr(requests, "get", fake_get)
    assert fetch_doomescape_slots(
        "https://doomescape.com", "4", "34", "2026-08-08", 5.0
    )

    slots = fetch_doomescape_slots(
        "https://doomescape.com", "4", "34", "2026-08-15", 5.0
    )

    assert [slot.time for slot in slots] == ["11:00"]
    assert slots[0].estimated is True
    assert slots[0].estimate_reason == "traffic_over"
    assert slots[0].source_date == "2026-08-08"


def test_fetch_doomescape_slots_caches_every_theme_from_one_page(monkeypatch, tmp_path):
    import requests

    monkeypatch.setenv("PENGUCRO_DATA_DIR", str(tmp_path))
    source_html = """
    <input name="rev_days" value="2026-08-08">
    <div class="tm_box"><p class="name">허수아비</p>
      <a href="?theme_time_num=801"><span class="num">11:00</span><span class="txt">예약가능</span></a>
    </div>
    <div class="tm_box"><p class="name">옵스큐라</p>
      <a href="?theme_time_num=901"><span class="num">11:20</span><span class="txt">예약가능</span></a>
    </div>
    """
    unopened_html = '<input name="rev_days" value="2026-08-15"><div>예약</div>'

    def fake_get(_url, *, params, **_kwargs):
        return MockResponse(
            200,
            source_html if params["rev_days"] == "2026-08-08" else unopened_html,
        )

    monkeypatch.setattr(requests, "get", fake_get)
    assert fetch_doomescape_slots(
        "https://doomescape.com", "4", "34", "2026-08-08", 5.0
    )

    obscura = fetch_doomescape_slots(
        "https://doomescape.com", "4", "35", "2026-08-15", 5.0
    )

    assert [slot.time for slot in obscura] == ["11:20"]
    assert obscura[0].estimated is True
    assert obscura[0].source_date == "2026-08-08"


def test_fetch_doomescape_slots_shares_branch_date_page_between_themes(monkeypatch, tmp_path):
    import requests

    monkeypatch.setenv("PENGUCRO_DATA_DIR", str(tmp_path))
    page_html = """
    <input name="rev_days" value="2026-08-15">
    <div class="tm_box"><p class="name">허수아비</p>
      <a href="?theme_time_num=801"><span class="num">11:00</span><span class="txt">예약가능</span></a>
    </div>
    <div class="tm_box"><p class="name">옵스큐라</p>
      <a href="?theme_time_num=901"><span class="num">11:20</span><span class="txt">예약가능</span></a>
    </div>
    """
    calls = []

    def fake_get(_url, *, params, **_kwargs):
        calls.append(dict(params))
        return MockResponse(200, page_html)

    monkeypatch.setattr(requests, "get", fake_get)
    scarecrow = fetch_doomescape_slots(
        "https://doomescape.com", "4", "34", "2026-08-15", 5.0
    )
    obscura = fetch_doomescape_slots(
        "https://doomescape.com", "4", "35", "2026-08-15", 5.0
    )

    assert [slot.time for slot in scarecrow] == ["11:00"]
    assert [slot.time for slot in obscura] == ["11:20"]
    assert len(calls) == 1


def test_doomescape_unopened_date_seeds_all_themes_on_a_cold_install(monkeypatch, tmp_path):
    import requests
    import engines.time_slot_fetchers as module

    monkeypatch.setenv("PENGUCRO_DATA_DIR", str(tmp_path))
    monkeypatch.setattr(module, "_doomescape_reference_day", lambda: datetime(2026, 8, 13).date())

    weekday_html = """
    <input name="rev_days" value="{date}">
    <div class="tm_box"><p class="name">허수아비</p>
      <a href="?theme_time_num=801"><span class="num">11:00</span><span class="txt">예약가능</span></a>
    </div>
    <div class="tm_box"><p class="name">옵스큐라</p>
      <a href="?theme_time_num=901"><span class="num">11:20</span><span class="txt">예약가능</span></a>
    </div>
    """
    holiday_html = """
    <input name="rev_days" value="2026-08-17">
    <div class="tm_box"><p class="name">허수아비</p>
      <a href="?theme_time_num=802"><span class="num">12:20</span><span class="txt">예약가능</span></a>
    </div>
    <div class="tm_box"><p class="name">옵스큐라</p>
      <a href="?theme_time_num=902"><span class="num">12:40</span><span class="txt">예약가능</span></a>
    </div>
    """
    unopened_html = '<input name="rev_days" value="{date}"><div class="tm_box"></div>'
    calls = []

    def fake_get(_url, *, params, **_kwargs):
        requested = params["rev_days"]
        calls.append(requested)
        if requested in {"2026-08-13", "2026-08-14"}:
            return MockResponse(200, weekday_html.format(date=requested))
        if requested == "2026-08-17":
            return MockResponse(200, holiday_html)
        return MockResponse(200, unopened_html.format(date=requested))

    monkeypatch.setattr(requests, "get", fake_get)

    scarecrow = fetch_doomescape_slots(
        "https://doomescape.com", "4", "34", "2026-08-18", 5.0
    )
    obscura = fetch_doomescape_slots(
        "https://doomescape.com", "4", "35", "2026-08-18", 5.0
    )

    assert [slot.time for slot in scarecrow] == ["12:20"]
    assert [slot.time for slot in obscura] == ["12:40"]
    assert all(slot.estimated for slot in scarecrow + obscura)
    assert all(slot.source_date == "2026-08-17" for slot in scarecrow + obscura)
    assert "2026-08-13" in calls and "2026-08-14" in calls
    assert calls.count("2026-08-18") == 1


def test_doomescape_estimate_never_crosses_weekday_and_weekend(monkeypatch, tmp_path):
    import requests

    monkeypatch.setenv("PENGUCRO_DATA_DIR", str(tmp_path))
    weekend_html = """
    <input name="rev_days" value="2026-08-15">
    <div class="tm_box"><p class="name">허수아비</p>
      <a href="?theme_time_num=801"><span class="num">11:00</span><span class="txt">예약가능</span></a>
    </div>
    """

    monkeypatch.setattr(requests, "get", lambda *_args, **_kwargs: MockResponse(200, weekend_html))
    assert fetch_doomescape_slots(
        "https://doomescape.com", "4", "34", "2026-08-15", 5.0
    )

    from engines.time_slot_fetchers import _estimate_doomescape_timetable

    assert _estimate_doomescape_timetable(
        "https://doomescape.com", "4", "34", "2026-08-18"
    ) == []
