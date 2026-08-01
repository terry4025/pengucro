from datetime import date, timedelta

import pytest

from pengucro.models import ReservationRequest


def valid_request(**overrides):
    values = {
        "site": "제로월드",
        "branch": "4",
        "reservation_date": (date.today() + timedelta(days=1)).isoformat(),
        "reservation_time": "14:00:00",
        "name": "테스트",
        "phone": "010-1234-5678",
        "people": 2,
        "theme_pk": "28",
    }
    values.update(overrides)
    return ReservationRequest(**values)


def test_valid_request_has_no_errors():
    assert valid_request().validate() == []


def test_request_rejects_invalid_date_time_phone_and_people():
    request = valid_request(
        reservation_date="2020-01-01",
        reservation_time="29:99:00",
        phone="123",
        people=0,
    )
    errors = request.validate()
    assert any("지난 날짜" in error for error in errors)
    assert any("시간" in error for error in errors)
    assert any("전화번호" in error for error in errors)
    assert any("인원" in error for error in errors)


def test_mapping_normalizes_time_and_people():
    request = ReservationRequest.from_mapping(
        "제로월드",
        {
            "branch": "5",
            "reservationDate": (date.today() + timedelta(days=1)).isoformat(),
            "reservationTime": "21:20",
            "name": "홍길동",
            "phone": "01012345678",
            "people": "3",
            "themePK": "36",
            "themeLabel": "사랑...하는...감?",
            "engine_metadata": {"theme": {"theme_num": "44"}},
        },
    )
    assert request.reservation_time == "21:20:00"
    assert request.people == 3
    assert request.to_engine_payload()["people"] == "3"
    assert request.to_engine_payload()["themeLabel"] == "사랑...하는...감?"
    assert request.to_engine_payload()["engine_metadata"]["theme"]["theme_num"] == "44"


@pytest.mark.parametrize("value", [False, 0, "0", "false", "False", "off", "no", ""])
def test_mapping_does_not_treat_false_like_strings_as_developer_mode(value):
    request = ReservationRequest.from_mapping(
        "네이버 예약",
        {
            "branch": "1",
            "reservationDate": (date.today() + timedelta(days=1)).isoformat(),
            "reservationTime": "22:00",
            "name": "홍길동",
            "people": "2",
            "themePK": "https://booking.naver.com/items/1",
            "devMode": value,
        },
    )
    assert request.developer_mode is False
    assert request.to_engine_payload()["devMode"] is False


def test_summary_uses_human_readable_labels():
    request = ReservationRequest.from_mapping(
        "제로월드",
        {
            "branch": "4",
            "branchLabel": "강남점",
            "reservationDate": (date.today() + timedelta(days=1)).isoformat(),
            "reservationTime": "14:00",
            "name": "홍길동",
            "phone": "01012345678",
            "people": "2",
            "themePK": "28",
            "themeLabel": "아이엠",
        },
    )
    assert "지점: 강남점" in request.summary()
    assert "테마: 아이엠" in request.summary()
    assert "실행 방식: 실제 예약 제출" in request.summary()


def test_summary_makes_developer_mode_unmistakable():
    request = valid_request(developer_mode=True)
    assert "개발자 테스트 (실제 제출 안 함)" in request.summary()
