from __future__ import annotations
import re
import urllib.parse
from typing import Any
import requests
from bs4 import BeautifulSoup
from engines.zeroworld_catalog import ZeroWorldTimeSlot, parse_time_slots, decode_body

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
    theme_map = KEYESCAPE_THEMES.get(str(branch_id), {})
    for t_name, t_val in theme_map.items():
        if isinstance(t_val, dict):
            if t_val.get("info_num") == theme_id:
                theme_num = t_val.get("theme_num", theme_id)
                break
        elif t_val == theme_id:
            break
            
    api_url = urllib.parse.urljoin(base_url, "/controller/run_proc.php")
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
        "Referer": f"{base_url.rstrip('/')}/reservation.php",
        "Origin": base_url.rstrip('/')
    }
    response = requests.post(
        api_url,
        data={
            't': 'get_theme_time',
            'date': date_str,
            'zizumNum': branch_id,
            'themeNum': theme_num,
            'endDay': '0'
        },
        headers=headers,
        timeout=timeout
    )
    response.raise_for_status()
    data = response.json()
    
    slots = []
    if data.get("status") and data.get("data"):
        for slot in data["data"]:
            slot_time = f"{int(slot.get('hh', 0)):02d}:{int(slot.get('mm', 0)):02d}"
            is_enabled = slot.get("enable") == "Y"
            slots.append(ZeroWorldTimeSlot(time=slot_time, slot_id=str(slot.get("num", "")), available=is_enabled))
    return sorted(slots, key=lambda s: s.time)


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
    try:
        slots = api.fetch_slots(date_str)
    except NaverApiError:
        return []
    finally:
        api.close()

    return sorted(
        (
            ZeroWorldTimeSlot(
                time=slot.time_str,
                slot_id=slot.slot_id,
                available=slot.is_open(),
            )
            for slot in slots
        ),
        key=lambda entry: entry.time,
    )


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

    url = urllib.parse.urljoin(base_url, "/layout/res/home.php")
    params = {
        "go": "rev.make",
        "s_zizum": branch_id,
        "rev_days": date_str
    }
    
    response = requests.get(
        url,
        params=params,
        headers={"User-Agent": "Mozilla/5.0"},
        timeout=timeout
    )
    response.raise_for_status()
    
    html = response.content.decode('utf-8', errors='ignore')
    soup = BeautifulSoup(html, "html.parser")
    
    slots = []
    target_box = None
    for box in soup.find_all('div', class_='tm_box'):
        name_p = box.find('p', class_='name')
        if name_p and (not theme_name or theme_name in name_p.text):
            target_box = box
            break
            
    boxes_to_parse = [target_box] if target_box else soup.find_all('div', class_='tm_box')
        
    for box in boxes_to_parse:
        for a in box.find_all('a'):
            num_span = a.find('span', class_='num')
            txt_span = a.find('span', class_='txt')
            if num_span:
                time_str = num_span.text.strip()
                time_match = re.search(r"(\d{2}:\d{2})", time_str)
                if time_match:
                    time_val = time_match.group(1)
                    if any(s.time == time_val for s in slots):
                        continue
                        
                    is_closed = txt_span and "예약마감" in txt_span.text
                    href = a.get('href', '')
                    match = re.search(r"theme_time_num=(\d+)", href)
                    slot_id = match.group(1) if match else time_val
                    
                    slots.append(ZeroWorldTimeSlot(time=time_val, slot_id=slot_id, available=not is_closed))
                    
    return sorted(slots, key=lambda s: s.time)


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
        
    return []
