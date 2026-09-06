from engines.cgv_client import CgvSeatGroup
from engines.cgv_engine_movie_identity_runtime import (
    _PREOPEN_SELECTION_ACTIVE,
    _PREOPEN_TIME_DRIFT,
    select_schedule,
)
from engines.cgv_engine_priority_ladder import CgvEngine as PriorityLadderCgvEngine
from engines.cgv_engine_priority_ladder_runtime import CgvEngine
from engines.cgv_engine_visitor_dom_runtime import CgvEngine as VisitorDomCgvEngine
from engines.registry import EngineRegistry
from pengucro.models import STANDARD_MODE


def noop(*_args, **_kwargs):
    return None


def _schedule(time_text: str, seq: str, *, controlled: str = "N"):
    return {
        "siteNo": "0013",
        "scnYmd": "20260820",
        "scnsNo": "01",
        "scnSseq": seq,
        "scnsrtTm": time_text,
        "movNm": "오디세이",
        "expoProdNm": "오디세이",
        "expoScnsNm": "IMAX관",
        "scnsNm": "IMAX관",
        "movkndDsplEnm": "IMAX LASER 2D",
        "movkndDsplNm": "IMAX LASER 2D",
        "cntlYn": controlled,
    }


def _seat_payload(*available_labels: str):
    return {
        "statusCode": 0,
        "data": {
            "items": [
                {
                    "seats": [
                        {
                            "seatLocNo": label,
                            "seatRowNm": label[0],
                            "seatNo": label[1:],
                            "seatStusCd": "00",
                            "seatSaleYn": "Y",
                        }
                        for label in available_labels
                    ]
                }
            ]
        },
    }


def _final_registry_engine():
    engine = EngineRegistry.create(
        site_name="CGV",
        mode=STANDARD_MODE,
        payload={},
        custom_sites={},
        log_callback=noop,
        success_callback=noop,
    )
    engine._browser_auth_data = lambda _page: {}
    engine._seat_url = lambda schedule, _cust_no="": (
        f"seat://{schedule['scnsrtTm']}"
    )
    return engine


def _configure_priority_ladder(engine, schedules, groups):
    engine._priority_movie = "오디세이"
    engine._priority_auditorium = "IMAX관"
    engine._priority_format = "IMAX LASER 2D"
    engine._priority_preferred_times = [
        f"{item['scnsrtTm'][:2]}:{item['scnsrtTm'][2:]}" for item in schedules
    ]
    engine._priority_manual_groups = tuple(groups)
    engine._priority_schedule_payload = {"data": list(schedules)}
    engine._priority_last_schedule_refresh = 10**9
    engine._refresh_priority_schedule_payload = lambda _page: None


def test_final_ladder_preserves_member_cust_no_in_reused_seat_url():
    engine = CgvEngine(noop)
    page = object()
    engine._priority_seed_page = page
    engine._browser_auth_data = lambda observed: {"custNo": "member-42"} if observed is page else {}
    engine._seat_url = lambda _schedule, cust_no="": f"seat-url?custNo={cust_no}"

    engine._seed_initial_payload({"siteNo": "0013"}, {"statusCode": 0})

    assert engine._initial_seat_response["url"] == "seat-url?custNo=member-42"
    assert engine._initial_seat_response["data"] == {"statusCode": 0}


def test_registry_uses_final_ladder_without_losing_visitor_runtime():
    engine = EngineRegistry.create(
        site_name="CGV",
        mode=STANDARD_MODE,
        payload={},
        custom_sites={},
        log_callback=noop,
        success_callback=noop,
    )

    assert isinstance(engine, CgvEngine)
    assert isinstance(engine, PriorityLadderCgvEngine)
    assert isinstance(engine, VisitorDomCgvEngine)
    assert engine._priority_time_label(_schedule("1400", "1")) == "14:00"


def test_final_registry_prioritizes_live_replacement_over_stale_primary():
    engine = _final_registry_engine()
    primary = _schedule("1400", "old")
    replacement = _schedule("1350", "new")
    engine._priority_movie = "오디세이"
    engine._priority_auditorium = "IMAX관"
    engine._priority_format = "IMAX LASER 2D"
    engine._priority_preferred_times = ["14:00"]
    engine._priority_schedule_payload = {"data": [replacement]}

    token = _PREOPEN_SELECTION_ACTIVE.set(True)
    drift_token = _PREOPEN_TIME_DRIFT.set(15)
    try:
        candidates = engine._ordered_schedule_candidates(primary)
    finally:
        _PREOPEN_TIME_DRIFT.reset(drift_token)
        _PREOPEN_SELECTION_ACTIVE.reset(token)

    assert [item["scnSseq"] for item in candidates] == ["new"]  # Removed publications cannot remain booking fallbacks.


def test_final_registry_staged_unlock_drift_and_base_conflict_reach_next_time():
    engine = _final_registry_engine()
    group = CgvSeatGroup(("C8", "C9"))
    partial = _schedule("1350", "partial")
    partial["scnsNo"] = ""
    partial["scnSseq"] = ""
    locked_first = _schedule("1350", "1", controlled="Y")
    locked_second = _schedule("1720", "2", controlled="Y")
    unlocked_first = dict(locked_first, cntlYn="N")
    unlocked_second = dict(locked_second, cntlYn="N")
    locked_payload = {"data": [locked_first, locked_second]}
    unlocked_payload = {"data": [unlocked_first, unlocked_second]}
    selection = {
        "movie": "오디세이",
        "auditorium": "IMAX관",
        "format_name": "IMAX LASER 2D",
        "preferred_times": ["14:00", "17:30"],
    }

    token = _PREOPEN_SELECTION_ACTIVE.set(True)
    drift_token = _PREOPEN_TIME_DRIFT.set(15)
    try:
        assert select_schedule({"data": []}, **selection) is None
        assert select_schedule({"data": [partial]}, **selection) is None
        assert select_schedule(locked_payload, **selection) is None

        engine._schedule_fingerprint = engine._schedule_payload_fingerprint(
            locked_payload
        )
        engine._schedule_burst_until = 0.0
        engine._update_schedule_watch_health({"ok": True, "data": unlocked_payload})
        assert engine._schedule_burst_until > 0.0

        primary = select_schedule(unlocked_payload, **selection)
        assert primary is not None
        assert primary["scnsrtTm"] == "1350"

        engine._priority_movie = selection["movie"]
        engine._priority_auditorium = selection["auditorium"]
        engine._priority_format = selection["format_name"]
        engine._priority_preferred_times = list(selection["preferred_times"])
        engine._priority_manual_groups = (group,)
        engine._priority_schedule_payload = unlocked_payload
        engine._priority_last_schedule_refresh = 10**9
        engine._refresh_priority_schedule_payload = lambda _page: None
        seat_payload = _seat_payload(*group.seats)
        engine._read_schedule_once = (
            lambda _page, schedule, people, *, allow_initial: (
                group,
                seat_payload,
                200,
            )
        )

        events = []
        monitor_times = []
        current_time = [""]

        def start_monitor(_page, seat_url, _groups, _concurrency, **_kwargs):
            current_time[0] = str(seat_url).removeprefix("seat://")
            monitor_times.append(current_time[0])
            return True

        def read_monitor(_page):
            if current_time[0] == "1350":
                events.append("conflict:1350")
                return {
                    "running": False,
                    "claiming": False,
                    "completed": 1,
                    "failureKind": "seat-conflict",
                    "hit": None,
                }
            events.append("hold:1720")
            return {
                "running": False,
                "claiming": False,
                "completed": 1,
                "hit": {
                    "data": seat_payload,
                    "elapsedMs": 10,
                    "transaction": {
                        "priceResponse": {"statusCode": 0},
                        "holdResponse": {
                            "statusCode": 0,
                            "data": {"movAtktNo": "hold-next-time"},
                        },
                        "holdPayload": {"seatPrmpDataList": []},
                        "elapsedMs": 8,
                    },
                },
            }

        engine.FAST_MONITOR_READ_INTERVAL = 0.0
        engine._start_fast_seat_monitor = start_monitor
        engine._read_fast_seat_monitor = read_monitor
        engine._stop_fast_seat_monitor = lambda _page: None
        engine._activate_priority_schedule = (
            lambda _page, schedule, _people: events.append(
                f"activate:{schedule['scnsrtTm']}"
            )
            or True
        )
        engine._select_api_seats_in_ui = (
            lambda *_args: events.append(f"sync:{current_time[0]}") or True
        )
        engine._install_cached_hold_responses = lambda *_args: None
        engine._submit_seat_selection = lambda *_args: True
        engine._restore_fetch = lambda *_args: None

        result = engine._watch_and_hold_api(
            object(),
            primary,
            (group,),
            2,
            True,
            {},
        )
    finally:
        _PREOPEN_TIME_DRIFT.reset(drift_token)
        _PREOPEN_SELECTION_ACTIVE.reset(token)

    assert result == (True, False)
    assert monitor_times == ["1350", "1720"]
    assert events == [
        "conflict:1350",
        "hold:1720",
        "activate:1720",
        "sync:1720",
    ]


def test_final_registry_retries_groups_then_next_time_after_hold_conflicts(
    monkeypatch,
):
    engine = _final_registry_engine()
    first = _schedule("1400", "1")
    second = _schedule("1730", "2")
    first_group = CgvSeatGroup(("C8", "C9"))
    second_group = CgvSeatGroup(("B8", "B9"))
    payload = _seat_payload("C8", "C9", "B8", "B9")
    engine._fetch_priority_seat_payload = lambda *_args: {"ok": True, "status": 200, "data": payload}
    _configure_priority_ladder(
        engine,
        (first, second),
        (first_group, second_group),
    )
    engine._read_schedule_once = (
        lambda _page, schedule, people, *, allow_initial: (
            engine._choose_priority_group(payload, schedule, people),
            payload,
            200,
        )
    )

    attempts = []
    developer_flags = []

    def delegated_hold(
        self,
        page,
        schedule,
        groups,
        people,
        developer_mode,
        _cgv,
    ):
        attempt = (schedule["scnsrtTm"], groups[0].seats)
        attempts.append(attempt)
        developer_flags.append(developer_mode)
        if attempt == ("1730", ("B8", "B9")):
            assert self._prepare_api_hold_ui(page, schedule, people) is True
            return True, False
        self._last_fast_monitor_exit_reason = "seat-conflict"
        return False, False

    monkeypatch.setattr(
        VisitorDomCgvEngine,
        "_watch_and_hold_api",
        delegated_hold,
    )
    engine._activate_priority_schedule = lambda *_args: True

    result = engine._watch_and_hold_api(
        object(),
        first,
        engine._priority_manual_groups,
        2,
        True,
        {},
    )

    assert result == (True, False)
    assert attempts == [
        ("1400", ("C8", "C9")),
        ("1400", ("B8", "B9")),
        ("1730", ("C8", "C9")),
        ("1730", ("B8", "B9")),
    ]
    assert developer_flags == [True, True, True, True]


def test_final_registry_holds_fallback_time_before_activating_its_ui(monkeypatch):
    engine = _final_registry_engine()
    first = _schedule("1400", "1")
    second = _schedule("1730", "2")
    group = CgvSeatGroup(("C8", "C9"))
    payload = _seat_payload("C8", "C9")
    _configure_priority_ladder(engine, (first, second), (group,))

    def read_once(_page, schedule, _people, *, allow_initial):
        if schedule["scnsrtTm"] == "1400":
            return None, {"statusCode": 0, "data": {"items": []}}, 200
        return group, payload, 200

    engine._read_schedule_once = read_once
    events = []
    engine._activate_priority_schedule = (
        lambda _page, schedule, _people: events.append(
            f"activate:{schedule['scnsrtTm']}"
        )
        or True
    )

    def delegated_hold(
        self,
        page,
        schedule,
        _groups,
        people,
        _developer_mode,
        _cgv,
    ):
        events.append(f"hold:{schedule['scnsrtTm']}")
        assert self._prepare_api_hold_ui(page, schedule, people) is True
        return True, False

    monkeypatch.setattr(
        VisitorDomCgvEngine,
        "_watch_and_hold_api",
        delegated_hold,
    )

    result = engine._watch_and_hold_api(
        object(),
        first,
        engine._priority_manual_groups,
        2,
        True,
        {},
    )

    assert result == (True, False)
    assert events == ["hold:1730", "activate:1730"]


def test_final_registry_developer_mode_reaches_checkout_but_never_final_pay(
    monkeypatch,
):
    engine = _final_registry_engine()
    first = _schedule("1400", "1")
    second = _schedule("1730", "2")
    group = CgvSeatGroup(("C8", "C9"))
    payload = _seat_payload("C8", "C9")
    _configure_priority_ladder(engine, (first, second), (group,))
    engine._read_schedule_once = (
        lambda _page, schedule, _people, *, allow_initial: (
            (None, {"statusCode": 0, "data": {"items": []}}, 200)
            if schedule["scnsrtTm"] == "1400"
            else (group, payload, 200)
        )
    )
    engine._activate_priority_schedule = lambda *_args: True

    observed_developer_modes = []

    def delegated_hold(
        self,
        page,
        schedule,
        _groups,
        people,
        developer_mode,
        _cgv,
    ):
        observed_developer_modes.append(developer_mode)
        assert self._prepare_api_hold_ui(page, schedule, people) is True
        return True, False

    monkeypatch.setattr(
        VisitorDomCgvEngine,
        "_watch_and_hold_api",
        delegated_hold,
    )

    held, fallback = engine._watch_and_hold_api(
        object(),
        first,
        engine._priority_manual_groups,
        2,
        True,
        {},
    )
    assert (held, fallback) == (True, False)
    assert observed_developer_modes == [True]

    cgv_page = object()
    naver_page = object()
    forbidden_calls = []
    monkeypatch.setattr(engine, "_advance_to_cgv_payment_methods", lambda _page: True)
    monkeypatch.setattr(engine, "_select_cgv_npay_method", lambda _page: True)
    monkeypatch.setattr(engine, "_accept_cgv_payment_terms", lambda _page: True)
    monkeypatch.setattr(engine, "_open_naver_payment_page", lambda _page: naver_page)
    monkeypatch.setattr(
        engine,
        "_prepare_naver_card",
        lambda _page: forbidden_calls.append("card") or True,
    )
    monkeypatch.setattr(
        engine,
        "_click_naver_final_payment",
        lambda _page: forbidden_calls.append("pay") or True,
    )
    monkeypatch.setattr(
        engine,
        "_wait_for_cgv_payment_confirmation",
        lambda *_args: forbidden_calls.append("confirmation") or True,
    )

    assert engine._proceed_naver_pay_checkout(
        cgv_page,
        developer_mode=observed_developer_modes[-1],
        npay_password="123456",
    ) is True
    assert forbidden_calls == []
