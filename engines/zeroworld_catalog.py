from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date

import requests
from bs4 import BeautifulSoup


SELECT_URL = "https://zeroworldkorea.com/core/res/rev.make.sel.php"
SUBJECT_BY_BRANCH = {"1": "A", "2": "B", "4": "A", "5": "A"}


@dataclass(frozen=True)
class ZeroWorldTimeSlot:
    time: str
    slot_id: str
    available: bool


def subject_for_branch(branch_id: str) -> str:
    return SUBJECT_BY_BRANCH.get(str(branch_id), "A")


def decode_body(content: bytes) -> str:
    utf8 = content.decode("utf-8", errors="replace")
    if utf8.count("\ufffd") <= 5:
        return utf8
    return content.decode("cp949", errors="replace")


def calendar_contains_date(html: str, target_date: str) -> bool:
    return f"fun_days_select('{target_date}'" in html


def parse_theme_list(html: str) -> dict[str, str]:
    themes: dict[str, str] = {}
    soup = BeautifulSoup(html, "html.parser")
    for anchor in soup.find_all("a"):
        href = anchor.get("href", "")
        match = re.search(r"fun_theme_select\('([^']+)'", href)
        if not match:
            continue
        name_node = anchor.select_one(".choice-themes__name, .themes__name")
        name = name_node.get_text(" ", strip=True) if name_node else anchor.get_text(" ", strip=True)
        name = re.sub(r"\s+", " ", name).strip()
        if name:
            themes[name] = match.group(1)
    return themes


def parse_time_slots(html: str) -> list[ZeroWorldTimeSlot]:
    slots: list[ZeroWorldTimeSlot] = []
    soup = BeautifulSoup(html, "html.parser")
    for element in soup.find_all(["a", "button"]):
        label = element.get_text(" ", strip=True)
        time_match = re.search(r"\b(\d{1,2}:\d{2})\b", label)
        if not time_match:
            continue
        action = element.get("href", "") or element.get("onclick", "")
        slot_match = re.search(r"fun_theme_time_select\('([^']+)'", action)
        classes = set(element.get("class", []))
        disabled = element.has_attr("disabled") or classes.intersection(
            {"disable", "disabled", "close", "sold-out"}
        )
        available = bool(slot_match) and not disabled
        slots.append(
            ZeroWorldTimeSlot(
                time=time_match.group(1).zfill(5),
                slot_id=slot_match.group(1) if slot_match else "",
                available=available,
            )
        )
    return slots


def fetch_themes(branch_id: str, reservation_date: str | None = None, timeout: float = 8.0) -> dict[str, str]:
    target_date = reservation_date or date.today().isoformat()
    response = requests.post(
        SELECT_URL,
        data={
            "act": "theme_list",
            "zizum_num": branch_id,
            "rev_days": target_date,
            "theme_num": "",
            "s_subj": subject_for_branch(branch_id),
        },
        headers={"User-Agent": "Mozilla/5.0"},
        timeout=timeout,
    )
    response.raise_for_status()
    return parse_theme_list(decode_body(response.content))


def fetch_time_slots(
    branch_id: str,
    reservation_date: str,
    theme_id: str,
    timeout: float = 8.0,
) -> list[ZeroWorldTimeSlot]:
    response = requests.post(
        SELECT_URL,
        data={
            "act": "theme_time_list",
            "zizum_num": branch_id,
            "rev_days": reservation_date,
            "theme_num": theme_id,
        },
        headers={"User-Agent": "Mozilla/5.0"},
        timeout=timeout,
    )
    response.raise_for_status()
    return parse_time_slots(decode_body(response.content))
