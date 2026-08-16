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
    assert "buildPricePayload" in script
    assert "buildHoldPayload" in script
    assert "state.conflicts += 1" in script
    assert "resume()" in script


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

    class PaymentWaiter:
        @property
        def last(self):
            return self

        def wait_for(self, **_kwargs):
            actions.append("payment")

    class Page:
        def get_by_text(self, *_args, **_kwargs):
            return PaymentWaiter()

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
    engine._click_visible_by_text = lambda *_args: actions.append("click") or True
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
    assert actions == ["monitor-with-direct-hold", "ui", "cache", "click", "payment"]


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
    engine._click_visible_by_text = lambda *_args: False
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

