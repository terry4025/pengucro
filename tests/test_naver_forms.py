"""Dynamic Naver extra-form preparation."""

from engines.naver_forms import prepare_custom_form_answers


RESERVATION = {
    "name": "홍길동",
    "phone": "010-1234-5678",
    "email": "test@example.com",
    "people": "2",
    "reservationDate": "2026-08-15",
    "reservationTime": "15:00",
}


def select(title, *values, required="y", per_item="n"):
    return {
        "type": "SELECT",
        "title": title,
        "required": required,
        "perItem": per_item,
        "options": [
            {"idx": index, "value": value, "originalValue": value}
            for index, value in enumerate(values)
        ],
    }


def test_resolves_any_number_of_immersive_required_questions():
    form = [
        select("1매예약은 1명예약 입니다. 즉 2명일경우 최소2매 예약", "1매", "2매", "3매"),
        select("심장,고음,진동 등 공포 연출 포함", "해당 없음", "관람 불가"),
        select("주차불가//한국어가 가능해야 관람가능", "확인했습니다", "동의하지 않습니다"),
        select("공연시간 5분전까지 도착", "동의합니다", "동의하지 않습니다"),
        select("지각시 중도입장불가, 환불불가", "확인 및 동의", "미동의"),
        select("추가로 생긴 여섯 번째 질문", "예", "아니요"),
    ]

    prepared, answers, error = prepare_custom_form_answers(form, RESERVATION)

    assert error is None
    assert len(prepared) == len(answers) == 6
    assert [answer.value for answer in answers] == [
        "2매", "해당 없음", "확인했습니다", "동의합니다", "확인 및 동의", "예"
    ]
    assert all(question["value"] for question in prepared)
    assert all(question["originalValue"] for question in prepared)


def test_supports_text_textarea_checkbox_birth_gender_and_unknown_shapes():
    form = [
        {"type": "TEXT", "title": "관람자 이름", "required": "y"},
        {"type": "TEXTAREA", "title": "필수 확인", "required": "y"},
        {
            "type": "CHECKBOX",
            "title": "주의사항",
            "required": "y",
            "options": [{"value": "확인"}, {"value": "미동의"}],
        },
        {"type": "BIRTH", "title": "생년월일", "required": "y"},
        {"type": "GENDER", "title": "성별", "required": "y"},
        {"type": "FUTURE_INPUT", "title": "새 입력 유형", "required": "y"},
    ]

    prepared, answers, error = prepare_custom_form_answers(form, RESERVATION)

    assert error is None
    assert [answer.value for answer in answers] == [
        "홍길동", "확인했습니다", "확인", "1990-01-01", "남", "확인했습니다"
    ]
    assert prepared[2]["options"][0]["checked"] is True
    assert prepared[2]["options"][1]["checked"] is False


def test_per_item_questions_expand_to_actual_ticket_count():
    form = [select("관람 제한 확인", "해당 없음", "관람 불가", per_item="y")]

    prepared, answers, error = prepare_custom_form_answers(
        form, RESERVATION, item_count=3
    )

    assert error is None
    assert len(prepared) == 3
    assert [question["itemOrder"] for question in prepared] == [1, 2, 3]
    assert [answer.item_order for answer in answers] == [1, 2, 3]


def test_required_people_question_never_guesses_a_different_count():
    form = [select("예약 인원", "1명", "2명")]
    reservation = dict(RESERVATION, people="4")

    prepared, answers, error = prepare_custom_form_answers(form, reservation)

    assert prepared == []
    assert answers == []
    assert "예약 인원" in error


def test_quantity_examples_in_title_do_not_hide_acknowledgement_option():
    form = [select(
        "1매예약은 1명예약 입니다. 즉 2명일경우 최소2매 예약하셔야합니다",
        "네 확인했습니다",
    )]
    reservation = dict(RESERVATION, people="1")

    prepared, answers, error = prepare_custom_form_answers(form, reservation)

    assert error is None
    assert prepared[0]["value"] == "네 확인했습니다"
    assert answers[0].value == "네 확인했습니다"
    assert answers[0].strategy == "안전 확인 선택"
