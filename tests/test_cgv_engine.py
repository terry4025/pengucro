from engines.cgv_client import CgvSeat, CgvSeatGroup
from engines.cgv_engine import CgvEngine


def _seat_payload(*labels: str):
    seats = []
    for index, label in enumerate(labels, start=1):
        row = "".join(character for character in label if not character.isdigit())
        number = "".join(character for character in label if character.isdigit())
        seats.append(
            {
                "seatLocNo": f"loc-{index}",
                "seatRowNm": row,
                "seatNo": number,
                "seatStusCd": "00",
                "seatSaleYn": "Y",
                "sbordNo": "001",
                "seatAreaNo": "001",
                "szoneNo": "001",
            }
        )
    return {"data": {"items": [{"seats": seats}]}}


def test_choose_available_group_uses_first_complete_priority_group():
    elements = [
        {"id": "loc-a22", "label": "A22", "unavailable": False},
        {"id": "loc-a23", "label": "A23", "unavailable": True},
        {"id": "loc-b10", "label": "B10", "unavailable": False},
        {"id": "loc-b11", "label": "B11", "unavailable": False},
    ]
    groups = (CgvSeatGroup(("A22", "A23")), CgvSeatGroup(("B10", "B11")))

    selected = CgvEngine.choose_available_group(elements, groups)

    assert selected is not None
    group, ids = selected
    assert group.seats == ("B10", "B11")
    assert ids == {"B10": "loc-b10", "B11": "loc-b11"}


def test_schedule_url_contains_official_cgv_query_fields():
    url = CgvEngine._schedule_url("0013", "2026-08-18")

    assert url.startswith("https://cgv.co.kr/api/v1/booking/searchMovScnInfo?")
    assert "siteNo=0013" in url
    assert "scnYmd=20260818" in url
    assert "rtctlScopCd=08" in url


def test_query_payload_keeps_fields_required_by_one_page_booking():
    payload = CgvEngine._query_payload(
        {
            "siteNo": "0013",
            "scnYmd": "20260818",
            "scnsNo": "01",
            "scnSseq": "5",
            "scnsrtTm": "1000",
            "movNm": "오디세이",
        }
    )

    assert payload["coCd"] == "A420"
    assert payload["siteNo"] == "0013"
    assert payload["scnSseq"] == "5"
    assert payload["salsTznCd"] == "26"
    assert payload["soldierJoinStus"] == "N"


def test_seat_url_contains_official_screening_identity():
    url = CgvEngine._seat_url(
        {
            "siteNo": "0013", "scnYmd": "20260818", "scnsNo": "018",
            "scnSseq": "2",
        },
        "customer-1",
    )

    assert url.startswith("https://cgv.co.kr/api/v1/booking/searchIfSeatData?")
    assert "scnsNo=018" in url
    assert "scnSseq=2" in url
    assert "custNo=customer-1" in url


def test_choose_available_api_group_preserves_priority_order():
    seats = (
        CgvSeat("a22", "A22", "A", 22, True),
        CgvSeat("a23", "A23", "A", 23, False),
        CgvSeat("b10", "B10", "B", 10, True),
        CgvSeat("b11", "B11", "B", 11, True),
    )
    groups = (CgvSeatGroup(("A22", "A23")), CgvSeatGroup(("B10", "B11")))

    selected = CgvEngine.choose_available_api_group(seats, groups)

    assert selected is not None
    group, chosen = selected
    assert group.seats == ("B10", "B11")
    assert [seat.seat_id for seat in chosen] == ["b10", "b11"]


def test_engine_never_combines_separated_available_seats():
    seats = (
        CgvSeat("f12", "F12", "F", 12, True),
        CgvSeat("f24", "F24", "F", 24, True),
        CgvSeat("g12", "G12", "G", 12, True),
        CgvSeat("g13", "G13", "G", 13, True),
    )
    groups = (
        CgvSeatGroup(("F12", "F24")),
        CgvSeatGroup(("G12", "G13")),
    )

    selected = CgvEngine.choose_available_api_group(seats, groups)

    assert selected is not None
    group, chosen = selected
    assert group.seats == ("G12", "G13")
    assert [seat.label for seat in chosen] == ["G12", "G13"]


def test_engine_never_selects_consecutive_numbers_across_an_official_aisle():
    seats = (
        CgvSeat("h22", "H22", "H", 22, True, right_passage=True),
        CgvSeat("h23", "H23", "H", 23, True),
        CgvSeat("h24", "H24", "H", 24, True),
        CgvSeat("h25", "H25", "H", 25, True),
    )
    groups = (
        CgvSeatGroup(("H22", "H23")),
        CgvSeatGroup(("H24", "H25")),
    )

    selected = CgvEngine.choose_available_api_group(seats, groups)

    assert selected is not None
    group, chosen = selected
    assert group.seats == ("H24", "H25")
    assert [seat.label for seat in chosen] == ["H24", "H25"]


def test_fast_monitor_uses_staggered_persistent_requests_with_safe_inflight_cap():
    calls = []

    class Page:
        def evaluate(self, script, argument):
            calls.append((script, argument))
            return True

    engine = CgvEngine(lambda *_args: None)
    engine.scan_concurrency = 4

    assert engine._start_fast_seat_monitor(
        Page(),
        "https://cgv.co.kr/api/v1/booking/searchIfSeatData",
        (CgvSeatGroup(("H22", "H23")),),
        4,
    ) is True

    script, argument = calls[0]
    assert "setInterval(launch, intervalMs)" in script
    assert "state.inflight >= concurrency" in script
    assert "seatStusCd" in script
    assert argument["intervalMs"] == 120
    assert argument["concurrency"] == 4
    assert argument["groups"] == [["H22", "H23"]]
    assert argument["directHold"] is None
    assert argument["requestHeaders"] == {}
    assert argument["initialPayload"] is None
    assert "headers.set('Authorization', `Bearer ${token}`)" in script
    assert "headers.set('Accept-Language', 'ko-KR')" in script
    assert "queuedPayload" in script
    assert "buildPricePayload" in script
    assert "buildHoldPayload" in script
    assert "state.conflicts += 1" in script
    assert "resume()" in script


def test_initial_modal_seat_response_is_reused_with_only_safe_auth_headers():
    payload = _seat_payload("H22", "H23")
    url = (
        "https://cgv.co.kr/api/v1/booking/searchIfSeatData?"
        "siteNo=0013&scnYmd=20260818&scnsNo=018&scnSseq=2"
    )

    class Request:
        def all_headers(self):
            return {
                "Authorization": "Bearer captured-token",
                "Accept-Language": "ko-KR",
                "Cookie": "must-not-be-copied",
                "User-Agent": "must-not-be-overridden",
            }

    class Response:
        status = 200
        request = Request()

        def __init__(self):
            self.url = url

        def json(self):
            return payload

    class Page:
        def __init__(self):
            self.handler = None
            self.removed = None

        def on(self, event, handler):
            assert event == "response"
            self.handler = handler

        def remove_listener(self, event, handler):
            self.removed = (event, handler)

    engine = CgvEngine(lambda *_args: None)
    page = Page()
    handler = engine._begin_initial_seat_response_capture(page)
    page.handler(Response())
    engine._end_initial_seat_response_capture(page, handler)
    captured = engine._consume_initial_seat_response(
        {"siteNo": "0013", "scnYmd": "20260818", "scnsNo": "018", "scnSseq": "2"}
    )

    assert captured["data"] == payload
    assert captured["url"] == url
    assert captured["requestHeaders"] == {
        "authorization": "Bearer captured-token",
        "accept-language": "ko-KR",
    }
    assert page.removed == ("response", handler)


def test_watch_seeds_monitor_from_initial_response_before_duplicate_get():
    payload = _seat_payload("H22")
    url = (
        "https://cgv.co.kr/api/v1/booking/searchIfSeatData?"
        "siteNo=0013&scnYmd=20260818&scnsNo=018&scnSseq=2"
    )
    starts = []
    engine = CgvEngine(lambda *_args: None)
    engine._initial_seat_response = {
        "url": url,
        "status": 200,
        "data": payload,
        "requestHeaders": {"authorization": "Bearer captured-token"},
    }
    engine._browser_auth_data = lambda _page: {"custNo": "member"}

    def start(_page, seat_url, _groups, _concurrency, **kwargs):
        starts.append((seat_url, kwargs))
        return True

    engine._start_fast_seat_monitor = start
    engine._read_fast_seat_monitor = lambda _page: {
        "running": False,
        "completed": 1,
        "lastStatus": 0,
        "terminalError": "test-stop",
        "hit": None,
    }
    engine._stop_fast_seat_monitor = lambda _page: None

    held, fallback = engine._watch_and_hold_api(
        object(),
        {"siteNo": "0013", "scnYmd": "20260818", "scnsNo": "018", "scnSseq": "2"},
        (CgvSeatGroup(("H22",)),),
        1,
        False,
        {},
    )

    assert (held, fallback) == (False, True)
    assert starts[0][0] == url
    assert starts[0][1]["initial_payload"] == payload
    assert starts[0][1]["request_headers"] == {
        "authorization": "Bearer captured-token"
    }
    assert engine._initial_seat_response is None


def test_direct_hold_config_prebuilds_schedule_and_sanitizes_customer_data():
    config = CgvEngine._direct_hold_config(
        {
            "coCd": "A420", "siteNo": "0013", "scnsNo": "018",
            "scnYmd": "20260818", "scnSseq": "2", "movNo": "movie-1",
        },
        2,
        {"custNo": "member", "cusgdCd": "02", "bymd": "1990-01-02", "mbltNo": "010-1234-5678"},
        {},
    )

    assert config["people"] == 2
    assert config["schedule"]["siteNo"] == "0013"
    assert config["schedule"]["rtctlScopCd"] == "08"
    assert config["auth"] == {
        "custNo": "member",
        "cusgdCd": "02",
        "bymd": "19900102",
        "mbltNo": "01012345678",
        "nmbrCrtfNo": "",
    }
    assert config["priceUrl"].endswith("/searchMovAtktSeatPrcList")
    assert config["holdUrl"].endswith("/seatTemp/seatTempPrmp")


def test_browser_internal_hold_finishes_before_visible_ui_sync():
    actions = []
    payload = _seat_payload("H22", "H23")
    starts = []

    class Page:
        pass

    engine = CgvEngine(lambda *_args: None)
    engine.scan_concurrency = 3
    engine._browser_auth_data = lambda _page: {"custNo": "member", "cusgdCd": "01"}

    def start(*_args, **kwargs):
        starts.append(kwargs["direct_hold"])
        actions.append("monitor-with-direct-hold")
        return True

    engine._start_fast_seat_monitor = start
    engine._read_fast_seat_monitor = lambda _page: {
        "running": False,
        "completed": 1,
        "hit": {
            "data": payload,
            "elapsedMs": 180,
            "transaction": {
                "priceResponse": {"statusCode": 0},
                "holdResponse": {"statusCode": 0, "data": {"resultCode": "0", "movAtktNo": "hold-1"}},
                "holdPayload": {"custNo": "member", "seatPrmpDataList": []},
                "elapsedMs": 95,
            },
        },
    }
    engine._stop_fast_seat_monitor = lambda _page: None
    engine._post_json = lambda *_args: (_ for _ in ()).throw(AssertionError("Python POST must not run"))
    engine._select_api_seats_in_ui = lambda *_args: actions.append("ui") or True
    engine._install_cached_hold_responses = lambda *_args: actions.append("cache")
    engine._submit_seat_selection = lambda *_args: actions.append("submit") or True
    engine._restore_fetch = lambda *_args: None

    held, fallback = engine._watch_and_hold_api(
        Page(),
        {"siteNo": "0013", "scnYmd": "20260818", "scnsNo": "018", "scnSseq": "2"},
        (CgvSeatGroup(("H22", "H23")),),
        2,
        False,
        {},
    )

    assert held is True
    assert fallback is False
    assert starts[0]["auth"]["custNo"] == "member"
    assert actions == ["monitor-with-direct-hold", "ui", "cache", "submit"]


def test_direct_hold_result_never_reloads_browser_page():
    payload = _seat_payload("H22", "H23")

    class Page:
        pass

    page = Page()
    engine = CgvEngine(lambda *_args: None)
    engine._browser_auth_data = lambda _page: {}
    engine._start_fast_seat_monitor = lambda *_args, **_kwargs: True
    engine._read_fast_seat_monitor = lambda _page: {
        "running": False,
        "completed": 1,
        "hit": {
            "data": payload,
            "transaction": {
                "priceResponse": {"statusCode": 0},
                "holdResponse": {"statusCode": 0, "data": {"movAtktNo": "hold-1"}},
                "holdPayload": {},
            },
        },
    }
    engine._stop_fast_seat_monitor = lambda _page: None
    engine._select_api_seats_in_ui = lambda *_args: True
    engine._install_cached_hold_responses = lambda *_args: None
    engine._submit_seat_selection = lambda *_args: False
    engine._cancel_api_hold = lambda *_args: None
    engine._restore_fetch = lambda *_args: None

    held, fallback = engine._watch_and_hold_api(
        page,
        {"siteNo": "0013", "scnYmd": "20260818", "scnsNo": "018", "scnSseq": "2"},
        (CgvSeatGroup(("H22", "H23")),),
        2,
        False,
        {},
    )

    assert held is False
    assert fallback is True
    assert not hasattr(page, "reload")


def test_cgv_engine_selects_first_matching_preferred_time():
    from engines.cgv_client import select_schedule

    payload = {
        "data": {
            "items": [
                {
                    "siteNo": "0013",
                    "scnYmd": "20260826",
                    "scnsNo": "01",
                    "scnSseq": "1",
                    "scnsrtTm": "1400",
                    "movNm": "오디세이",
                    "expoScnsNm": "IMAX관",
                    "movkndDsplEnm": "IMAX LASER 2D",
                },
                {
                    "siteNo": "0013",
                    "scnYmd": "20260826",
                    "scnsNo": "01",
                    "scnSseq": "2",
                    "scnsrtTm": "1730",
                    "movNm": "오디세이",
                    "expoScnsNm": "IMAX관",
                    "movkndDsplEnm": "IMAX LASER 2D",
                },
            ]
        }
    }

    # User prioritizes 17:30 over 14:00
    chosen = select_schedule(
        payload,
        movie="오디세이",
        auditorium="IMAX관",
        preferred_times=["17:30", "14:00"],
    )
    assert chosen is not None
    assert chosen["scnSseq"] == "2"
    assert chosen["scnsrtTm"] == "1730"
    assert chosen["scnYmd"] == "20260826"


def test_historical_identifiers_never_leak_into_target_booking():
    from engines.cgv_client import select_schedule

    # Historical screening had scnYmd 20260824, scnsNo 099, scnSseq 7
    # Live schedule returned for 20260826 has scnYmd 20260826, scnsNo 002, scnSseq 1
    live_schedule_payload = {
        "data": [
            {
                "siteNo": "0013",
                "scnYmd": "20260826",
                "scnsNo": "002",
                "scnSseq": "1",
                "scnsrtTm": "1400",
                "movNm": "오디세이",
                "expoScnsNm": "IMAX관",
                "movkndDsplEnm": "IMAX LASER 2D",
            }
        ]
    }

    chosen = select_schedule(
        live_schedule_payload,
        movie="오디세이",
        auditorium="IMAX관",
        preferred_times=["14:00"],
    )
    assert chosen is not None
    assert chosen["scnYmd"] == "20260826"
    assert chosen["scnsNo"] == "002"
    assert chosen["scnSseq"] == "1"


def test_preopen_idle_and_schedule_hint_intervals():
    from engines.cgv_engine import CgvEngine, _has_schedule_hint

    assert CgvEngine.PREOPEN_IDLE_INTERVAL == 20.0
    assert CgvEngine.SCHEDULE_HINT_INTERVAL == 2.0
    assert CgvEngine.MIN_POLL_INTERVAL == 0.12
    assert CgvEngine.HEDGE_DELAY_MS == 110

    # Unrelated movie on target date in 2D -> no hint (PREOPEN_IDLE)
    unrelated_2d = {
        "data": [
            {
                "siteNo": "0013",
                "scnYmd": "20260826",
                "scnsNo": "001",
                "scnSseq": "1",
                "scnsrtTm": "1200",
                "movNm": "전혀 다른 영화",
                "expoScnsNm": "2관",
            }
        ]
    }
    assert _has_schedule_hint(unrelated_2d, "오디세이", "IMAX관") is False

    # Unrelated movie on target date in IMAX -> MUST NOT trigger hint (PREOPEN_IDLE)
    unrelated_imax = {
        "data": [
            {
                "siteNo": "0013",
                "scnYmd": "20260826",
                "scnsNo": "001",
                "scnSseq": "1",
                "scnsrtTm": "1200",
                "movNm": "전혀 다른 영화",
                "expoScnsNm": "IMAX관",
            }
        ]
    }
    assert _has_schedule_hint(unrelated_imax, "오디세이", "IMAX관") is False

    # Target movie exists on target date (even in 2D) -> hint (SCHEDULE_HINT)
    movie_2d_hint = {
        "data": [
            {
                "siteNo": "0013",
                "scnYmd": "20260826",
                "scnsNo": "001",
                "scnSseq": "1",
                "scnsrtTm": "1200",
                "movNm": "오디세이",
                "expoScnsNm": "2관",
            }
        ]
    }
    assert _has_schedule_hint(movie_2d_hint, "오디세이", "IMAX관") is True

    # Target movie in IMAX -> hint (SCHEDULE_HINT)
    movie_imax_hint = {
        "data": [
            {
                "siteNo": "0013",
                "scnYmd": "20260826",
                "scnsNo": "001",
                "scnSseq": "1",
                "scnsrtTm": "1200",
                "movNm": "오디세이",
                "expoScnsNm": "IMAX관",
            }
        ]
    }
    assert _has_schedule_hint(movie_imax_hint, "오디세이", "IMAX관") is True


def test_cgv_engine_detects_recoverable_browser_errors():
    from engines.cgv_engine import CgvEngine

    assert CgvEngine._is_recoverable_browser_error(
        RuntimeError("TargetClosedError: Page.evaluate: Target page, context or browser has been closed")
    ) is True
    assert CgvEngine._is_recoverable_browser_error(
        RuntimeError("Target page has been closed")
    ) is True
    assert CgvEngine._is_recoverable_browser_error(
        RuntimeError("CDP disconnected")
    ) is True
    assert CgvEngine._is_recoverable_browser_error(
        ValueError("Something else went wrong")
    ) is False


def test_developer_mode_retains_direct_hold():
    starts = []
    payload = _seat_payload("H22", "H23")

    class Page:
        pass

    engine = CgvEngine(lambda *_args: None)
    engine._browser_auth_data = lambda _page: {"custNo": "cust-dev", "cusgdCd": "01"}

    def fake_start(*_args, **kwargs):
        starts.append(kwargs.get("direct_hold"))
        return True

    engine._start_fast_seat_monitor = fake_start
    engine._read_fast_seat_monitor = lambda _page: {
        "running": False,
        "completed": 1,
        "hit": {
            "data": payload,
            "transaction": {
                "priceResponse": {"statusCode": 0},
                "holdResponse": {"statusCode": 0, "data": {"movAtktNo": "hold-dev-1"}},
                "holdPayload": {},
            },
        },
    }
    engine._stop_fast_seat_monitor = lambda _page: None
    engine._select_api_seats_in_ui = lambda *_args: True
    engine._install_cached_hold_responses = lambda *_args: None
    engine._submit_seat_selection = lambda *_args: False
    engine._cancel_api_hold = lambda *_args: None
    engine._restore_fetch = lambda *_args: None

    held, fallback = engine._watch_and_hold_api(
        Page(),
        {"siteNo": "0013", "scnYmd": "20260818", "scnsNo": "018", "scnSseq": "2"},
        (CgvSeatGroup(("H22", "H23")),),
        2,
        True,  # developer_mode=True
        {"nonmember_birth": "19900101", "nonmember_phone": "01012345678"},
    )

    assert len(starts) == 1
    assert starts[0] is not None
    assert starts[0]["auth"]["custNo"] == "cust-dev"
    assert starts[0]["people"] == 2
    assert "searchMovAtktSeatPrcList" in starts[0]["priceUrl"]
    assert "seatTempPrmp" in starts[0]["holdUrl"]


def test_already_selected_seat_produces_zero_clicks():
    clicks = []

    class MockLocatorItem:
        def __init__(self, seat_id: str, aria_pressed: str = "false", class_name: str = ""):
            self.seat_id = seat_id
            self.aria_pressed = aria_pressed
            self.class_name = class_name

        def is_visible(self):
            return True

        def is_enabled(self):
            return True

        def get_attribute(self, attr: str):
            if attr == "aria-pressed":
                return self.aria_pressed
            if attr == "aria-selected":
                return "false"
            if attr == "class":
                return self.class_name
            return None

        def click(self, **_kwargs):
            clicks.append(self.seat_id)

    class MockLocator:
        def __init__(self, items):
            self.items = items

        def count(self):
            return len(self.items)

        def nth(self, idx):
            return self.items[idx]

    class Page:
        def evaluate(self, *_args, **_kwargs):
            return None

        def locator(self, selector: str):
            if 'data-seatlocno="loc-selected"' in selector:
                return MockLocator([MockLocatorItem("loc-selected", aria_pressed="true", class_name="seat_btn selected")])
            if 'data-seatlocno="loc-unselected"' in selector:
                return MockLocator([MockLocatorItem("loc-unselected", aria_pressed="false", class_name="seat_btn")])
            return MockLocator([])

    page = Page()

    # Selecting already-selected seat produces 0 clicks
    result_selected = CgvEngine._ensure_seat_selected_by_id(page, "loc-selected")
    assert result_selected is True
    assert len(clicks) == 0

    # Selecting unselected seat produces 1 click
    result_unselected = CgvEngine._ensure_seat_selected_by_id(page, "loc-unselected")
    assert result_unselected is True
    assert clicks == ["loc-unselected"]


def test_mixed_selected_unselected_seat_groups_only_clicks_unselected():
    clicked_ids = []

    class MockSeatCandidate:
        def __init__(self, seat_id: str, is_selected: bool):
            self.seat_id = seat_id
            self.is_selected = is_selected

        def is_visible(self):
            return True

        def is_enabled(self):
            return True

        def get_attribute(self, attr: str):
            if attr == "aria-pressed":
                return "true" if self.is_selected else "false"
            if attr == "class":
                return "seat_btn selected" if self.is_selected else "seat_btn"
            return None

        def click(self, **_kwargs):
            clicked_ids.append(self.seat_id)

    class MockLocatorList:
        def __init__(self, candidates):
            self.candidates = candidates

        def count(self):
            return len(self.candidates)

        def nth(self, idx):
            return self.candidates[idx]

    class Page:
        def evaluate(self, script, *args):
            if "querySelectorAll('button[data-seatlocno]')" in script:
                return [
                    {"id": "loc-b10", "label": "B10", "selected": True, "available": False, "unavailable": False},
                    {"id": "loc-b11", "label": "B11", "selected": False, "available": True, "unavailable": False},
                ]
            return None

        def locator(self, selector: str):
            if 'data-seatlocno="loc-b10"' in selector:
                return MockLocatorList([MockSeatCandidate("loc-b10", is_selected=True)])
            if 'data-seatlocno="loc-b11"' in selector:
                return MockLocatorList([MockSeatCandidate("loc-b11", is_selected=False)])
            return MockLocatorList([])

        def wait_for_timeout(self, _ms):
            pass

    engine = CgvEngine(lambda *_args: None)
    engine._submit_seat_selection = lambda _page: True

    page = Page()
    groups = (CgvSeatGroup(("B10", "B11")),)

    held = engine._select_and_hold_seats(page, groups, 2, developer_mode=False)

    assert held is True
    # Only the unselected seat B11 was clicked; already-selected B10 was untouched
    assert clicked_ids == ["loc-b11"]


def test_available_seat_elements_three_state_classification():
    class Page:
        def evaluate(self, script):
            if "button[data-seatlocno]" in script:
                return [
                    {"id": "loc-1", "label": "A01", "available": True, "selected": False, "unavailable": False},
                    {"id": "loc-2", "label": "A02", "available": False, "selected": True, "unavailable": False},
                    {"id": "loc-3", "label": "A03", "available": False, "selected": False, "unavailable": True},
                ]
            return []

    elements = CgvEngine._available_seat_elements(Page())
    assert len(elements) == 3
    assert elements[0] == {"id": "loc-1", "label": "A01", "available": True, "selected": False, "unavailable": False}
    assert elements[1] == {"id": "loc-2", "label": "A02", "available": False, "selected": True, "unavailable": False}
    assert elements[2] == {"id": "loc-3", "label": "A03", "available": False, "selected": False, "unavailable": True}


def test_submit_seat_selection_single_shot_and_waits_for_transition():
    clicks = []
    poll_count = [0]

    class Page:
        def evaluate(self, script, *args):
            if "clean(b.textContent) === '선택완료'" in script:
                clicks.append("submit_btn")
                return True
            if "text.includes('이미 선택된')" in script:
                return False
            if "hasPaySection" in script or "visibleSeats" in script:
                poll_count[0] += 1
                # Transition succeeds on 2nd poll
                return poll_count[0] >= 2
            return False

        def wait_for_timeout(self, _ms):
            pass

    engine = CgvEngine(lambda *_args: None)
    page = Page()

    success = engine._submit_seat_selection(page)

    assert success is True
    # Button was clicked exactly ONCE (single-shot), not multiple blind clicks
    assert clicks == ["submit_btn"]
    assert poll_count[0] == 2


def test_submit_seat_selection_detects_conflict_dialog():
    dismissed = []

    class Page:
        def evaluate(self, script, *args):
            if "clean(b.textContent) === '선택완료'" in script:
                return True
            if "text.includes('이미 선택된')" in script:
                return True
            return False

        def wait_for_timeout(self, _ms):
            pass

    engine = CgvEngine(lambda *_args: None)
    engine._click_visible_by_text = lambda _page, labels: dismissed.append(labels) or True
    page = Page()

    success = engine._submit_seat_selection(page)

    assert success is False
    assert dismissed == [("확인", "닫기", "취소")]


def test_seat_stage_recoverable_error_triggers_session_reconnect():
    reconnect_calls = []

    class DeadPage:
        def reload(self, **_kwargs):
            raise RuntimeError("TargetClosedError: Target page, context or browser has been closed")

    class RecoveredPage:
        def reload(self, **_kwargs):
            pass

    dead_page = DeadPage()
    recovered_page = RecoveredPage()

    engine = CgvEngine(lambda *_args: None)
    schedule = {"siteNo": "0013", "scnYmd": "20260818", "scnsNo": "018", "scnSseq": "2"}

    def fake_reconnect(sched, people):
        reconnect_calls.append((sched, people))
        return recovered_page

    engine._reconnect_seat_session = fake_reconnect

    page_out, ok = engine._reload_or_recover_seat_page(dead_page, schedule=schedule, people=2)

    assert ok is True
    assert page_out is recovered_page
    assert len(reconnect_calls) == 1
    assert reconnect_calls[0] == (schedule, 2)


def test_ensure_seat_selected_handles_all_attributes_and_edge_cases():
    class Page:
        def evaluate(self, script, arg):
            if arg == "loc-aria-selected":
                return True
            if arg == "loc-active-class":
                return True
            if arg == "loc-disabled":
                return False
            if arg == "loc-not-found":
                return None
            return None

        def locator(self, selector: str):
            class MockLoc:
                def count(self):
                    return 0
            return MockLoc()

    page = Page()

    # aria-selected or active class returning True from DOM
    assert CgvEngine._ensure_seat_selected_by_id(page, "loc-aria-selected") is True
    assert CgvEngine._ensure_seat_selected_by_id(page, "loc-active-class") is True
    # Disabled seat returns False
    assert CgvEngine._ensure_seat_selected_by_id(page, "loc-disabled") is False
    # Not found returns False
    assert CgvEngine._ensure_seat_selected_by_id(page, "loc-not-found") is False


def test_reload_or_recover_seat_page_edge_cases():
    engine = CgvEngine(lambda *_args: None)

    # 1. Normal reload succeeds
    class NormalPage:
        def reload(self, **_kwargs):
            pass

    engine._select_visitors = lambda _page, _people: True
    page_out, ok = engine._reload_or_recover_seat_page(NormalPage(), people=1)
    assert ok is True

    # 2. Non-recoverable error returns False
    class CrashPage:
        def reload(self, **_kwargs):
            raise ValueError("Unexpected DOM parsing failure")

    page_out, ok = engine._reload_or_recover_seat_page(CrashPage(), people=1)
    assert ok is False

    # 3. Stop event set returns False immediately
    engine.stop_event.set()
    page_out, ok = engine._reload_or_recover_seat_page(CrashPage(), people=1)
    assert ok is False
    engine.stop_event.clear()


def test_select_visitors_distinguishes_open_modal_with_blank_seat_data(monkeypatch):
    logs = []
    engine = CgvEngine(lambda message, level="info": logs.append((message, level)))
    engine._seat_modal_snapshot = lambda _page: {
        "modalOpen": True,
        "seatCount": 0,
    }
    clock = iter((0.0, 0.0, 13.0))
    monkeypatch.setattr("engines.cgv_engine.time.monotonic", lambda: next(clock))

    class Page:
        def evaluate(self, *_args, **_kwargs):
            raise AssertionError("an open modal must not click hidden visitor controls again")

        def wait_for_timeout(self, _ms):
            pass

    assert engine._select_visitors(Page(), 2) is False
    messages = [message for message, _level in logs]
    assert any("좌석 모달은 열렸지만 좌석 데이터" in message for message in messages)
    assert not any("관람 인원 선택 및 좌석 모달 열기" in message for message in messages)


def test_rate_limited_browser_monitor_survives_one_blank_reload(monkeypatch):
    engine = CgvEngine(lambda *_args: None)
    page = object()
    reloads = []
    element_reads = []
    clock = iter((0.0, 5.0, 5.0))
    monkeypatch.setattr("engines.cgv_engine.time.monotonic", lambda: next(clock))
    engine._is_block_page = lambda _page: False

    def available(_page):
        element_reads.append(1)
        if len(element_reads) == 1:
            return []
        return [
            {
                "id": "loc-h22",
                "label": "H22",
                "available": True,
                "selected": False,
                "unavailable": False,
            }
        ]

    engine._available_seat_elements = available
    engine._reload_or_recover_seat_page = (
        lambda current, **_kwargs: reloads.append(current) or (current, False)
    )
    engine._ensure_seat_selected_by_id = lambda *_args: True
    engine._submit_seat_selection = lambda _page: True

    class Page:
        def wait_for_timeout(self, _ms):
            pass

    page = Page()
    held = engine._select_and_hold_seats(
        page,
        (CgvSeatGroup(("H22",)),),
        1,
        False,
        fallback_reason="rate-limited",
    )

    assert held is True
    assert reloads == [page]
    assert len(element_reads) == 2


def test_submit_seat_selection_timeout_returns_false():
    class Page:
        def evaluate(self, script, *args):
            if "clean(b.textContent) === '선택완료'" in script:
                return True
            return False

        def wait_for_timeout(self, _ms):
            pass

    engine = CgvEngine(lambda *_args: None)
    engine.stop_event.set()  # Stop event set terminates wait loop
    page = Page()

    success = engine._submit_seat_selection(page)
    assert success is False
    engine.stop_event.clear()


def test_submit_seat_selection_not_clicked_returns_false_immediately():
    class Page:
        def evaluate(self, script, *args):
            # 선택완료 button not found
            if "clean(b.textContent) === '선택완료'" in script:
                return False
            # Even if transition check would evaluate true because 0 seat buttons exist
            if "seatButtons.length === 0" in script:
                return True
            return False

        def locator(self, _selector):
            class MockLoc:
                def count(self):
                    return 0
            return MockLoc()

        def get_by_text(self, *_args, **_kwargs):
            class MockLoc:
                def count(self):
                    return 0
            return MockLoc()

    engine = CgvEngine(lambda *_args: None)
    page = Page()

    # Must return False because submit button was never clicked
    success = engine._submit_seat_selection(page)
    assert success is False


def test_select_and_hold_seats_recovers_when_seat_click_fails_midway():
    engine = CgvEngine(lambda *_args: None)
    attempts = [0]
    reload_count = [0]

    class Page:
        def wait_for_timeout(self, _ms):
            pass

    def fake_available_elements(_page):
        attempts[0] += 1
        if attempts[0] == 1:
            # First attempt: Group (A01, A02) available in DOM
            return [
                {"id": "loc-a1", "label": "A01", "available": True, "selected": False, "unavailable": False},
                {"id": "loc-a2", "label": "A02", "available": True, "selected": False, "unavailable": False},
            ]
        else:
            # Second attempt: Group (B01, B02) available in DOM
            return [
                {"id": "loc-b1", "label": "B01", "available": True, "selected": False, "unavailable": False},
                {"id": "loc-b2", "label": "B02", "available": True, "selected": False, "unavailable": False},
            ]

    def fake_ensure_selected(_page, seat_id):
        # A01 succeeds, but A02 fails (collision with another user)
        if seat_id == "loc-a1":
            return True
        if seat_id == "loc-a2":
            return False
        # B01 and B02 succeed
        if seat_id in ("loc-b1", "loc-b2"):
            return True
        return False

    def fake_reload(page, schedule=None, people=1):
        reload_count[0] += 1
        return page, True

    engine._available_seat_elements = fake_available_elements
    engine._ensure_seat_selected_by_id = fake_ensure_selected
    engine._reload_or_recover_seat_page = fake_reload
    engine._submit_seat_selection = lambda _page: True

    groups = (CgvSeatGroup(("A01", "A02")), CgvSeatGroup(("B01", "B02")))
    held = engine._select_and_hold_seats(Page(), groups, 2, False)

    assert held is True
    assert reload_count[0] >= 1
    assert attempts[0] >= 2


def test_rapid_consecutive_conflict_popups_recovery():
    engine = CgvEngine(lambda *_args: None)
    submit_attempts = [0]
    dismissals = []
    reloads = [0]

    class Page:
        def wait_for_timeout(self, _ms):
            pass

    def fake_available_elements(_page):
        return [
            {"id": "loc-1", "label": "C01", "available": True, "selected": False, "unavailable": False},
            {"id": "loc-2", "label": "C02", "available": True, "selected": False, "unavailable": False},
        ]

    def fake_submit(_page):
        submit_attempts[0] += 1
        if submit_attempts[0] < 3:
            # First 2 attempts produce conflict popup
            dismissals.append("dismiss_conflict")
            return False
        # 3rd attempt succeeds
        return True

    def fake_reload(page, schedule=None, people=1):
        reloads[0] += 1
        return page, True

    engine._available_seat_elements = fake_available_elements
    engine._ensure_seat_selected_by_id = lambda _p, _id: True
    engine._submit_seat_selection = fake_submit
    engine._reload_or_recover_seat_page = fake_reload

    groups = (CgvSeatGroup(("C01", "C02")),)
    held = engine._select_and_hold_seats(Page(), groups, 2, False)

    assert held is True
    assert submit_attempts[0] == 3
    assert len(dismissals) == 2
    assert reloads[0] == 2


def test_ensure_seat_selected_handles_on_and_finish_classes():
    class Page:
        def evaluate(self, script, arg):
            if arg == "loc-on":
                return True
            if arg == "loc-finish":
                return False
            if arg == "loc-soldout":
                return False
            return None

    page = Page()
    assert CgvEngine._ensure_seat_selected_by_id(page, "loc-on") is True
    assert CgvEngine._ensure_seat_selected_by_id(page, "loc-finish") is False
    assert CgvEngine._ensure_seat_selected_by_id(page, "loc-soldout") is False


def test_exception_handling_in_helpers():
    class CrashPage:
        def evaluate(self, *_args, **_kwargs):
            raise RuntimeError("CDP disconnected")

    page = CrashPage()
    # Helpers should return safe fallbacks without throwing unhandled exceptions
    assert CgvEngine._available_seat_elements(page) == []
    assert CgvEngine._browser_auth_data(page) == {}
    assert CgvEngine._read_fast_seat_monitor(page) == {}
    engine = CgvEngine(lambda *_args: None)
    assert engine._start_fast_seat_monitor(page, "http://fake", (CgvSeatGroup(("A1",)),), 1) is False
    assert engine._sync_seat_payload_to_ui(page, {}) is False
    assert engine._enter_visitor_page(page, {}) is False


def test_choose_available_group_matches_both_padded_and_unpadded_seat_numbers():
    # DOM has padded label "A01", "A02"
    elements = [
        {"id": "loc-a1", "label": "A01", "unavailable": False},
        {"id": "loc-a2", "label": "A02", "unavailable": False},
    ]
    # Priority group has unpadded "A1", "A2"
    groups = (CgvSeatGroup(("A1", "A2")),)

    selected = CgvEngine.choose_available_group(elements, groups)
    assert selected is not None
    group, ids = selected
    assert group.seats == ("A1", "A2")
    assert ids == {"A1": "loc-a1", "A2": "loc-a2"}

    # Reverse: DOM has unpadded label "B1", "B2", group has padded "B01", "B02"
    elements_unpadded = [
        {"id": "loc-b1", "label": "B1", "unavailable": False},
        {"id": "loc-b2", "label": "B2", "unavailable": False},
    ]
    groups_padded = (CgvSeatGroup(("B01", "B02")),)
    selected_padded = CgvEngine.choose_available_group(elements_unpadded, groups_padded)
    assert selected_padded is not None
    group_p, ids_p = selected_padded
    assert group_p.seats == ("B01", "B02")
    assert ids_p == {"B01": "loc-b1", "B02": "loc-b2"}


def test_ensure_seat_selected_distinguishes_unselected_and_inactive_classes():
    clicks = []

    class MockLocatorItem:
        def __init__(self, seat_id: str, class_name: str):
            self.seat_id = seat_id
            self.class_name = class_name

        def is_visible(self):
            return True

        def is_enabled(self):
            return True

        def get_attribute(self, attr: str):
            if attr in ("aria-pressed", "aria-selected"):
                return "false"
            if attr == "class":
                return self.class_name
            return None

        def click(self, **_kwargs):
            clicks.append(self.seat_id)

    class MockLocator:
        def __init__(self, item):
            self.item = item

        def count(self):
            return 1

        def nth(self, _idx):
            return self.item

    class Page:
        def evaluate(self, *_args, **_kwargs):
            return None  # Force fallback to locator check

        def locator(self, selector: str):
            if 'data-seatlocno="loc-unselected"' in selector:
                return MockLocator(MockLocatorItem("loc-unselected", "seat_btn unselected"))
            if 'data-seatlocno="loc-inactive"' in selector:
                return MockLocator(MockLocatorItem("loc-inactive", "seat_btn inactive"))
            return MockLocator(MockLocatorItem("unknown", "seat_btn"))

    page = Page()

    # "unselected" must NOT be treated as "selected" -> should click
    res1 = CgvEngine._ensure_seat_selected_by_id(page, "loc-unselected")
    assert res1 is True
    assert "loc-unselected" in clicks

    # "inactive" must NOT be treated as "active" -> should click
    res2 = CgvEngine._ensure_seat_selected_by_id(page, "loc-inactive")
    assert res2 is True
    assert "loc-inactive" in clicks


def test_reconnect_seat_session_handles_browser_cdp_reconnect():
    engine = CgvEngine(lambda *_args: None)
    reconnected = [0]

    class FakePage:
        def __init__(self, url="https://cgv.co.kr"):
            self.url = url

        def is_closed(self):
            return False

        def on(self, *_args, **_kwargs):
            pass

    class FakeContext:
        def __init__(self):
            self.pages = [FakePage()]

        def new_page(self):
            p = FakePage()
            self.pages.append(p)
            return p

    class FakeBrowser:
        def __init__(self):
            self.contexts = [FakeContext()]

        def is_connected(self):
            return False

    class FakeChromium:
        def connect_over_cdp(self, _endpoint):
            reconnected[0] += 1
            fb = FakeBrowser()
            fb.is_connected = lambda: True
            return fb

    class FakePlaywright:
        def __init__(self):
            self.chromium = FakeChromium()

    class FakeChrome:
        def __init__(self):
            self.endpoint = "http://127.0.0.1:9222"

    engine._playwright = FakePlaywright()
    engine._chrome = FakeChrome()
    engine._browser = FakeBrowser()
    engine._enter_visitor_page = lambda _page, _sched: True
    engine._select_visitors = lambda _page, _people: True

    page = engine._reconnect_seat_session(schedule={"siteNo": "0013"}, people=2)
    assert page is not None
    assert reconnected[0] == 1


def test_select_api_seats_in_ui_retries_if_dom_updates_asynchronously():
    engine = CgvEngine(lambda *_args: None)
    attempts = [0]

    def fake_ensure_selected(_page, _seat_id):
        attempts[0] += 1
        # Succeeds on 3rd retry
        return attempts[0] >= 3

    engine._ensure_seat_selected_by_id = fake_ensure_selected
    engine._sync_seat_payload_to_ui = lambda _p, _payload: True

    class Page:
        def wait_for_timeout(self, _ms):
            pass

    class FakeSeat:
        seat_id = "loc-test-1"

    ok = engine._select_api_seats_in_ui(Page(), {}, [FakeSeat()])
    assert ok is True
    assert attempts[0] == 3


def test_select_api_seats_waits_for_real_selection_and_enabled_submit():
    engine = CgvEngine(lambda *_args: None)
    clicks = []
    waits = []
    states = iter(
        (
            {"selectedIds": [], "submitPresent": True, "submitReady": False, "ready": False},
            {
                "selectedIds": ["loc-1"],
                "submitPresent": True,
                "submitReady": False,
                "ready": False,
            },
            {
                "selectedIds": ["loc-1", "loc-2"],
                "submitPresent": True,
                "submitReady": True,
                "ready": True,
            },
        )
    )
    engine._sync_seat_payload_to_ui = lambda *_args: True
    engine._ensure_seat_selected_by_id = (
        lambda _page, seat_id: clicks.append(seat_id) or True
    )
    engine._api_seat_selection_snapshot = lambda *_args: next(states)

    class Page:
        def wait_for_timeout(self, milliseconds):
            waits.append(milliseconds)

    selected = [
        {"seatLocNo": "loc-1"},
        {"seatLocNo": "loc-2"},
    ]
    assert engine._select_api_seats_in_ui(Page(), {}, selected) is True
    assert clicks[:2] == ["loc-1", "loc-2"]
    assert waits == [engine.API_UI_SYNC_INTERVAL_MS] * 2


def test_choose_available_group_normalizes_group_seat_names_case_and_spacing():
    elements = [
        {"id": "loc-a1", "label": "A1", "unavailable": False},
        {"id": "loc-a2", "label": "A2", "unavailable": False},
    ]
    # Lowercase & spaced group input
    groups = (CgvSeatGroup(("a 1", "a 2")),)
    selected = CgvEngine.choose_available_group(elements, groups)

    assert selected is not None
    group, ids = selected
    assert ids["a 1"] == "loc-a1"
    assert ids["a 2"] == "loc-a2"


def test_select_api_seats_in_ui_handles_dict_and_string_seats():
    engine = CgvEngine(lambda *_args: None)
    selected_ids = []

    def fake_ensure_selected(_page, seat_id):
        selected_ids.append(seat_id)
        return True

    engine._ensure_seat_selected_by_id = fake_ensure_selected
    engine._sync_seat_payload_to_ui = lambda _p, _payload: True

    class Page:
        def wait_for_timeout(self, _ms):
            pass

    dict_seat = {"seatLocNo": "loc-dict-1"}
    str_seat = "loc-str-2"

    ok = engine._select_api_seats_in_ui(Page(), {}, [dict_seat, str_seat])
    assert ok is True
    assert selected_ids == ["loc-dict-1", "loc-str-2"]


def test_select_and_hold_seats_survives_wait_for_timeout_exception():
    engine = CgvEngine(lambda *_args: None)

    class Page:
        def wait_for_timeout(self, _ms):
            raise RuntimeError("wait_for_timeout not supported in headless mode")

    engine._available_seat_elements = lambda _p: [
        {"id": "loc-1", "label": "D01", "available": True, "selected": False, "unavailable": False},
    ]
    engine._ensure_seat_selected_by_id = lambda _p, _id: True
    engine._submit_seat_selection = lambda _p: True

    groups = (CgvSeatGroup(("D01",)),)
    held = engine._select_and_hold_seats(Page(), groups, 1, False)
    assert held is True
