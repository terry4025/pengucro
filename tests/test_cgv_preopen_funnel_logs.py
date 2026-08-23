from __future__ import annotations

from engines.cgv_engine_funnel_runtime import CgvEngine
from engines.cgv_engine_movie_identity_runtime import _PREOPEN_SELECTION_ACTIVE


def _schedule(
    time_text: str,
    *,
    auditorium: str = "IMAX관",
    format_name: str = "IMAX LASER 2D",
    movie: str = "오디세이",
    seq: str = "1",
    controlled: str = "N",
):
    return {
        "siteNo": "0013",
        "scnYmd": "20260826",
        "scnsNo": f"screen-{seq}",
        "scnSseq": seq,
        "scnsrtTm": time_text,
        "movNm": movie,
        "expoProdNm": movie,
        "expoScnsNm": auditorium,
        "scnsNm": auditorium,
        "movkndDsplEnm": format_name,
        "movkndDsplNm": format_name,
        "cntlYn": controlled,
    }


def _payload(*items):
    return {"data": list(items)}


def _engine(preferred=("14:00",)):
    engine = CgvEngine(log_callback=lambda *_args, **_kwargs: None, success_callback=lambda *_args, **_kwargs: None)
    engine._priority_movie = "오디세이"
    engine._priority_auditorium = "IMAX관"
    engine._priority_format = "IMAX LASER 2D"
    engine._priority_preferred_times = list(preferred)
    engine._preopen_diag_signature = None
    logs: list[tuple[str, str]] = []
    engine.log = lambda message, level="info": logs.append((str(message), str(level)))
    return engine, logs


def _run(engine: CgvEngine, payload):
    token = _PREOPEN_SELECTION_ACTIVE.set(True)
    try:
        engine._log_preopen_schedule_diagnostics(payload)
    finally:
        _PREOPEN_SELECTION_ACTIVE.reset(token)


def test_funnel_log_explains_time_rejection_with_actual_times():
    engine, logs = _engine(("14:00",))
    _run(engine, _payload(_schedule("0900"), _schedule("2200", seq="2")))

    messages = [message for message, _level in logs]
    assert any(
        "[CGV][미오픈 판정] 전체 2 → 영화 2 → 관/포맷 2 → 회차ID 2 → "
        "판매가능 2 → 시간허용 0 → 판매대기 0 → 최종 -" in message
        for message in messages
    )
    assert any(
        "[CGV][미오픈 거절] 시간 조건 0" in message
        and "참고시간 [14:00]" in message
        and "실제 판매가능 [09:00, 22:00]" in message
        and "허용 ±90분" in message
        for message in messages
    )


def test_funnel_log_explains_partial_identity_before_attempt():
    engine, logs = _engine()
    partial = _schedule("1400")
    partial["scnsNo"] = ""
    partial["scnSseq"] = ""
    _run(engine, _payload(partial))

    messages = [message for message, _level in logs]
    assert any(
        "회차ID 0 → 판매가능 0 → 시간허용 0 → 판매대기 0 → 최종 -" in message
        for message in messages
    )
    assert any(
        "[CGV][미오픈 거절] 회차ID 단계 0" in message
        and "missing=scnsNo+scnSseq" in message
        for message in messages
    )


def test_funnel_log_explains_locked_schedule_before_attempt():
    engine, logs = _engine()
    _run(engine, _payload(_schedule("1400", controlled="Y")))

    messages = [message for message, _level in logs]
    assert any(
        "회차ID 1 → 판매가능 0 → 시간허용 0 → 판매대기 0 → 최종 -" in message
        for message in messages
    )
    assert any(
        "[CGV][미오픈 거절] 판매가능 단계 0" in message
        and "cntlYn=Y 잠금 1개" in message
        and "잠긴 실제시간 [14:00]" in message
        for message in messages
    )


def test_funnel_log_explains_published_schedule_waiting_for_inventory():
    engine, logs = _engine()
    awaiting = _schedule("1400")
    awaiting.update({"frSeatCnt": 0, "stcnt": 400})

    _run(engine, _payload(awaiting))

    messages = [message for message, _level in logs]
    assert any("시간허용 0 → 판매대기 1 → 최종 -" in message for message in messages)
    assert any(
        "[CGV][미오픈 거절] 좌석 재고 0" in message
        and "재고 개시 즉시 자동 진입" in message
        for message in messages
    )


def test_funnel_log_records_final_drift_mapping_and_deduplicates_same_state():
    engine, logs = _engine(("14:00",))
    payload = _payload(_schedule("1350"), _schedule("1730", seq="2"))

    _run(engine, payload)
    first_count = len(logs)
    _run(engine, payload)

    assert len(logs) == first_count
    messages = [message for message, _level in logs]
    assert any("판매가능 2 → 시간허용 1 → 판매대기 0 → 최종 13:50" in message for message in messages)
    assert any(
        "[CGV][미오픈 선택] 참고 14:00 → 실제 13:50" in message
        and "scnsNo=screen-1" in message
        and "scnSseq=1" in message
        and "다음 단계=관람인원/좌석 진입" in message
        for message in messages
    )
