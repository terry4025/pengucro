"""Schema-driven answers for Naver Booking's dynamic extra-information form."""

from __future__ import annotations

import copy
import re
from dataclasses import dataclass
from typing import Any, Iterable, Mapping


_COUNT_TITLE = re.compile(
    r"(?:인원|명\b|매(?:수|예약)?|수량|티켓|ticket|people|person)", re.I
)
_PLACEHOLDER = re.compile(r"(?:선택해|입력해|select|choose|placeholder)", re.I)


@dataclass(frozen=True)
class NaverFormAnswer:
    """One resolved question, usable by both GraphQL and browser fallback."""

    index: int
    title: str
    kind: str
    value: str
    selected_values: tuple[str, ...]
    required: bool
    strategy: str
    item_order: int | None = None


def _digits(value: Any) -> str:
    return re.sub(r"\D", "", str(value or ""))


def _is_required(question: Mapping[str, Any]) -> bool:
    value = question.get("required")
    if isinstance(value, bool):
        return value
    return str(value or "").strip().lower() in {"y", "yes", "true", "1"}


def _option_value(option: Any) -> str:
    if isinstance(option, Mapping):
        return str(option.get("value") or "").strip()
    return str(option or "").strip()


def _option_score(value: str, *, title: str, people: str) -> int:
    normalized = re.sub(r"\s+", "", value).lower()
    if not normalized or _PLACEHOLDER.search(normalized):
        return -10_000

    score = 0
    wanted_digits = _digits(people)
    if _COUNT_TITLE.search(title) and wanted_digits and _digits(value) == wanted_digits:
        score += 10_000

    # Prefer a non-applicable/safe acknowledgement over inventing a condition.
    positive = (
        ("해당없", 900),
        ("없음", 700),
        ("동의", 650),
        ("확인", 600),
        ("가능", 550),
        ("알겠", 500),
        ("준수", 450),
        ("예", 300),
        ("네", 300),
        ("yes", 300),
        ("none", 700),
        ("agree", 650),
    )
    for token, points in positive:
        if token in normalized:
            score += points

    negative = ("동의하지", "미동의", "아니오", "아니요", "불가능", "불가합니다", "no")
    if any(token in normalized for token in negative):
        score -= 800
    return score


def _select_option(
    question: Mapping[str, Any], people: str
) -> tuple[dict[str, Any] | None, str]:
    options = [
        option for option in (question.get("options") or [])
        if isinstance(option, Mapping) and _option_value(option)
    ]
    if not options:
        return None, "선택지 없음"

    existing = str(question.get("value") or "").strip()
    if existing:
        for option in options:
            if _option_value(option) == existing:
                return dict(option), "사이트 기본값 유지"

    title = str(question.get("title") or "")
    wanted_digits = _digits(people)
    numeric_options = [
        option for option in options if _digits(_option_value(option))
    ]
    # A warning sentence may contain examples such as "1매=1명, 2명은 2매" while
    # its only real choice is "네 확인했습니다".  Treat it as a quantity selector
    # only when the option values themselves carry quantities.
    if _COUNT_TITLE.search(title) and wanted_digits and numeric_options:
        for option in numeric_options:
            if _digits(_option_value(option)) == wanted_digits:
                return dict(option), "인원·매수 일치"
        return None, "인원·매수 선택지 불일치"

    ranked = sorted(
        enumerate(options),
        key=lambda pair: (
            _option_score(_option_value(pair[1]), title=title, people=people),
            -pair[0],
        ),
        reverse=True,
    )
    selected = dict(ranked[0][1])
    score = _option_score(_option_value(selected), title=title, people=people)
    strategy = "인원·매수 일치" if score >= 10_000 else (
        "안전 확인 선택" if score > 0 else "첫 번째 유효 선택지"
    )
    return selected, strategy


def _text_value(
    question: Mapping[str, Any], reservation: Mapping[str, Any]
) -> tuple[str, str]:
    existing = str(question.get("value") or "").strip()
    if existing:
        return existing, "사이트 기본값 유지"

    title = re.sub(r"\s+", "", str(question.get("title") or "")).lower()
    routes = (
        (("이름", "성명", "name"), "name", "예약자 이름"),
        (("연락처", "전화", "휴대폰", "phone", "tel"), "phone", "예약자 연락처"),
        (("이메일", "email"), "email", "예약자 이메일"),
        (("인원", "몇명", "매수", "수량", "people"), "people", "예약 인원"),
        (("예약일", "관람일", "date"), "reservationDate", "예약 날짜"),
        (("예약시간", "관람시간", "time"), "reservationTime", "예약 시간"),
    )
    for tokens, field, reason in routes:
        if any(token in title for token in tokens):
            value = str(reservation.get(field) or "").strip()
            if value:
                return value, reason
    return "확인했습니다", "필수 확인 문구"


def _expand_per_item(form: Iterable[dict[str, Any]], count: int) -> list[dict[str, Any]]:
    expanded: list[dict[str, Any]] = []
    for raw in form or []:
        if not isinstance(raw, dict):
            continue
        question = copy.deepcopy(raw)
        per_item = str(question.get("perItem") or "").strip().lower() in {
            "y", "yes", "true", "1"
        }
        if not per_item:
            expanded.append(question)
            continue
        for item_order in range(1, max(1, count) + 1):
            clone = copy.deepcopy(question)
            clone["itemOrder"] = item_order
            expanded.append(clone)
    return expanded


def prepare_custom_form_answers(
    form: Any,
    reservation: Mapping[str, Any],
    *,
    item_count: int | None = None,
) -> tuple[list[dict[str, Any]], list[NaverFormAnswer], str | None]:
    """Resolve every Naver custom-form question without relying on its count.

    Unknown future types are handled by their shape: questions with options are
    treated as a choice, and questions without options receive a short text.
    This keeps the implementation forward-compatible while preserving every
    server-provided field in the GraphQL payload.
    """
    if not isinstance(form, list):
        return [], [], None

    people = str(reservation.get("people") or "").strip()
    if item_count is None:
        item_count = int(_digits(people) or 1)
    questions = _expand_per_item(form, max(1, int(item_count or 1)))
    output: list[dict[str, Any]] = []
    answers: list[NaverFormAnswer] = []

    for index, raw in enumerate(questions):
        question = copy.deepcopy(raw)
        kind = str(question.get("type") or "TEXT").strip().upper()
        title = str(question.get("title") or f"추가 정보 {index + 1}").strip()
        required = _is_required(question)
        selected_values: tuple[str, ...] = ()
        strategy = ""

        if kind == "CHECKBOX":
            option, strategy = _select_option(question, people)
            if option is None:
                if required:
                    return [], [], f"필수 추가 입력 '{title}'에 선택지가 없습니다"
                output.append(question)
                continue
            chosen = _option_value(option)
            chosen_original = str(option.get("originalValue") or chosen)
            for raw_option in question.get("options") or []:
                if isinstance(raw_option, dict):
                    raw_option["checked"] = _option_value(raw_option) == chosen
            question["value"] = chosen
            question["originalValue"] = chosen_original
            selected_values = (chosen,)

        elif kind in {"SELECT", "RADIO", "GENDER"} or question.get("options"):
            if kind == "GENDER" and not question.get("options"):
                existing = str(question.get("value") or "").strip()
                value = existing or str(reservation.get("gender") or "남").strip()
                original = value
                strategy = "사이트 기본값 유지" if existing else "기본 성별 선택"
            else:
                option, strategy = _select_option(question, people)
                if option is None:
                    if required:
                        return [], [], f"필수 추가 입력 '{title}'에 선택지가 없습니다"
                    output.append(question)
                    continue
                value = _option_value(option)
                original = str(option.get("originalValue") or value)
            question["value"] = value
            question["originalValue"] = original
            selected_values = (value,)

        elif kind == "BIRTH":
            existing = str(question.get("value") or "").strip()
            value = existing or str(reservation.get("birthDate") or "1990-01-01").strip()
            question["value"] = value
            question["originalValue"] = value
            selected_values = tuple(value.split("-"))
            strategy = "사이트 기본값 유지" if existing else "기본 생년월일 입력"

        else:
            value, strategy = _text_value(question, reservation)
            max_length = int(question.get("maxLength") or (500 if kind == "TEXTAREA" else 100))
            value = value[:max(1, max_length)]
            question["value"] = value
            question["originalValue"] = value
            selected_values = (value,)

        output.append(question)
        answers.append(NaverFormAnswer(
            index=index,
            title=title,
            kind=kind,
            value=str(question.get("value") or ""),
            selected_values=selected_values,
            required=required,
            strategy=strategy,
            item_order=(
                int(question.get("itemOrder"))
                if str(question.get("itemOrder") or "").isdigit()
                else None
            ),
        ))

    return output, answers, None
