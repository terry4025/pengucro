from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date
from html import unescape

import requests
from bs4 import BeautifulSoup

SELECT_URL = "https://zeroworldkorea.com/core/res/rev.make.sel.php"
SUBJECT_BY_BRANCH = {"1": "A", "2": "B", "4": "A", "5": "A"}


@dataclass(frozen=True)
class ZeroWorldTimeSlot:
    time: str
    slot_id: str
    available: bool
    # Naver omits hourly records entirely for a calendar day that has not opened.
    # In that case the picker copies the latest timetable for the same weekday.
    # It is selectable as a time choice, but deliberately remains unavailable so
    # the UI never claims the closed target date can already be booked.
    estimated: bool = False
    source_date: str = ""
    # Why this row is estimated.  Keeping the basis explicit lets the picker
    # distinguish an exact weekday template from a looser weekday/weekend
    # fallback instead of presenting every estimate as equally reliable.
    estimate_basis: str = ""
    # Optional reason why an estimate was used even though an exact request was
    # attempted.  The picker uses this to distinguish an unopened date from a
    # temporary site outage without changing booking availability semantics.
    estimate_reason: str = ""

    @property
    def selectable(self) -> bool:
        return self.available or self.estimated


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


_TIME_ELEMENT_RE = re.compile(
    r"<(?P<tag>a|button)\b(?P<attrs>[^>]*)>(?P<body>.*?)</(?P=tag)\s*>",
    re.IGNORECASE | re.DOTALL,
)
_HTML_TAG_RE = re.compile(r"<[^>]+>")
_CLASS_RE = re.compile(r"\bclass\s*=\s*(['\"])(.*?)\1", re.IGNORECASE | re.DOTALL)
_SLOT_ACTION_RE = re.compile(r"fun_theme_time_select\('([^']+)'", re.IGNORECASE)
_ANY_TIME_RE = re.compile(r"\b(\d{1,2}:\d{2})\b")


def find_target_time_slot(
    html: str,
    target_time: str,
) -> tuple[ZeroWorldTimeSlot | None, int]:
    """Find one target slot without building a full BeautifulSoup tree.

    Live responses consist of independent ``a``/``button`` time elements.  The
    regex path keeps the 50-worker event loop free; unfamiliar markup falls
    back to the compatibility parser instead of silently changing behaviour.
    """

    normalized_target = str(target_time or "")[:5].zfill(5)
    candidates: list[ZeroWorldTimeSlot] = []
    slot_count = 0
    recognized_markup = False

    for element in _TIME_ELEMENT_RE.finditer(str(html or "")):
        attrs = element.group("attrs")
        label = unescape(_HTML_TAG_RE.sub(" ", element.group("body")))
        time_match = _ANY_TIME_RE.search(label)
        if not time_match:
            continue
        recognized_markup = True
        slot_count += 1
        slot_time = time_match.group(1).zfill(5)
        if slot_time != normalized_target:
            continue

        action_match = _SLOT_ACTION_RE.search(attrs)
        class_match = _CLASS_RE.search(attrs)
        classes = set((class_match.group(2) if class_match else "").lower().split())
        disabled = bool(
            re.search(r"(?:^|\s)disabled(?:\s*=|\s|$)", attrs, re.IGNORECASE)
        ) or bool(classes.intersection({"disable", "disabled", "close", "sold-out"}))
        candidates.append(
            ZeroWorldTimeSlot(
                time=slot_time,
                slot_id=action_match.group(1) if action_match else "",
                available=bool(action_match) and not disabled,
            )
        )

    if recognized_markup:
        if not candidates:
            return None, slot_count
        return next((slot for slot in candidates if slot.available), candidates[0]), slot_count

    # Preserve compatibility if the provider changes its element shape.
    slots = parse_time_slots(html)
    matching = [slot for slot in slots if slot.time == normalized_target]
    if not matching:
        return None, len(slots)
    return next((slot for slot in matching if slot.available), matching[0]), len(slots)


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
