from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Mapping


@dataclass(frozen=True)
class CatchTableRsaKey:
    public_key_pem: str
    c1: int = 0
    c2: int = 0
    c3: int = 0
    c4: int = 0


@dataclass(frozen=True)
class CatchTableDaySlot:
    date: str  # e.g. "2026-08-25"
    available_status: str  # e.g. "AVAILABLE", "DAY_OFF", "CLOSED", "FULL"
    available_person_counts: tuple[int, ...] = ()
    benefit: Any | None = None

    @property
    def is_available(self) -> bool:
        return self.available_status == "AVAILABLE"


@dataclass(frozen=True)
class CatchTableTimeSlot:
    time: str  # e.g. "1900" or "19:00"
    date: str  # e.g. "260825"
    shop_ref: str
    table_type: str = "H"
    period_gubun: str = "D"  # "D" for dinner, "L" for lunch
    available_yn: bool = True
    menu_set_seq: int | None = None
    menu_set_seq_comma_list: str = ""
    online_notice_seq: int | None = None
    resp2: str = ""
    raw_data: dict[str, Any] = field(default_factory=dict)

    @property
    def formatted_time(self) -> str:
        t = self.time.replace(":", "")
        if len(t) == 4:
            return f"{t[:2]}:{t[2:]}"
        return self.time


@dataclass(frozen=True)
class CatchTableHoldingResult:
    holding_seq: int
    shop_ref: str
    visit_date: str
    visit_time: str
    person_count: int
    table_type: str
    menu_set_seqs: tuple[int, ...] = ()
    deposit_required: bool = False
    deposit_amount: int = 0
    raw_data: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class CatchTableSessionValidation:
    is_valid: bool
    user_name: str = ""
    user_phone: str = ""
    user_email: str = ""
    user_seq: int = 0
    error_message: str = ""
    raw_data: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class CatchTableBookingConfig:
    shop_alias_or_ref: str
    target_date: str  # "YYYY-MM-DD" or "YYMMDD"
    person_count: int = 2
    time_priorities: tuple[str, ...] = ()  # e.g. ("19:00", "18:30", "19:30")
    table_type: str = "_ALL_"
    user_name: str = ""
    user_phone: str = ""
    user_email: str = ""
    use_login: bool = True  # True: 회원 로그인 모드, False: 비회원(익명) 선점 모드
    auth_token: str = ""
    device_id: str = ""
    cookies: Mapping[str, str] = field(default_factory=dict)
    auto_create: bool = False
    open_time: str = ""  # "HH:MM:SS" if open-run mode
    site_url: str = "https://app.catchtable.co.kr"
    api_url: str = "https://ct-api.catchtable.co.kr"
