"""Validate a private DPSNNN plan; never embed contact information in builds."""
from __future__ import annotations
import json
import re
from datetime import datetime
from pathlib import Path


def load_plan(path):
    from engines.dpsnnn_engine import DPSNNN_BRANCHES
    raw = json.loads(Path(path).read_text(encoding="utf-8-sig"))
    rows = raw.get("reservations") if isinstance(raw, dict) else None
    if not isinstance(rows, list) or not 1 <= len(rows) <= 4:
        raise ValueError("예약 목록은 1~4건이어야 합니다.")
    result, seen = [], set()
    for index, row in enumerate(rows, 1):
        if not isinstance(row, dict):
            raise ValueError(f"{index}번 예약 형식 오류")
        data = {key: str(row.get(key, "")).strip() for key in
                ("branch", "themePK", "reservationDate", "reservationTime", "name", "phone")}
        branch = DPSNNN_BRANCHES.get(data["branch"])
        if branch is None or data["themePK"] not in branch["themes"]:
            raise ValueError(f"{index}번 지점·테마를 확인해주세요.")
        try:
            date = datetime.strptime(data["reservationDate"], "%Y-%m-%d")
            if date.strftime("%Y-%m-%d") != data["reservationDate"]:
                raise ValueError()
        except ValueError:
            raise ValueError(f"{index}번 날짜는 YYYY-MM-DD로 입력해주세요.") from None
        if not re.fullmatch(r"(?:[01]\d|2[0-3]):[0-5]\d(?::00)?", data["reservationTime"]):
            raise ValueError(f"{index}번 시간을 확인해주세요.")
        data["reservationTime"] = data["reservationTime"][:5] + ":00"
        data["phone"] = re.sub(r"\D", "", data["phone"])
        if len(data["name"]) < 2 or not re.fullmatch(r"01\d{8,9}", data["phone"]):
            raise ValueError(f"{index}번 예약자 이름·휴대전화번호를 확인해주세요.")
        data["people"] = str(row.get("people", 2))
        if not data["people"].isdigit() or not 2 <= int(data["people"]) <= 6:
            raise ValueError(f"{index}번 인원을 확인해주세요.")
        identity = tuple(data[k] for k in ("branch", "themePK", "reservationDate", "reservationTime"))
        if identity in seen:
            raise ValueError("같은 테마·날짜·시간을 중복 지정할 수 없습니다.")
        seen.add(identity)
        data["devMode"] = False
        result.append(data)
    return result
