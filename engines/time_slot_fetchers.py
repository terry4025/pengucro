from __future__ import annotations
import re
import threading
import time
import urllib.parse
from datetime import date, datetime, timedelta
from typing import Any
import requests
from bs4 import BeautifulSoup
from engines.zeroworld_catalog import ZeroWorldTimeSlot, parse_time_slots, decode_body
from pengucro.storage import load_json, save_json


DOOMESCAPE_TIMETABLE_CACHE = "doomescape_timetable_cache.json"
_doomescape_cache_lock = threading.Lock()
_doomescape_page_lock = threading.Lock()
_doomescape_page_cache: dict[tuple[str, str, str, object], tuple[float, str, str]] = {}
_doomescape_page_inflight: dict[tuple[str, str, str, object], threading.Event] = {}
_doomescape_seed_lock = threading.Lock()
_doomescape_seed_inflight: dict[tuple[str, str, str, object], threading.Event] = {}
_doomescape_seed_complete: set[tuple[str, str, str, object]] = set()
DOOMESCAPE_PAGE_CACHE_SECONDS = 15.0
DOOMESCAPE_ERROR_CACHE_SECONDS = 1.5
DOOMESCAPE_DISCOVERY_DAYS = 8

def fetch_zeroworld_slots(
    base_url: str,
    branch_id: str,
    theme_id: str,
    date_str: str,
    engine_options: dict[str, Any],
    timeout: float
) -> list[ZeroWorldTimeSlot]:
    select_url = engine_options.get("select_url") or urllib.parse.urljoin(base_url, "/core/res/rev.make.sel.php")
    subject = engine_options.get("subject_by_branch", {}).get(branch_id, "A")
    response = requests.post(
        select_url,
        data={
            "act": "theme_time_list",
            "zizum_num": branch_id,
            "rev_days": date_str,
            "theme_num": theme_id,
            "s_subj": subject,
        },
        headers={"User-Agent": "Mozilla/5.0"},
        timeout=timeout,
    )
    response.raise_for_status()
    return parse_time_slots(decode_body(response.content))


def fetch_keyescape_slots(
    base_url: str,
    branch_id: str,
    theme_id: str,
    date_str: str,
    timeout: float
) -> list[ZeroWorldTimeSlot]:
    from data.themes import KEYESCAPE_THEMES

    theme_num = theme_id
    theme_info_num = theme_id
    theme_map = KEYESCAPE_THEMES.get(str(branch_id), {})
    for t_name, t_val in theme_map.items():
        if isinstance(t_val, dict):
            if str(t_val.get("info_num")) == str(theme_id):
                theme_num = t_val.get("theme_num", theme_id)
                theme_info_num = t_val.get("info_num", theme_id)
                break
            if str(t_val.get("theme_num")) == str(theme_id):
                theme_num = t_val.get("theme_num", theme_id)
                theme_info_num = t_val.get("info_num", theme_id)
                break
        elif t_val == theme_id:
            break
            
    api_url = urllib.parse.urljoin(base_url, "/controller/run_proc.php")
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
        "Referer": f"{base_url.rstrip('/')}/reservation.php",
        "Origin": base_url.rstrip('/')
    }
    def post(payload):
        response = requests.post(
            api_url,
            data=payload,
            headers=headers,
            timeout=timeout,
        )
        response.raise_for_status()
        return response.json()

    def rows_to_slots(rows, *, estimated=False, source_date="", basis=""):
        result = []
        for slot in rows or []:
            slot_time = f"{int(slot.get('hh', 0)):02d}:{int(slot.get('mm', 0)):02d}"
            result.append(ZeroWorldTimeSlot(
                time=slot_time,
                # Estimated picker rows are display-only.  Even where Keyescape
                # reuses a schedule id, the booking engine validates that id
                # independently before allowing its one-page fast path.
                slot_id="" if estimated else str(slot.get("num", "")),
                available=False if estimated else slot.get("enable") == "Y",
                estimated=estimated,
                source_date=source_date,
                estimate_basis=basis,
            ))
        return sorted(result, key=lambda item: item.time)

    exact = post({
            't': 'get_theme_time',
            'date': date_str,
            'zizumNum': branch_id,
            'themeNum': theme_num,
        })
    if exact.get("status") and exact.get("data"):
        return rows_to_slots(exact["data"])

    # The target date is not published.  Read the same server-date/calendar
    # metadata used by Keyescape's own picker and choose the strongest template:
    # exact weekday first, then the same weekday/weekend class, then nearest.
    try:
        target_day = datetime.strptime(date_str, "%Y-%m-%d").date()
        theme = post({'t': 'get_theme_date', 'num': theme_info_num})
        server_day = datetime.strptime(
            str((theme.get("calendarData") or {}).get("today") or ""),
            "%Y-%m-%d",
        ).date()
        branch = post({'t': 'get_theme_info_list', 'zizum_num': branch_id})
        doing = 0
        for item in branch.get("data") or []:
            if (
                str(item.get("info_num")) == str(theme_info_num)
                or str(item.get("theme_num")) == str(theme_num)
            ):
                doing = max(0, int(item.get("doing") or 0))
                break
        if doing <= 0:
            return []
    except (TypeError, ValueError, KeyError):
        return []

    def day_type(day):
        return "weekend" if day.weekday() >= 5 else "weekday"

    allowed_candidates = [
        server_day + timedelta(days=offset)
        for offset in range(doing)
        if server_day + timedelta(days=offset) != target_day
    ]
    allowed_candidates.sort(key=lambda day: (
        0 if day.weekday() == target_day.weekday() else
        1 if day_type(day) == day_type(target_day) else 2,
        abs((target_day - day).days),
        -day.toordinal(),
    ))

    # If the only matching weekday in the active window is the still-blocked
    # target itself (for example Sunday before a Saturday opening), first try the
    # most recent historical occurrence: Sat 15 -> Sat 8, never Fri 14.
    weekday_back = (server_day.weekday() - target_day.weekday()) % 7
    historical_same_weekday = server_day - timedelta(days=weekday_back)
    if historical_same_weekday == target_day:
        historical_same_weekday -= timedelta(days=7)
    candidates = [historical_same_weekday]
    candidates.extend(
        day for day in allowed_candidates if day not in candidates
    )

    for source_day in candidates:
        if source_day.weekday() == target_day.weekday():
            basis = "same_weekday"
        elif day_type(source_day) == day_type(target_day):
            basis = "same_day_type"
        else:
            basis = "nearest"
        template = post({
            't': 'get_theme_time',
            'date': source_day.isoformat(),
            'zizumNum': branch_id,
            'themeNum': theme_num,
        })
        if template.get("status") and template.get("data"):
            return rows_to_slots(
                template["data"], estimated=True,
                source_date=source_day.isoformat(), basis=basis,
            )
    return []


def fetch_jigubyeol_slots(
    base_url: str,
    branch_id: str,
    theme_id: str,
    date_str: str,
    timeout: float
) -> list[ZeroWorldTimeSlot]:
    params = {
        "branch": branch_id,
        "theme": theme_id,
        "themePK": theme_id,
        "date": date_str
    }
    url = urllib.parse.urljoin(base_url, "/reservation")
    response = requests.get(
        url,
        params=params,
        headers={"User-Agent": "Mozilla/5.0"},
        timeout=timeout
    )
    response.raise_for_status()
    
    html = response.text
    slots = []
    soup = BeautifulSoup(html, "html.parser")
    
    # 1. button 요소를 먼저 탐색 (실제 플레이33 및 지구별 라라벨 렌더링 스타일)
    buttons = soup.find_all("button")
    for btn in buttons:
        btn_text = btn.get_text(" ", strip=True)
        time_match = re.search(r"\b(\d{2}:\d{2})\b", btn_text)
        if time_match:
            time_str = time_match.group(1)
            if any(s.time == time_str for s in slots):
                continue
                
            disabled = btn.has_attr("disabled")
            classes = set(btn.get("class", []))
            
            is_disabled = disabled or any(kw in btn_text for kw in ["마감", "종료", "불가", "매진", "soldout"]) or any(cls in " ".join(classes).lower() for cls in ["disable", "disabled", "close", "sold-out", "none"])
            
            slots.append(ZeroWorldTimeSlot(time=time_str, slot_id=time_str, available=not is_disabled))

    # 2. 만약 button으로 아무것도 못 찾았을 경우, input[name="time"][type="radio"] 탐색 (Fallback 1)
    if not slots:
        time_inputs = soup.find_all("input", attrs={"name": "time", "type": "radio"})
        for inp in time_inputs:
            val = inp.get("value", "").strip()
            if re.match(r"^\d{2}:\d{2}(:\d{2})?$", val):
                time_str = val[:5]
                parent = inp.parent
                disabled = inp.has_attr("disabled")
                classes = set(inp.get("class", []))
                if parent:
                    classes.update(parent.get("class", []))
                
                parent_text = parent.get_text() if parent else ""
                is_disabled = disabled or any(kw in parent_text for kw in ["마감", "종료", "불가"]) or any(cls in " ".join(classes).lower() for cls in ["disable", "disabled", "close", "sold-out", "none"])
                
                slots.append(ZeroWorldTimeSlot(time=time_str, slot_id=time_str, available=not is_disabled))
                
    # 3. Fallback 2: 일반 시간 형식의 a, span, div 중 class에 time/slot이 묻어나는 것
    if not slots:
        for element in soup.find_all(["a", "span", "div"]):
            classes = " ".join(element.get("class", [])) if element.get("class") else ""
            if "time" in classes.lower() or "slot" in classes.lower():
                label = element.get_text(" ", strip=True)
                time_match = re.search(r"\b(\d{2}:\d{2})\b", label)
                if time_match:
                    time_str = time_match.group(1)
                    if any(s.time == time_str for s in slots):
                        continue
                    disabled = element.has_attr("disabled") or any(cls in classes.lower() for cls in ["disable", "disabled", "close", "sold-out"])
                    slots.append(ZeroWorldTimeSlot(time=time_str, slot_id=time_str, available=not disabled))
                    
    return sorted(slots, key=lambda s: s.time)


def fetch_naver_slots(
    url: str,
    date_str: str,
    timeout: float
) -> list[ZeroWorldTimeSlot]:
    """Naver time slots, read through GraphQL.

    This used to call ``api.booking.naver.com/v3.0/.../schedules``. That endpoint
    is gone: it answers ``403 {"errorCode":"NotAccessibleUrl"}`` with or without
    Referer/Origin headers, so the picker had been silently showing no times for
    every Naver site. ``hourlySchedule`` on the site's own GraphQL endpoint returns
    the same information and needs no login.
    """
    from engines.naver_api import NaverApiError, NaverBookingApi, parse_ids
    from engines.site_parser import normalize_naver_url

    ids = parse_ids(url)
    if not ids:
        normalized = normalize_naver_url(url)
        ids = parse_ids(normalized) if normalized else None
    if not ids:
        return []

    service_id, business_id, item_id = ids
    api = NaverBookingApi(business_id, item_id, service_id, timeout=timeout)
    estimated = False
    source_date = ""
    try:
        slots = api.fetch_slots(date_str)
        if not slots:
            # A closed Naver calendar day has no hourly records.  Its weekday
            # pattern is still useful for choosing a target time, so read the
            # latest published occurrence of the same weekday.  Availability is
            # intentionally false below: these are timetable choices, not a
            # promise that the target date is already bookable.
            target_day = datetime.strptime(date_str, "%Y-%m-%d").date()
            meta = api.fetch_item_meta()
            reference_day = (
                meta.server_time.date()
                if meta.server_time is not None
                else datetime.now().date()
            )
            history = api.fetch_slots(
                (reference_day - timedelta(days=28)).isoformat(),
                (reference_day + timedelta(days=14)).isoformat(),
            )
            same_weekday = [
                slot for slot in history
                if slot.start.date() < target_day
                and slot.start.weekday() == target_day.weekday()
            ]
            if same_weekday:
                source_day = max(slot.start.date() for slot in same_weekday)
                slots = [
                    slot for slot in same_weekday if slot.start.date() == source_day
                ]
                estimated = True
                source_date = source_day.isoformat()
    except NaverApiError:
        return []
    except (TypeError, ValueError):
        return []
    finally:
        api.close()

    return sorted(
        (
            ZeroWorldTimeSlot(
                time=slot.time_str,
                slot_id="" if estimated else slot.slot_id,
                available=False if estimated else slot.is_open(),
                estimated=estimated,
                source_date=source_date,
                estimate_basis="same_weekday" if estimated else "",
            )
            for slot in slots
        ),
        key=lambda entry: entry.time,
    )


def _doomescape_cache_key(base_url: str, branch_id: str, theme_id: str) -> str:
    parsed = urllib.parse.urlparse(base_url)
    host = (parsed.netloc or parsed.path).lower().rstrip("/")
    return f"{host}|{branch_id}|{theme_id}"


def _remember_doomescape_timetable(
    base_url: str,
    branch_id: str,
    theme_id: str,
    date_str: str,
    slots: list[ZeroWorldTimeSlot],
) -> None:
    times = sorted({slot.time for slot in slots if slot.time})
    if not times:
        return
    try:
        source_day = datetime.strptime(date_str, "%Y-%m-%d").date()
    except (TypeError, ValueError):
        return

    key = _doomescape_cache_key(base_url, branch_id, theme_id)
    snapshot = {
        "date": source_day.isoformat(),
        "weekday": source_day.weekday(),
        "day_type": "weekend" if source_day.weekday() >= 5 else "weekday",
        "times": times,
        "updated_at": datetime.now().isoformat(timespec="seconds"),
    }
    with _doomescape_cache_lock:
        cache = load_json(DOOMESCAPE_TIMETABLE_CACHE, {"version": 1, "entries": {}})
        if not isinstance(cache, dict):
            cache = {"version": 1, "entries": {}}
        entries = cache.setdefault("entries", {})
        if not isinstance(entries, dict):
            entries = {}
            cache["entries"] = entries
        history = entries.get(key, [])
        if not isinstance(history, list):
            history = []
        history = [row for row in history if isinstance(row, dict) and row.get("date") != date_str]
        history.append(snapshot)
        entries[key] = sorted(history, key=lambda row: str(row.get("date", "")))[-24:]
        save_json(DOOMESCAPE_TIMETABLE_CACHE, cache)


def _estimate_doomescape_timetable(
    base_url: str,
    branch_id: str,
    theme_id: str,
    date_str: str,
    *,
    reason: str = "",
) -> list[ZeroWorldTimeSlot]:
    try:
        target_day = datetime.strptime(date_str, "%Y-%m-%d").date()
    except (TypeError, ValueError):
        return []

    key = _doomescape_cache_key(base_url, branch_id, theme_id)
    with _doomescape_cache_lock:
        cache = load_json(DOOMESCAPE_TIMETABLE_CACHE, {"entries": {}})
    entries = cache.get("entries", {}) if isinstance(cache, dict) else {}
    history = entries.get(key, []) if isinstance(entries, dict) else []
    candidates = []
    for row in history if isinstance(history, list) else []:
        if not isinstance(row, dict) or not isinstance(row.get("times"), list):
            continue
        try:
            source_day = datetime.strptime(str(row.get("date", "")), "%Y-%m-%d").date()
        except ValueError:
            continue
        times = sorted({str(value)[:5] for value in row["times"] if re.fullmatch(r"\d{1,2}:\d{2}", str(value))})
        if not times:
            continue
        if source_day.weekday() == target_day.weekday():
            basis = "same_weekday"
        elif (source_day.weekday() >= 5) == (target_day.weekday() >= 5):
            basis = "same_day_type"
        else:
            # A weekend timetable must never be displayed as a weekday estimate
            # (or vice versa).  Doom Escape occasionally publishes holiday
            # schedules that look like weekends on a weekday, so crossing this
            # boundary is less useful than returning no estimate.
            continue
        candidates.append(
            (
                abs((target_day - source_day).days),
                -source_day.toordinal(),
                source_day,
                basis,
                tuple(times),
            )
        )

    if not candidates:
        return []

    exact_weekday = [row for row in candidates if row[3] == "same_weekday"]
    if exact_weekday:
        _distance, _recent, source_day, basis, signature = min(exact_weekday)
    else:
        # When the exact weekday is outside the site's short published window,
        # prefer the timetable signature seen on the most dates.  This prevents
        # one substitute holiday from overriding the normal weekday schedule.
        signature_counts: dict[tuple[str, ...], int] = {}
        for row in candidates:
            signature_counts[row[4]] = signature_counts.get(row[4], 0) + 1
        best_count = max(signature_counts.values())
        dominant = [
            row for row in candidates if signature_counts[row[4]] == best_count
        ]
        _distance, _recent, source_day, basis, signature = min(dominant)
    times = list(signature)
    return [
        ZeroWorldTimeSlot(
            time=value,
            slot_id="",
            available=False,
            estimated=True,
            source_date=source_day.isoformat(),
            estimate_basis=basis,
            estimate_reason=reason,
        )
        for value in times
    ]


def _doomescape_page_key(
    base_url: str, branch_id: str, date_str: str
) -> tuple[str, str, str, object]:
    parsed = urllib.parse.urlparse(base_url)
    host = (parsed.netloc or parsed.path).lower().rstrip("/")
    # Including the callable identity keeps monkeypatched test transports from
    # sharing process-global responses while production requests still share.
    return host, str(branch_id), str(date_str), requests.get


def _doomescape_page_is_healthy(html: str) -> bool:
    lowered = (html or "").lower()
    return "tm_box" in lowered or "name=\"rev_days\"" in lowered or "name='rev_days'" in lowered


def _doomescape_outage_reason(response_url: str, html: str) -> str:
    lowered_url = (response_url or "").lower()
    lowered_html = (html or "").lower()
    if "traffic_over" in lowered_url or "일일전송량" in html or "트래픽" in html:
        return "traffic_over"
    if "service unavailable" in lowered_html or "temporarily unavailable" in lowered_html:
        return "server_outage"
    return ""


def _fetch_shared_doomescape_page(
    base_url: str,
    branch_id: str,
    date_str: str,
    timeout: float,
) -> tuple[str, str]:
    key = _doomescape_page_key(base_url, branch_id, date_str)

    while True:
        now = time.monotonic()
        with _doomescape_page_lock:
            cached = _doomescape_page_cache.get(key)
            if cached:
                cached_at, cached_url, cached_html = cached
                ttl = (
                    DOOMESCAPE_PAGE_CACHE_SECONDS
                    if _doomescape_page_is_healthy(cached_html)
                    else DOOMESCAPE_ERROR_CACHE_SECONDS
                )
                if now - cached_at <= ttl:
                    return cached_url, cached_html
                _doomescape_page_cache.pop(key, None)

            inflight = _doomescape_page_inflight.get(key)
            if inflight is None:
                inflight = threading.Event()
                _doomescape_page_inflight[key] = inflight
                owner = True
            else:
                owner = False

        if owner:
            break
        # The owner always signals in finally.  A bounded wait also prevents a
        # dead thread from permanently blocking future timetable lookups.
        inflight.wait(max(float(timeout), 1.0) + 1.0)

    url = urllib.parse.urljoin(base_url, "/layout/res/home.php")
    params = {
        "go": "rev.make",
        "s_zizum": branch_id,
        "rev_days": date_str,
    }
    try:
        response = requests.get(
            url,
            params=params,
            headers={"User-Agent": "Mozilla/5.0"},
            timeout=timeout,
        )
        response.raise_for_status()
        html = response.content.decode("utf-8", errors="ignore")
        response_url = str(getattr(response, "url", url) or url)
        with _doomescape_page_lock:
            _doomescape_page_cache[key] = (time.monotonic(), response_url, html)
        return response_url, html
    finally:
        with _doomescape_page_lock:
            event = _doomescape_page_inflight.pop(key, None)
            if event is not None:
                event.set()


def _doomescape_slots_from_box(box) -> list[ZeroWorldTimeSlot]:
    slots: list[ZeroWorldTimeSlot] = []
    for anchor in box.find_all("a"):
        num_span = anchor.find("span", class_="num")
        txt_span = anchor.find("span", class_="txt")
        if not num_span:
            continue
        time_match = re.search(r"(\d{2}:\d{2})", num_span.get_text(" ", strip=True))
        if not time_match:
            continue
        time_value = time_match.group(1)
        if any(slot.time == time_value for slot in slots):
            continue
        is_closed = bool(txt_span and "예약마감" in txt_span.get_text(" ", strip=True))
        match = re.search(r"theme_time_num=(\d+)", anchor.get("href", ""))
        slot_id = match.group(1) if match else time_value
        slots.append(
            ZeroWorldTimeSlot(
                time=time_value,
                slot_id=slot_id,
                available=not is_closed,
            )
        )
    return sorted(slots, key=lambda slot: slot.time)


def _cache_all_doomescape_themes(
    base_url: str,
    branch_id: str,
    date_str: str,
    boxes,
    theme_map: dict[str, str],
) -> dict[str, list[ZeroWorldTimeSlot]]:
    parsed: dict[str, list[ZeroWorldTimeSlot]] = {}
    for theme_name, mapped_theme_id in theme_map.items():
        target_box = next(
            (
                box
                for box in boxes
                if theme_name in box.get_text(" ", strip=True)
            ),
            None,
        )
        if target_box is None:
            continue
        slots = _doomescape_slots_from_box(target_box)
        if not slots:
            continue
        parsed[str(mapped_theme_id)] = slots
        _remember_doomescape_timetable(
            base_url, str(branch_id), str(mapped_theme_id), date_str, slots
        )
    return parsed


def _doomescape_reference_day() -> date:
    return datetime.now().date()


def _seed_doomescape_published_timetables(
    base_url: str,
    branch_id: str,
    target_date: str,
    timeout: float,
    theme_map: dict[str, str],
) -> None:
    """Populate a cold cache from the site's currently published date window.

    Doom Escape returns the requested date even when that date is not open, but
    every theme box is empty.  A new installation therefore has nothing to use
    for an unopened date.  One low-frequency discovery pass reads the short
    public window and stores every theme found on each page.
    """

    parsed = urllib.parse.urlparse(base_url)
    host = (parsed.netloc or parsed.path).lower().rstrip("/")
    reference_day = _doomescape_reference_day()
    seed_key = (host, str(branch_id), reference_day.isoformat(), requests.get)

    while True:
        with _doomescape_seed_lock:
            if seed_key in _doomescape_seed_complete:
                return
            inflight = _doomescape_seed_inflight.get(seed_key)
            if inflight is None:
                inflight = threading.Event()
                _doomescape_seed_inflight[seed_key] = inflight
                owner = True
            else:
                owner = False
        if owner:
            break
        inflight.wait(max(float(timeout), 1.0) * DOOMESCAPE_DISCOVERY_DAYS + 1.0)

    found_page = False
    empty_after_found = 0
    try:
        for offset in range(DOOMESCAPE_DISCOVERY_DAYS):
            candidate = (reference_day + timedelta(days=offset)).isoformat()
            if candidate == target_date:
                continue
            try:
                response_url, html = _fetch_shared_doomescape_page(
                    base_url, str(branch_id), candidate, timeout
                )
            except requests.RequestException:
                continue
            if _doomescape_outage_reason(response_url, html):
                continue

            soup = BeautifulSoup(html, "html.parser")
            selected_day = soup.find("input", attrs={"name": "rev_days"})
            selected_value = selected_day.get("value", "").strip() if selected_day else ""
            if selected_value and selected_value != candidate:
                continue
            boxes = soup.find_all("div", class_="tm_box")
            parsed_by_theme = _cache_all_doomescape_themes(
                base_url, str(branch_id), candidate, boxes, theme_map
            )
            if parsed_by_theme:
                found_page = True
                empty_after_found = 0
            elif found_page:
                empty_after_found += 1
                if empty_after_found >= 2:
                    break
    finally:
        with _doomescape_seed_lock:
            if found_page:
                _doomescape_seed_complete.add(seed_key)
            event = _doomescape_seed_inflight.pop(seed_key, None)
            if event is not None:
                event.set()


def fetch_doomescape_slots(
    base_url: str,
    branch_id: str,
    theme_id: str,
    date_str: str,
    timeout: float
) -> list[ZeroWorldTimeSlot]:
    from data.themes import DOOMESCAPE_THEMES
    
    theme_name = ""
    theme_map = DOOMESCAPE_THEMES.get(str(branch_id), {})
    for name, tid in theme_map.items():
        if str(tid) == str(theme_id):
            theme_name = name
            break
            
    if not theme_name:
        from engines.doomescape_engine import DoomEscapeEngine
        theme_name = DoomEscapeEngine.THEME_ID_TO_NAME.get(str(theme_id), "")

    try:
        response_url, html = _fetch_shared_doomescape_page(
            base_url, str(branch_id), date_str, timeout
        )
    except requests.RequestException as exc:
        cached = _estimate_doomescape_timetable(
            base_url,
            str(branch_id),
            str(theme_id),
            date_str,
            reason="server_outage",
        )
        if cached:
            return cached
        raise ValueError(
            "둠이스케이프 서버 연결 장애 · 저장된 시간표가 없습니다. 서버 복구 후 다시 조회합니다."
        ) from exc

    outage_reason = _doomescape_outage_reason(response_url, html)
    if outage_reason:
        cached = _estimate_doomescape_timetable(
            base_url,
            str(branch_id),
            str(theme_id),
            date_str,
            reason=outage_reason,
        )
        if cached:
            return cached
        message = "일일 트래픽 초과" if outage_reason == "traffic_over" else "서버 장애"
        raise ValueError(
            f"둠이스케이프 서버 {message} · 저장된 시간표가 없습니다. 서버 복구 후 다시 조회합니다."
        )

    soup = BeautifulSoup(html, "html.parser")

    selected_day = soup.find("input", attrs={"name": "rev_days"})
    selected_value = selected_day.get("value", "").strip() if selected_day else ""
    if selected_value and selected_value != date_str:
        raise ValueError(
            f"둠이스케이프가 요청 날짜({date_str}) 대신 {selected_value} 예약 페이지를 반환했습니다."
        )
    all_boxes = soup.find_all('div', class_='tm_box')
    if not selected_day and not all_boxes:
        raise ValueError("둠이스케이프 예약 페이지 형식을 확인할 수 없습니다. 서버 복구 후 다시 조회합니다.")

    parsed_by_theme = _cache_all_doomescape_themes(
        base_url, str(branch_id), date_str, all_boxes, theme_map
    )
    slots = parsed_by_theme.get(str(theme_id), [])
    if not slots and theme_name:
        target_box = next(
            (
                box
                for box in all_boxes
                if theme_name in box.get_text(" ", strip=True)
            ),
            None,
        )
        slots = _doomescape_slots_from_box(target_box) if target_box else []
    elif not slots and not theme_name:
        slots = sorted(
            (slot for box in all_boxes for slot in _doomescape_slots_from_box(box)),
            key=lambda slot: slot.time,
        )

    if slots:
        if str(theme_id) not in parsed_by_theme:
            _remember_doomescape_timetable(
                base_url, str(branch_id), str(theme_id), date_str, slots
            )
        return slots
    cached_estimate = _estimate_doomescape_timetable(
        base_url, str(branch_id), str(theme_id), date_str
    )
    if cached_estimate and cached_estimate[0].estimate_basis == "same_weekday":
        return cached_estimate
    _seed_doomescape_published_timetables(
        base_url, str(branch_id), date_str, timeout, theme_map
    )
    discovered_estimate = _estimate_doomescape_timetable(
        base_url, str(branch_id), str(theme_id), date_str
    )
    return discovered_estimate or cached_estimate


def fetch_any_time_slots(
    site_config: dict[str, Any],
    branch_id: str,
    theme_id: str,
    date_str: str,
    timeout: float = 8.0
) -> list[ZeroWorldTimeSlot]:
    engine_id = site_config.get("engine_id", "")
    base_url = site_config.get("base_url", "") or site_config.get("url", "")
    if not base_url:
        return []

    if engine_id in ("zeroworld_laravel", "zeroworld_gu", "zeroworld_shin", "sinbiworld"):
        return fetch_zeroworld_slots(base_url, branch_id, theme_id, date_str, site_config.get("engine_options", {}), timeout)
    elif engine_id == "keyescape":
        return fetch_keyescape_slots(base_url, branch_id, theme_id, date_str, timeout)
    elif engine_id == "jigubyeol":
        return fetch_jigubyeol_slots(base_url, branch_id, theme_id, date_str, timeout)
    elif engine_id == "doomescape":
        return fetch_doomescape_slots(base_url, branch_id, theme_id, date_str, timeout)
    elif engine_id == "naver":
        return fetch_naver_slots(site_config.get("url", ""), date_str, timeout)
    elif engine_id == "dpsnnn":
        from engines.dpsnnn_engine import fetch_dpsnnn_slots

        return fetch_dpsnnn_slots(
            branch_id,
            theme_id,
            date_str,
            site_config.get("engine_options", {}),
            timeout,
        )
        
    return []
