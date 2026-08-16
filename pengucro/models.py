from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Any, Mapping


STANDARD_MODE = "일반 사이트"
NAVER_MODE = "네이버 예약"
TRIPCOM_MODE = "Trip.com 이벤트"
LEGACY_MODE_MAP = {
    "고속 (Async)": STANDARD_MODE,
    "일반 (Sync)": STANDARD_MODE,
    "네이버 (Playwright)": NAVER_MODE,
    "Trip.com 핫딜": TRIPCOM_MODE,
}


def parse_bool_flag(value: Any, default: bool = False) -> bool:
    """Parse UI/config flags without treating the string ``"false"`` as true."""
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return value != 0
    if isinstance(value, str):
        normalized = value.strip().lower()
        if normalized in {"1", "true", "yes", "on", "y"}:
            return True
        if normalized in {"0", "false", "no", "off", "n", ""}:
            return False
    return default


def coerce_bool(value: Any) -> bool:
    """Interpret persisted/UI boolean values without treating "false" as true."""
    if isinstance(value, bool):
        return value
    if value is None:
        return False
    if isinstance(value, str):
        normalized = value.strip().lower()
        if normalized in {"", "0", "false", "off", "no", "n"}:
            return False
        if normalized in {"1", "true", "on", "yes", "y"}:
            return True
    return bool(value)


class BookingEventType(str, Enum):
    INFO = "info"
    ATTEMPT = "attempt"
    WARNING = "warning"
    ERROR = "error"
    SUCCESS = "success"
    STATE = "state"


@dataclass(frozen=True)
class BookingEvent:
    event_type: BookingEventType
    message: str
    attempt_count: int = 0
    details: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class BookingResult:
    success: bool
    message: str
    booking_number: str = ""
    details: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class ReservationRequest:
    site: str
    branch: str
    reservation_date: str
    reservation_time: str
    name: str
    phone: str
    people: int
    theme_pk: str
    branch_label: str = ""
    theme_label: str = ""
    payment_type: str = "1"
    policy: bool = True
    developer_mode: bool = False
    site_url: str = ""
    naver_time_offset: float = 0.0
    yescaptcha_enabled: bool = False
    yescaptcha_test_mode: bool = False
    yescaptcha_client_key: str = ""
    yescaptcha_soft_id: str = "26273"
    engine_metadata: Mapping[str, Any] = field(default_factory=dict)

    @classmethod
    def from_mapping(cls, site: str, values: Mapping[str, Any]) -> "ReservationRequest":
        raw_people = str(values.get("people", "")).strip()
        people = int(raw_people) if raw_people.isdigit() else 0
        reservation_time = str(values.get("reservationTime", "")).strip()
        if len(reservation_time) == 5:
            reservation_time += ":00"
        return cls(
            site=site,
            branch=str(values.get("branch", "")).strip(),
            reservation_date=str(values.get("reservationDate", "")).strip(),
            reservation_time=reservation_time,
            name=str(values.get("name", "")).strip(),
            phone=str(values.get("phone", "")).strip(),
            people=people,
            theme_pk=str(values.get("themePK", "")).strip(),
            branch_label=str(values.get("branchLabel", "")).strip(),
            theme_label=str(values.get("themeLabel", "")).strip(),
            payment_type=str(values.get("paymentType", "1")),
            policy=str(values.get("policy", "true")).lower() == "true",
            developer_mode=parse_bool_flag(values.get("devMode", False)),
            site_url=str(values.get("site_url", "")).strip(),
            naver_time_offset=float(values.get("naver_time_offset", 0.0) or 0.0),
            yescaptcha_enabled=coerce_bool(values.get("yescaptcha_enabled", False)),
            yescaptcha_test_mode=coerce_bool(values.get("yescaptcha_test_mode", False)),
            yescaptcha_client_key=str(values.get("yescaptcha_client_key", "")).strip(),
            yescaptcha_soft_id=str(values.get("yescaptcha_soft_id", "26273")).strip() or "26273",
            engine_metadata=dict(values.get("engine_metadata", {})),
        )

    def validate(
        self, *, phone_required: bool = True, name_required: bool = True
    ) -> list[str]:
        errors: list[str] = []
        if not self.theme_pk:
            errors.append("테마를 선택해주세요.")
        try:
            selected_date = datetime.strptime(self.reservation_date, "%Y-%m-%d").date()
            if selected_date < datetime.now().date():
                errors.append("지난 날짜는 예약할 수 없습니다.")
        except ValueError:
            errors.append("날짜를 YYYY-MM-DD 형식으로 입력해주세요.")
        try:
            datetime.strptime(self.reservation_time, "%H:%M:%S")
        except ValueError:
            errors.append("시간을 HH:MM 형식으로 입력해주세요.")
        if name_required and not self.name:
            errors.append("예약자 이름을 입력해주세요.")
        phone_digits = "".join(ch for ch in self.phone if ch.isdigit())
        if phone_required and len(phone_digits) not in (10, 11):
            errors.append("전화번호를 정확히 입력해주세요.")
        if not 1 <= self.people <= 10:
            errors.append("인원 수는 1명부터 10명 사이여야 합니다.")
        if not self.branch:
            errors.append("지점을 선택해주세요.")
        return errors

    def to_engine_payload(self) -> dict[str, Any]:
        return {
            "branch": self.branch,
            "reservationDate": self.reservation_date,
            "name": self.name,
            "phone": self.phone,
            "people": str(self.people),
            "themePK": self.theme_pk,
            "reservationTime": self.reservation_time,
            "paymentType": self.payment_type,
            "policy": "true" if self.policy else "false",
            "devMode": self.developer_mode,
            "site_url": self.site_url,
            "naver_time_offset": self.naver_time_offset,
            "branchLabel": self.branch_label,
            "themeLabel": self.theme_label,
            # Engines receive this dict, not the request object. Leaving these
            # out silently disabled YesCaptcha: the keyescape engine read
            # payload.get("yescaptcha_enabled") and always got False, so it
            # never asked the API and fell back to clicking the widget forever.
            "yescaptcha_enabled": self.yescaptcha_enabled,
            "yescaptcha_test_mode": self.yescaptcha_test_mode,
            "yescaptcha_client_key": self.yescaptcha_client_key,
            "yescaptcha_soft_id": self.yescaptcha_soft_id,
            "engine_metadata": dict(self.engine_metadata),
        }

    def summary(self) -> str:
        return (
            f"사이트: {self.site}\n"
            f"지점: {self.branch_label or self.branch}\n"
            f"테마: {self.theme_label or self.theme_pk}\n"
            f"날짜: {self.reservation_date}\n"
            f"시간: {self.reservation_time[:5]}\n"
            f"인원: {self.people}명\n"
            f"예약자: {self.name}\n"
            f"실행 방식: "
            + (
                "개발자 테스트 (Npay 선결제는 임시 예약 후 결제 직전 정지)"
                if self.developer_mode
                else (
                    "실제 예약 제출 (Npay 선결제는 최종 결제까지 자동 진행)"
                    if self.site == "네이버 예약"
                    else "실제 예약 제출"
                )
            )
        )
