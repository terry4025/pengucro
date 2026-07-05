import asyncio
import aiohttp
import requests
import json
import re
import time
import urllib.parse
from bs4 import BeautifulSoup
from engines.base_engine import BaseEngine

class DoomEscapeEngine(BaseEngine):
    THEME_ID_TO_NAME = {
        # 1호점
        "8": "Rendering",
        "27": "기담정",
        "28": "인앤아웃",
        "29": "나폴리탄",
        # 2호점
        "30": "운명",
        "31": "디스토피아",
        "32": "죄",
        "33": "인바이트",
        # DTH점(부평)
        "19": "슬래셔",
        "22": "트리거",
        "24": "언리얼",
        "25": "스네어",
        # FEAR점(수원)
        "34": "허수아비",
        "35": "옵스큐라",
        "36": "데이투어"
    }

    def __init__(self, site_url, log_callback, success_callback=None):
        """
        Doom Escape (Sinbiweb-based) Booking Engine.
        """
        super().__init__(log_callback, success_callback)
        self.site_url = site_url
        
        import threading
        self._log_lock = threading.Lock()
        self._notified_bypass = False
        self._notified_found = False
        self._last_err_msg = ""
        self._last_err_time = 0
        self._last_wait_time = 0

    def make_reservation_thread(self, reservation_data):
        session = requests.Session()
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
        }
        session.headers.update(headers)

        zizum_num = reservation_data.get("branch", "3")
        rev_days = reservation_data.get("reservationDate")
        theme_num = reservation_data.get("themePK")
        target_time = reservation_data.get("reservationTime")[:5]
        name = reservation_data.get("name")
        phone = reservation_data.get("phone", "")
        phone_digits = "".join(c for c in phone if c.isdigit())
        people = reservation_data.get("people", "3")

        # Split phone number into 3 parts
        if len(phone_digits) == 11:
            mobile1 = phone_digits[0:3]
            mobile2 = phone_digits[3:7]
            mobile3 = phone_digits[7:11]
        elif len(phone_digits) == 10:
            mobile1 = phone_digits[0:3]
            mobile2 = phone_digits[3:6]
            mobile3 = phone_digits[6:10]
        else:
            mobile1 = "010"
            mobile2 = "1234"
            mobile3 = "5678"

        theme_name = self.THEME_ID_TO_NAME.get(theme_num, "")

        list_url = f"https://doomescape.com/layout/res/home.php?go=rev.make&s_zizum={zizum_num}&rev_days={rev_days}"
        act_url = "https://doomescape.com/core/res/rev.act.php"

        self.log(f"🚀 둠이스케이프 동기 스레드 시작 (지점: {zizum_num}, 테마: {theme_name}({theme_num}), 날짜: {rev_days}, 시간: {target_time})", "info")

        while not self.stop_event.is_set():
            try:
                # 1. Fetch reservation page to list slots
                resp = session.get(list_url, timeout=5)
                if resp.status_code != 200:
                    self.silent_tick(f"조회 실패 ({resp.status_code})")
                    time.sleep(0.1)
                    continue

                html_text = resp.content.decode('cp949', errors='ignore')
                
                # Parse available slots
                soup = BeautifulSoup(html_text, 'html.parser')
                found_slot = None
                
                # Find matching theme box
                target_box = None
                for box in soup.find_all('div', class_='tm_box'):
                    name_p = box.find('p', class_='name')
                    if name_p and theme_name in name_p.text:
                        target_box = box
                        break
                
                if not target_box:
                    self.silent_tick("테마 박스를 찾을 수 없음")
                    time.sleep(0.1)
                    continue

                # Look for target time in theme box
                for a in target_box.find_all('a'):
                    num_span = a.find('span', class_='num')
                    txt_span = a.find('span', class_='txt')
                    if num_span and target_time in num_span.text:
                        if txt_span and "예약마감" in txt_span.text:
                            continue
                        
                        href = a.get('href', '')
                        match = re.search(r"theme_time_num=(\d+)", href)
                        if match:
                            found_slot = match.group(1)
                            break

                if not found_slot:
                    self.silent_tick(f"아직 열리지 않은 시간대 ({target_time}) 재시도 중")
                    now = time.time()
                    show_err = False
                    with self._log_lock:
                        if not hasattr(self, "_last_time_err_time") or (now - self._last_time_err_time) > 2.0:
                            self._last_time_err_time = now
                            show_err = True
                    if show_err:
                        self.log(f"아직 열리지 않은 시간대 ({target_time}) 재시도 중", "warning")
                    time.sleep(0.1)
                    continue

                slot_id = found_slot
                show_found = False
                with self._log_lock:
                    if not self._notified_found:
                        self._notified_found = True
                        show_found = True
                if show_found:
                    self.log(f"⏰ {target_time} 슬롯 발견! ID: {slot_id}. 예약 제출 진행 중...", "info")

                if self.stop_event.is_set():
                    break

                lock_acquired = False
                if hasattr(self, "submission_lock"):
                    lock_acquired = self.submission_lock.acquire(block=False)
                
                if hasattr(self, "submission_lock") and not lock_acquired:
                    time.sleep(0.1)
                    continue

                try:
                    if self.stop_event.is_set():
                        break

                    # 2. Visit input page to extract prices dynamically
                    input_url = f"https://doomescape.com/layout/res/home.php?go=rev.make.input&rev_days={rev_days}&theme_time_num={slot_id}"
                    resp_input = session.get(input_url, timeout=5)
                    input_html = resp_input.content.decode('cp949', errors='ignore')

                    price_fields = {}
                    # Extract hidden price fields
                    for p_inp in re.findall(r'<input[^>]*type=["\']?hidden["\']?[^>]*>', input_html, re.I):
                        name_m = re.search(r'name=["\']?(price\d*)["\']?', p_inp, re.I)
                        val_m = re.search(r'value=["\']?(\d+)["\']?', p_inp, re.I)
                        if name_m and val_m:
                            price_fields[name_m.group(1)] = val_m.group(1)

                    # Default fallback prices if scraping fails
                    base_price = price_fields.get("price", "126000")
                    price1 = price_fields.get("price1", base_price)
                    price2 = price_fields.get("price2", base_price)
                    price3 = price_fields.get("price3", base_price)
                    price4 = price_fields.get("price4", "168000")
                    price5 = price_fields.get("price5", "210000")
                    price6 = price_fields.get("price6", "252000")

                    # Use selected person price
                    actual_price = price_fields.get(f"price{people}", base_price)

                    # 3. Post to rev.act.php (UTF-8 encoding required!)
                    act_data = {
                        "name": name,
                        "mobile1": mobile1,
                        "mobile2": mobile2,
                        "mobile3": mobile3,
                        "person": people,
                        "ck_agree": "on",
                        "rev_days": rev_days,
                        "theme_time_num": slot_id,
                        "price": actual_price,
                        "price1": price1,
                        "price2": price2,
                        "price3": price3,
                        "price4": price4,
                        "price5": price5,
                        "price6": price6,
                        "act": "make",
                        "layout_folder": "layout/res"
                    }

                    encoded_pairs = []
                    for k, v in act_data.items():
                        encoded_pairs.append((k.encode('utf-8'), v.encode('utf-8')))
                    post_data = urllib.parse.urlencode(encoded_pairs).encode()

                    post_headers = {
                        "Content-Type": "application/x-www-form-urlencoded",
                        "Referer": input_url,
                        "Origin": "https://doomescape.com"
                    }

                    act_resp = session.post(act_url, data=post_data, headers=post_headers, timeout=8)
                    act_text = act_resp.content.decode('utf-8', errors='ignore')

                    # Parse response to find num
                    num_m = re.search(r"num=(\d+)", act_text)
                    if num_m:
                        num = num_m.group(1)
                        kcp_url = f"https://doomescape.com/layout/res/home.php?go=rev.kcp&num={num}"
                        
                        # 4. Fetch KCP page to extract ck_code
                        kcp_resp = session.get(kcp_url, timeout=8)
                        kcp_text = kcp_resp.content.decode('cp949', errors='ignore')
                        
                        ck_m = re.search(r"name=['\"]?ck_code['\"]?\s*value=['\"]?([^'\"'>\s]+)", kcp_text)
                        ck_code_val = ck_m.group(1) if ck_m else ""

                        # 5. Submit mutong.php (using GET or POST - GET is browser default)
                        mutong_url = "https://doomescape.com/core/res/rev.make.mutong.php"
                        mutong_params = {
                            "num": num,
                            "ck_code": ck_code_val,
                            "layout_folder": "layout/res",
                            "payment": "D"  # 'D' is mutong for Doom Escape
                        }
                        
                        # Request using GET
                        query_str = urllib.parse.urlencode(mutong_params)
                        mutong_get_url = f"{mutong_url}?{query_str}"
                        mutong_resp = session.get(mutong_get_url, timeout=8)
                        mutong_text = mutong_resp.content.decode('utf-8', errors='ignore')

                        # Follow meta refresh -> rev.make.exe.php
                        refresh_m = re.search(r"url=([^'\"\>]+)", mutong_text, re.I)
                        if refresh_m:
                            next_url = refresh_m.group(1).strip()
                            if not next_url.startswith("http"):
                                next_url = urllib.parse.urljoin(mutong_url, next_url)
                            try:
                                exe_resp = session.get(next_url, timeout=8)
                                exe_text = exe_resp.content.decode('utf-8', errors='ignore')
                                mutong_text += exe_text
                            except Exception:
                                pass

                        # Extract final booking number (ck_code)
                        bnum_m = re.search(r"ck_code=(\d+)", mutong_text)
                        bnum = bnum_m.group(1) if bnum_m else ck_code_val

                        if "rev.make.exe.php" in mutong_text or "rev.make.end" in mutong_text or "완료" in mutong_text or "성공" in mutong_text:
                            final_msg = f"예약 최종 완료! 예약번호: {bnum}"
                            try:
                                import webbrowser
                                webbrowser.open(f"https://doomescape.com/layout/res/home.php?go=rev.make.end&num={num}&ck_code={bnum}")
                            except Exception:
                                pass
                            try:
                                time_str = time.strftime('%Y-%m-%d %H:%M:%S', time.localtime())
                                with open("success_reservations.txt", "a", encoding="utf-8") as sf:
                                    sf.write(f"[{time_str}] 둠이스케이프 | 날짜: {rev_days} | 시간: {target_time} | 이름: {name} | 예약번호: {bnum}\n")
                            except Exception:
                                pass
                        else:
                            # Save response for debug
                            try:
                                import os
                                os.makedirs("scratch", exist_ok=True)
                                with open("scratch/last_mutong_response.html", "w", encoding="utf-8") as debug_mf:
                                    debug_mf.write(mutong_text)
                            except Exception:
                                pass
                            final_msg = f"예약 선점 성공! 예약번호: {bnum} / 임시번호: {num} (결제확인 응답 재확인 필요)"
                    else:
                        err_msg = "선점 실패"
                        alert_match = re.search(r"alert\s*\(\s*['\"](.*?)['\"]\s*\)", act_text)
                        if alert_match:
                            err_msg = alert_match.group(1)
                        raise Exception(err_msg)

                    self.log(f"🎉 {final_msg}", "success")
                    self.stop_event.set()
                    if self.success_callback:
                        self.success_callback()
                    break

                finally:
                    if hasattr(self, "submission_lock"):
                        try:
                            self.submission_lock.release()
                        except RuntimeError:
                            pass

            except Exception as e:
                err_str = str(e)
                now = time.time()
                show_log = False
                with self._log_lock:
                    if err_str != self._last_err_msg or (now - self._last_err_time) > 3.0:
                        self._last_err_msg = err_str
                        self._last_err_time = now
                        show_log = True
                if show_log:
                    self.log(f"오류 발생: {err_str}", "error")
                time.sleep(0.1)

    async def make_reservation_async_task(self, session_pool, reservation_data, task_idx):
        """
        Doom Escape Asynchronous Booking Path
        """
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
        }
        
        zizum_num = reservation_data.get("branch", "3")
        rev_days = reservation_data.get("reservationDate")
        theme_num = reservation_data.get("themePK")
        target_time = reservation_data.get("reservationTime")[:5]
        name = reservation_data.get("name")
        phone = reservation_data.get("phone", "")
        phone_digits = "".join(c for c in phone if c.isdigit())
        people = reservation_data.get("people", "3")

        # Split phone number into 3 parts
        if len(phone_digits) == 11:
            mobile1 = phone_digits[0:3]
            mobile2 = phone_digits[3:7]
            mobile3 = phone_digits[7:11]
        elif len(phone_digits) == 10:
            mobile1 = phone_digits[0:3]
            mobile2 = phone_digits[3:6]
            mobile3 = phone_digits[6:10]
        else:
            mobile1 = "010"
            mobile2 = "1234"
            mobile3 = "5678"

        theme_name = self.THEME_ID_TO_NAME.get(theme_num, "")
        
        list_url = f"https://doomescape.com/layout/res/home.php?go=rev.make&s_zizum={zizum_num}&rev_days={rev_days}"
        act_url = "https://doomescape.com/core/res/rev.act.php"

        session = session_pool[task_idx % len(session_pool)]
        self.log(f"[태스크 {task_idx+1}] 둠이스케이프 비동기 테스크 시작", "info")

        while not self.stop_event.is_set():
            try:
                # 1. Fetch reservation page to list slots
                async with session.get(list_url, timeout=5) as resp:
                    if resp.status != 200:
                        self.silent_tick(f"[태스크 {task_idx+1}] 시간 조회 오류 ({resp.status})")
                        await asyncio.sleep(0.1)
                        continue

                    # Read raw bytes and decode as CP949
                    html_bytes = await resp.read()
                    html_text = html_bytes.decode('cp949', errors='ignore')

                # Parse available slots
                soup = BeautifulSoup(html_text, 'html.parser')
                found_slot = None
                
                # Find matching theme box
                target_box = None
                for box in soup.find_all('div', class_='tm_box'):
                    name_p = box.find('p', class_='name')
                    if name_p and theme_name in name_p.text:
                        target_box = box
                        break
                
                if not target_box:
                    self.silent_tick(f"[태스크 {task_idx+1}] 테마 박스를 찾을 수 없음")
                    await asyncio.sleep(0.1)
                    continue

                # Look for target time in theme box
                for a in target_box.find_all('a'):
                    num_span = a.find('span', class_='num')
                    txt_span = a.find('span', class_='txt')
                    if num_span and target_time in num_span.text:
                        if txt_span and "예약마감" in txt_span.text:
                            continue
                        
                        href = a.get('href', '')
                        match = re.search(r"theme_time_num=(\d+)", href)
                        if match:
                            found_slot = match.group(1)
                            break

                if not found_slot:
                    self.silent_tick(f"[태스크 {task_idx+1}] 아직 열리지 않은 시간대 ({target_time}) 재시도 중")
                    now = time.time()
                    show_err = False
                    with self._log_lock:
                        if not hasattr(self, "_last_time_err_time") or (now - self._last_time_err_time) > 2.0:
                            self._last_time_err_time = now
                            show_err = True
                    if show_err:
                        self.log(f"[태스크 {task_idx+1}] 아직 열리지 않은 시간대 ({target_time}) 재시도 중", "warning")
                    await asyncio.sleep(0.1)
                    continue

                slot_id = found_slot
                show_found = False
                with self._log_lock:
                    if not self._notified_found:
                        self._notified_found = True
                        show_found = True
                if show_found:
                    self.log(f"⏰ [태스크 {task_idx+1}] {target_time} 슬롯 발견! ID: {slot_id}. 예약 제출 진행 중...", "info")

                if self.stop_event.is_set():
                    break

                lock_acquired = False
                if hasattr(self, "submission_lock"):
                    lock_acquired = self.submission_lock.acquire(block=False)
                
                if hasattr(self, "submission_lock") and not lock_acquired:
                    await asyncio.sleep(0.1)
                    continue

                try:
                    if self.stop_event.is_set():
                        break

                    # 2. Visit input page to extract prices dynamically
                    input_url = f"https://doomescape.com/layout/res/home.php?go=rev.make.input&rev_days={rev_days}&theme_time_num={slot_id}"
                    async with session.get(input_url, timeout=5) as resp_input:
                        input_bytes = await resp_input.read()
                        input_html = input_bytes.decode('cp949', errors='ignore')

                    price_fields = {}
                    # Extract hidden price fields
                    for p_inp in re.findall(r'<input[^>]*type=["\']?hidden["\']?[^>]*>', input_html, re.I):
                        name_m = re.search(r'name=["\']?(price\d*)["\']?', p_inp, re.I)
                        val_m = re.search(r'value=["\']?(\d+)["\']?', p_inp, re.I)
                        if name_m and val_m:
                            price_fields[name_m.group(1)] = val_m.group(1)

                    # Default fallback prices if scraping fails
                    base_price = price_fields.get("price", "126000")
                    price1 = price_fields.get("price1", base_price)
                    price2 = price_fields.get("price2", base_price)
                    price3 = price_fields.get("price3", base_price)
                    price4 = price_fields.get("price4", "168000")
                    price5 = price_fields.get("price5", "210000")
                    price6 = price_fields.get("price6", "252000")

                    # Use selected person price
                    actual_price = price_fields.get(f"price{people}", base_price)

                    # 3. Post to rev.act.php (UTF-8 encoding required!)
                    act_data = {
                        "name": name,
                        "mobile1": mobile1,
                        "mobile2": mobile2,
                        "mobile3": mobile3,
                        "person": people,
                        "ck_agree": "on",
                        "rev_days": rev_days,
                        "theme_time_num": slot_id,
                        "price": actual_price,
                        "price1": price1,
                        "price2": price2,
                        "price3": price3,
                        "price4": price4,
                        "price5": price5,
                        "price6": price6,
                        "act": "make",
                        "layout_folder": "layout/res"
                    }

                    encoded_pairs = []
                    for k, v in act_data.items():
                        encoded_pairs.append((k.encode('utf-8'), v.encode('utf-8')))
                    post_data = urllib.parse.urlencode(encoded_pairs).encode()

                    post_headers = {
                        "Content-Type": "application/x-www-form-urlencoded",
                        "Referer": input_url,
                        "Origin": "https://doomescape.com",
                        "User-Agent": headers["User-Agent"]
                    }

                    async with session.post(act_url, data=post_data, headers=post_headers, timeout=8) as act_resp:
                        act_bytes = await act_resp.read()
                        act_text = act_bytes.decode('utf-8', errors='ignore')

                    # Parse response to find num
                    num_m = re.search(r"num=(\d+)", act_text)
                    if num_m:
                        num = num_m.group(1)
                        kcp_url = f"https://doomescape.com/layout/res/home.php?go=rev.kcp&num={num}"
                        
                        # 4. Fetch KCP page to extract ck_code
                        async with session.get(kcp_url, headers=headers, timeout=8) as kcp_resp:
                            kcp_text = await kcp_resp.text(encoding='cp949', errors='ignore')
                        
                        ck_m = re.search(r"name=['\"]?ck_code['\"]?\s*value=['\"]?([^'\"'>\s]+)", kcp_text)
                        ck_code_val = ck_m.group(1) if ck_m else ""

                        # 5. Submit mutong.php (using GET or POST - GET is browser default)
                        mutong_url = "https://doomescape.com/core/res/rev.make.mutong.php"
                        mutong_params = {
                            "num": num,
                            "ck_code": ck_code_val,
                            "layout_folder": "layout/res",
                            "payment": "D"  # 'D' is mutong for Doom Escape
                        }
                        
                        query_str = urllib.parse.urlencode(mutong_params)
                        mutong_get_url = f"{mutong_url}?{query_str}"
                        
                        async with session.get(mutong_get_url, headers=headers, timeout=8) as mutong_resp:
                            mutong_bytes = await mutong_resp.read()
                            mutong_text = mutong_bytes.decode('utf-8', errors='ignore')

                        # Follow meta refresh -> rev.make.exe.php
                        refresh_m = re.search(r"url=([^'\"\>]+)", mutong_text, re.I)
                        if refresh_m:
                            next_url = refresh_m.group(1).strip()
                            if not next_url.startswith("http"):
                                next_url = urllib.parse.urljoin(mutong_url, next_url)
                            try:
                                async with session.get(next_url, headers=headers, timeout=8) as exe_resp:
                                    exe_bytes = await exe_resp.read()
                                    exe_text = exe_bytes.decode('utf-8', errors='ignore')
                                    mutong_text += exe_text
                            except Exception:
                                pass

                        # Extract final booking number (ck_code)
                        bnum_m = re.search(r"ck_code=(\d+)", mutong_text)
                        bnum = bnum_m.group(1) if bnum_m else ck_code_val

                        if "rev.make.exe.php" in mutong_text or "rev.make.end" in mutong_text or "완료" in mutong_text or "성공" in mutong_text:
                            final_msg = f"[태스크 {task_idx+1}] 예약 최종 완료! 예약번호: {bnum}"
                            try:
                                import webbrowser
                                webbrowser.open(f"https://doomescape.com/layout/res/home.php?go=rev.make.end&num={num}&ck_code={bnum}")
                            except Exception:
                                pass
                            try:
                                time_str = time.strftime('%Y-%m-%d %H:%M:%S', time.localtime())
                                with open("success_reservations.txt", "a", encoding="utf-8") as sf:
                                    sf.write(f"[{time_str}] 둠이스케이프 | 날짜: {rev_days} | 시간: {target_time} | 이름: {name} | 예약번호: {bnum}\n")
                            except Exception:
                                pass
                        else:
                            # Save response for debug
                            try:
                                import os
                                os.makedirs("scratch", exist_ok=True)
                                with open("scratch/last_mutong_response.html", "w", encoding="utf-8") as debug_mf:
                                    debug_mf.write(mutong_text)
                            except Exception:
                                pass
                            final_msg = f"[태스크 {task_idx+1}] 예약 선점 성공! 예약번호: {bnum} / 임시번호: {num} (결제확인 응답 재확인 필요)"
                    else:
                        err_msg = "선점 실패"
                        alert_match = re.search(r"alert\s*\(\s*['\"](.*?)['\"]\s*\)", act_text)
                        if alert_match:
                            err_msg = alert_match.group(1)
                        raise Exception(err_msg)

                    self.log(f"🎉 {final_msg}", "success")
                    self.stop_event.set()
                    if self.success_callback:
                        self.success_callback()
                    break

                finally:
                    if hasattr(self, "submission_lock"):
                        try:
                            self.submission_lock.release()
                        except RuntimeError:
                            pass

            except Exception as e:
                err_str = str(e)
                now = time.time()
                show_log = False
                with self._log_lock:
                    if err_str != self._last_err_msg or (now - self._last_err_time) > 3.0:
                        self._last_err_msg = err_str
                        self._last_err_time = now
                        show_log = True
                if show_log:
                    self.log(f"[태스크 {task_idx+1}] 오류 발생: {err_str}", "error")
                await asyncio.sleep(0.1)
