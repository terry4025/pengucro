import asyncio
import aiohttp
import requests
import json
import re
import time
import urllib.parse
from bs4 import BeautifulSoup
from engines.base_engine import BaseEngine

class ZeroWorldShinEngine(BaseEngine):
    def __init__(self, site_url, log_callback, success_callback=None):
        """
        ZeroWorld New (Sinbiweb-based) Booking Engine.
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

        zizum_num = reservation_data.get("branch", "4")
        rev_days = reservation_data.get("reservationDate")
        theme_num = reservation_data.get("themePK")
        target_time = reservation_data.get("reservationTime")[:5]
        name = reservation_data.get("name")
        phone_digits = "".join(c for c in reservation_data.get("phone", "") if c.isdigit())
        if len(phone_digits) == 11:
            phone_formatted = f"{phone_digits[0:3]}-{phone_digits[3:7]}-{phone_digits[7:11]}"
        elif len(phone_digits) == 10:
            phone_formatted = f"{phone_digits[0:3]}-{phone_digits[3:6]}-{phone_digits[6:10]}"
        else:
            phone_formatted = phone_digits
        people = reservation_data.get("people", "2")
        s_subj = "B" if zizum_num == "2" else "A"

        sel_url = "https://zeroworldkorea.com/core/res/rev.make.sel.php"
        act_url = "https://zeroworldkorea.com/core/res/rev.act.php"

        post_headers = {
            "Content-Type": "application/x-www-form-urlencoded",
            "Referer": f"https://zeroworldkorea.com/layout/res/home.php?go=rev.make&s_subj={s_subj}&zizum_num={zizum_num}&rev_days={rev_days}"
        }

        # 요일 확인 (0~4: 평일, 5~6: 주말)
        is_weekend = False
        try:
            from datetime import datetime
            dt = datetime.strptime(rev_days, "%Y-%m-%d")
            if dt.weekday() in [5, 6]:
                is_weekend = True
        except Exception:
            pass

        weekday_map = {
            "10:50": "694", "12:00": "698", "13:10": "701", "14:20": "16",
            "15:30": "704", "16:40": "705", "17:50": "706", "19:00": "709",
            "20:10": "710", "21:20": "711"
        }
        weekend_map = {
            "10:50": "695", "12:00": "699", "13:10": "702", "14:20": "707",
            "15:30": "712", "16:40": "713", "17:50": "17", "19:00": "714",
            "20:10": "715", "21:20": "716"
        }
        fallback_map = weekend_map if is_weekend else weekday_map
        mapped_slot = fallback_map.get(target_time)

        self.log(f"신 제로월드 고속 감시 시작 (스레드): {target_time} (매핑 ID: {mapped_slot})", "info")

        slot_id = None
        is_date_opened = False
        while not self.stop_event.is_set():
            try:
                # 0. Check calendar if date is opened
                if not is_date_opened:
                    dt_parts = rev_days.split("-")
                    cal_payload = f"act=calendar&zizum_num={zizum_num}&rev_days={rev_days}&year={dt_parts[0]}&month={dt_parts[1]}&s_subj={s_subj}"
                    cal_resp = session.post(sel_url, data=cal_payload, headers=post_headers, timeout=5)
                    if cal_resp.status_code == 200 and f"fun_days_select('{rev_days}'" in cal_resp.text:
                        is_date_opened = True
                        self.log(f"📅 예약 날짜({rev_days}) 오픈 확인! 예약 진행합니다.", "info")
                    else:
                        now = time.time()
                        show_wait = False
                        with self._log_lock:
                            if (now - getattr(self, "_last_wait_time", 0)) > 2.0:
                                self._last_wait_time = now
                                show_wait = True
                        if show_wait:
                            self.log(f"⏳ 아직 열리지 않은 날짜 ({rev_days}) 대기 중... 계속 시도 중", "info")
                        self.silent_tick(f"아직 열리지 않은 날짜 ({rev_days}) 대기 중")
                        time.sleep(0.1)
                        continue

                # 1. Fetch time slots
                payload = f"act=theme_time_list&zizum_num={zizum_num}&rev_days={rev_days}&theme_num={theme_num}"
                resp = session.post(sel_url, data=payload, headers=post_headers, timeout=5)
                if resp.status_code != 200:
                    self.silent_tick(f"시간 조회 오류 ({resp.status_code})")
                    time.sleep(0.1)
                    continue

                html_text = resp.text
                if "에러" in html_text or not html_text.strip():
                    self.silent_tick(f"시간 데이터 없음")
                    time.sleep(0.1)
                    continue

                soup = BeautifulSoup(html_text, 'html.parser')
                found_slot = None
                target_found_disabled = False
                for a in soup.find_all('a'):
                    if target_time in a.text:
                        classes = a.get('class', [])
                        if any(c in classes for c in ['disable', 'close', 'sold-out']):
                            target_found_disabled = True
                            continue
                        
                        href = a.get('href', '')
                        match = re.search(r"fun_theme_time_select\('(\d+)'", href)
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

                    # 1.5 Select theme (registers selection in session)
                    theme_payload = f"act=theme_select&theme_num={theme_num}&rev_days={rev_days}&theme_time_num="
                    session.post(sel_url, data=theme_payload, headers=post_headers, timeout=5)

                    if self.stop_event.is_set():
                        break

                    # 2. Select slot
                    sel_payload = f"act=theme_time_select&theme_time_num={slot_id}"
                    session.post(sel_url, data=sel_payload, headers=post_headers, timeout=5)

                    if self.stop_event.is_set():
                        break

                    # 3. Finalize reservation
                    act_data = {
                        "name": name,
                        "mobile": phone_formatted,
                        "person": people,
                        "zizum_num": zizum_num,
                        "rev_days": rev_days,
                        "theme_num": theme_num,
                        "theme_time_num": slot_id,
                        "act": "make",
                        "s_subj": s_subj
                    }

                    act_resp = session.post(act_url, data=act_data, headers=post_headers, timeout=8)
                    try:
                        act_bytes = act_resp.content
                        try:
                            act_text = act_bytes.decode('utf-8')
                        except UnicodeDecodeError:
                            act_text = act_bytes.decode('cp949', errors='ignore')
                    except Exception:
                        act_text = act_resp.text

                    try:
                        import os
                        os.makedirs("scratch", exist_ok=True)
                        with open("scratch/last_act_response.html", "w", encoding="utf-8") as debug_f:
                            debug_f.write(act_text)
                    except Exception:
                        pass

                    history_urls = [str(h.url) for h in act_resp.history]
                    final_url = str(act_resp.url)

                    success = False
                    combined_check = act_text + " " + final_url + " " + " ".join(history_urls)
                    combined_lower = combined_check.lower()
                    if "rev.pay" in combined_lower or "rev.kcp" in combined_lower:
                        success = True
                    elif "location.replace" in combined_lower or "location.href" in combined_lower:
                        success = True
                    elif "결제" in act_text or "예약확인" in act_text or "payment" in combined_lower:
                        success = True
                    elif "toss" in combined_lower or "vbank" in combined_lower:
                        success = True

                    if success:
                        if self.stop_event.is_set():
                            break
                        final_msg = "예약 선점 성공 (수동 확인 필요)"
                        try:
                            code_m = re.search(r"name=['\"]?code['\"]?\s*value=['\"]?([^'\"'>\s]+)", act_text)
                            ck_m = re.search(r"name=['\"]?ck_code['\"]?\s*value=['\"]?([^'\"'>\s]+)", act_text)
                            rev_code = code_m.group(1) if code_m else ""
                            ck_code_val = ck_m.group(1) if ck_m else ""
                            if not rev_code:
                                url_m = re.search(r"code=([a-zA-Z0-9]+)", combined_check)
                                if url_m:
                                    rev_code = url_m.group(1)
                            if rev_code:
                                if not ck_code_val:
                                    kcp_url = f"https://zeroworldkorea.com/layout/res/home.php?go=rev.kcp&code={rev_code}"
                                    kcp_resp = session.get(kcp_url, timeout=8)
                                    kcp_text = kcp_resp.text
                                    ck_m2 = re.search(r"name=['\"]?ck_code['\"]?\s*value=['\"]?([^'\"'>\s]+)", kcp_text)
                                    if ck_m2:
                                        ck_code_val = ck_m2.group(1)
                                mutong_url = "https://zeroworldkorea.com/core/res/rev.make.mutong.php"
                                mutong_params = {
                                    "code": rev_code,
                                    "ck_code": ck_code_val,
                                    "layout_folder": "layout/res",
                                    "payment": "A",
                                    "privacy": "on",
                                    "name": name,
                                    "mobile": phone_formatted,
                                    "tel": phone_formatted
                                }
                                mutong_resp = session.post(mutong_url, data=mutong_params, timeout=8)
                                try:
                                    mutong_bytes = mutong_resp.content
                                    try:
                                        mutong_text = mutong_bytes.decode('utf-8')
                                    except UnicodeDecodeError:
                                        mutong_text = mutong_bytes.decode('cp949', errors='ignore')
                                except Exception:
                                    mutong_text = mutong_resp.text

                                # mutong.php returns meta refresh -> rev.make.exe.php
                                # rev.make.exe.php triggers KakaoTalk notification & returns meta refresh -> rev.make.end
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

                                # Extract booking number from ck_code in URL (most reliable)
                                bnum_m = re.search(r"ck_code=(\d+)", mutong_text)
                                if not bnum_m:
                                    bnum_m = re.search(r"예약번호[^0-9]*(\d+)", mutong_text)
                                bnum = bnum_m.group(1) if bnum_m else ck_code_val

                                if "rev.make.exe.php" in mutong_text or "rev.make.end" in mutong_text or "완료" in mutong_text or "접수" in mutong_text or "성공" in mutong_text:
                                    final_msg = f"예약 최종 완료! 예약번호: {bnum}"
                                    try:
                                        import webbrowser
                                        webbrowser.open(f"https://zeroworldkorea.com/layout/res/home.php?go=rev.make.end&code={rev_code}")
                                    except Exception:
                                        pass
                                    try:
                                        time_str = time.strftime('%Y-%m-%d %H:%M:%S', time.localtime())
                                        with open("success_reservations.txt", "a", encoding="utf-8") as sf:
                                            sf.write(f"[{time_str}] 제로월드(신) | 날짜: {rev_days} | 시간: {target_time} | 이름: {name} | 예약번호: {bnum}\n")
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
                                    final_msg = f"예약 선점 성공! 예약번호: {bnum} / 코드: {rev_code} (결제확인 응답 재확인 필요)"
                            else:
                                final_msg = "예약 선점 성공! (코드 추출 실패 - 수동 확인 필요)"
                        except Exception as e:
                            final_msg = f"예약 선점 성공! (자동확인 오류: {e})"
                        self.log(f"🎉 {final_msg}", "success")
                        self.stop_event.set()
                        if self.success_callback:
                            self.success_callback()
                        break
                    else:
                        err_msg = "선점 실패"
                        alert_match = re.search(r"alert\s*\(\s*['\"](.*?)['\"]\s*\)", act_text)
                        if alert_match:
                            err_msg = alert_match.group(1)
                        else:
                            err_soup = BeautifulSoup(act_text, 'html.parser')
                            for s in err_soup.find_all('script'):
                                if "alert" in s.text:
                                    inner_match = re.search(r"alert\(['\"](.*?)['\"]\)", s.text)
                                    if inner_match:
                                        err_msg = inner_match.group(1)
                                        break
                        
                        show_err = False
                        now = time.time()
                        with self._log_lock:
                            if err_msg != self._last_err_msg or (now - self._last_err_time) > 2.0:
                                self._last_err_msg = err_msg
                                self._last_err_time = now
                                show_err = True
                        if show_err:
                            self.log(f"제출 대기 중: {err_msg}", "warning")
                        
                        time.sleep(0.5)
                finally:
                    if lock_acquired and hasattr(self, "submission_lock"):
                        try:
                            self.submission_lock.release()
                        except Exception:
                            pass

            except Exception as e:
                now = time.time()
                show_conn_err = False
                with self._log_lock:
                    if "connection_error" != self._last_err_msg or (now - self._last_err_time) > 2.0:
                        self._last_err_msg = "connection_error"
                        self._last_err_time = now
                        show_conn_err = True
                if show_conn_err:
                    self.log(f"통신 에러 발생: {e} - 세션 재연결 시도", "warning")
                try:
                    session.close()
                except Exception:
                    pass
                session = requests.Session()
                session.headers.update(headers)
                time.sleep(0.5)

    async def make_reservation_async_task(self, reservation_data, task_idx):
        session = None
        if hasattr(self, "session_pool") and len(self.session_pool) > 0:
            local_idx = task_idx % len(self.session_pool)
            session, _ = self.session_pool[local_idx]
            
        if not session:
            headers = {
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
            }
            session = aiohttp.ClientSession(headers=headers)
            
        zizum_num = reservation_data.get("branch", "4")
        rev_days = reservation_data.get("reservationDate")
        theme_num = reservation_data.get("themePK")
        target_time = reservation_data.get("reservationTime")[:5]
        name = reservation_data.get("name")
        phone_digits = "".join(c for c in reservation_data.get("phone", "") if c.isdigit())
        if len(phone_digits) == 11:
            phone_formatted = f"{phone_digits[0:3]}-{phone_digits[3:7]}-{phone_digits[7:11]}"
        elif len(phone_digits) == 10:
            phone_formatted = f"{phone_digits[0:3]}-{phone_digits[3:6]}-{phone_digits[6:10]}"
        else:
            phone_formatted = phone_digits
        people = reservation_data.get("people", "2")
        s_subj = "B" if zizum_num == "2" else "A"
        
        sel_url = "https://zeroworldkorea.com/core/res/rev.make.sel.php"
        act_url = "https://zeroworldkorea.com/core/res/rev.act.php"
        
        headers = {
            "Content-Type": "application/x-www-form-urlencoded",
            "Referer": f"https://zeroworldkorea.com/layout/res/home.php?go=rev.make&s_subj={s_subj}&zizum_num={zizum_num}&rev_days={rev_days}"
        }
        
        is_weekend = False
        try:
            from datetime import datetime
            dt = datetime.strptime(rev_days, "%Y-%m-%d")
            if dt.weekday() in [5, 6]:
                is_weekend = True
        except Exception:
            pass

        weekday_map = {
            "10:50": "694", "12:00": "698", "13:10": "701", "14:20": "16",
            "15:30": "704", "16:40": "705", "17:50": "706", "19:00": "709",
            "20:10": "710", "21:20": "711"
        }
        weekend_map = {
            "10:50": "695", "12:00": "699", "13:10": "702", "14:20": "707",
            "15:30": "712", "16:40": "713", "17:50": "17", "19:00": "714",
            "20:10": "715", "21:20": "716"
        }
        fallback_map = weekend_map if is_weekend else weekday_map
        mapped_slot = fallback_map.get(target_time)
        
        self.log(f"[태스크 {task_idx+1}] 신 제로월드 고속 감시 시작: {target_time} (매핑 ID: {mapped_slot})", "info")
        
        slot_id = None
        is_date_opened = False
        while not self.stop_event.is_set():
            try:
                # 0. Check calendar if date is opened
                if not is_date_opened:
                    dt_parts = rev_days.split("-")
                    cal_payload = f"act=calendar&zizum_num={zizum_num}&rev_days={rev_days}&year={dt_parts[0]}&month={dt_parts[1]}&s_subj={s_subj}"
                    async with session.post(sel_url, data=cal_payload, headers=headers, timeout=5) as cal_resp:
                        if cal_resp.status == 200:
                            cal_text = await cal_resp.text()
                            if f"fun_days_select('{rev_days}'" in cal_text:
                                is_date_opened = True
                                self.log(f"[태스크 {task_idx+1}] 📅 예약 날짜({rev_days}) 오픈 확인! 예약 진행합니다.", "info")
                            else:
                                now = time.time()
                                show_wait = False
                                with self._log_lock:
                                    if (now - getattr(self, "_last_wait_time", 0)) > 2.0:
                                        self._last_wait_time = now
                                        show_wait = True
                                if show_wait:
                                    self.log(f"⏳ [태스크 {task_idx+1}] 아직 열리지 않은 날짜 ({rev_days}) 대기 중... 계속 시도 중", "info")
                                self.silent_tick(f"아직 열리지 않은 날짜 ({rev_days}) 대기 중")
                                await asyncio.sleep(0.1)
                                continue
                        else:
                            self.silent_tick("캘린더 조회 API 에러")
                            await asyncio.sleep(0.1)
                            continue

                # 1. Fetch time slots
                payload = f"act=theme_time_list&zizum_num={zizum_num}&rev_days={rev_days}&theme_num={theme_num}"
                async with session.post(sel_url, data=payload, headers=headers, timeout=5) as resp:
                    if resp.status != 200:
                        self.silent_tick(f"시간 조회 오류 ({resp.status})")
                        await asyncio.sleep(0.1)
                        continue
                        
                    html_text = await resp.text()
                    if "에러" in html_text or not html_text.strip():
                        self.silent_tick(f"시간 데이터 없음")
                        await asyncio.sleep(0.1)
                        continue
                        
                    soup = BeautifulSoup(html_text, 'html.parser')
                    found_slot = None
                    target_found_disabled = False
                    for a in soup.find_all('a'):
                        if target_time in a.text:
                            classes = a.get('class', [])
                            if any(c in classes for c in ['disable', 'close', 'sold-out']):
                                target_found_disabled = True
                                continue
                            
                            href = a.get('href', '')
                            match = re.search(r"fun_theme_time_select\('(\d+)'", href)
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
                        await asyncio.sleep(0.1)
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

                # Try to acquire submission lock so only one task per process submits at a time
                lock_acquired = False
                if hasattr(self, "submission_lock"):
                    # multiprocess Lock acquire
                    lock_acquired = self.submission_lock.acquire(block=False)
                
                if hasattr(self, "submission_lock") and not lock_acquired:
                    # Another process is submitting, just wait
                    await asyncio.sleep(0.1)
                    continue

                try:
                    # 1.5 Select theme (registers selection in session)
                    theme_payload = f"act=theme_select&theme_num={theme_num}&rev_days={rev_days}&theme_time_num="
                    async with session.post(sel_url, data=theme_payload, headers=headers, timeout=5):
                        pass

                    if self.stop_event.is_set():
                        break

                    # 2. Select slot
                    sel_payload = f"act=theme_time_select&theme_time_num={slot_id}"
                    async with session.post(sel_url, data=sel_payload, headers=headers, timeout=5):
                        pass
                    
                    if self.stop_event.is_set():
                        break

                    # 3. Finalize reservation
                    act_data = {
                        "name": name,
                        "mobile": phone_formatted,
                        "person": people,
                        "zizum_num": zizum_num,
                        "rev_days": rev_days,
                        "theme_num": theme_num,
                        "theme_time_num": slot_id,
                        "act": "make",
                        "s_subj": s_subj
                    }
                    
                    async with session.post(act_url, data=act_data, headers=headers, timeout=8) as act_resp:
                        try:
                            act_bytes = await act_resp.read()
                            try:
                                act_text = act_bytes.decode('utf-8')
                            except UnicodeDecodeError:
                                act_text = act_bytes.decode('cp949', errors='ignore')
                        except Exception:
                            act_text = await act_resp.text()

                        try:
                            import os
                            os.makedirs("scratch", exist_ok=True)
                            with open("scratch/last_act_response.html", "w", encoding="utf-8") as debug_f:
                                debug_f.write(act_text)
                        except Exception:
                            pass
                        
                        history_urls = [str(h.url) for h in act_resp.history]
                        final_url = str(act_resp.url)
                        
                        success = False
                        combined_check = act_text + " " + final_url + " " + " ".join(history_urls)
                        combined_lower = combined_check.lower()
                        if "rev.pay" in combined_lower or "rev.kcp" in combined_lower:
                            success = True
                        elif "location.replace" in combined_lower or "location.href" in combined_lower:
                            success = True
                        elif "결제" in act_text or "예약확인" in act_text or "payment" in combined_lower:
                            success = True
                        elif "toss" in combined_lower or "vbank" in combined_lower:
                            success = True
                            
                        if success:
                            if self.stop_event.is_set():
                                break
                            final_msg = f"[태스크 {task_idx+1}] 예약 선점 성공 (수동 확인 필요)"
                            try:
                                code_m = re.search(r"name=['\"]?code['\"]?\s*value=['\"]?([^'\"'>\s]+)", act_text)
                                ck_m = re.search(r"name=['\"]?ck_code['\"]?\s*value=['\"]?([^'\"'>\s]+)", act_text)
                                rev_code = code_m.group(1) if code_m else ""
                                ck_code_val = ck_m.group(1) if ck_m else ""
                                if not rev_code:
                                    url_m = re.search(r"code=([a-zA-Z0-9]+)", combined_check)
                                    if url_m:
                                        rev_code = url_m.group(1)
                                if rev_code:
                                    if not ck_code_val:
                                        kcp_url = f"https://zeroworldkorea.com/layout/res/home.php?go=rev.kcp&code={rev_code}"
                                        async with session.get(kcp_url, timeout=8) as kcp_resp:
                                            kcp_text = await kcp_resp.text()
                                            ck_m2 = re.search(r"name=['\"]?ck_code['\"]?\s*value=['\"]?([^'\"'>\s]+)", kcp_text)
                                            if ck_m2:
                                                ck_code_val = ck_m2.group(1)
                                    mutong_url = "https://zeroworldkorea.com/core/res/rev.make.mutong.php"
                                    mutong_params = {
                                        "code": rev_code,
                                        "ck_code": ck_code_val,
                                        "layout_folder": "layout/res",
                                        "payment": "A",
                                        "privacy": "on",
                                        "name": name,
                                        "mobile": phone_formatted,
                                        "tel": phone_formatted
                                    }
                                    
                                    # The original code used POST. Let's stick to POST with dict to ensure proper UTF-8 urlencoding
                                    async with session.post(mutong_url, data=mutong_params, headers=headers, timeout=8) as mutong_resp:
                                        try:
                                            mutong_bytes = await mutong_resp.read()
                                            try:
                                                mutong_text = mutong_bytes.decode('utf-8')
                                            except UnicodeDecodeError:
                                                mutong_text = mutong_bytes.decode('cp949', errors='ignore')
                                        except Exception:
                                            mutong_text = await mutong_resp.text()
    
                                        # mutong.php returns a meta refresh to rev.make.exe.php
                                        refresh_m = re.search(r"url=([^'\">]+)", mutong_text, re.I)
                                        if refresh_m:
                                            next_url = refresh_m.group(1).strip()
                                            # e.g., next_url = rev.make.exe.php?code=...&ck_code=...
                                            if not next_url.startswith("http"):
                                                next_url = urllib.parse.urljoin(mutong_url, next_url)
                                            # GET request to rev.make.exe.php
                                            async with session.get(next_url, headers=headers, timeout=8) as exe_resp:
                                                try:
                                                    exe_bytes = await exe_resp.read()
                                                    exe_text = exe_bytes.decode('utf-8', errors='ignore')
                                                    mutong_text += exe_text
                                                except Exception:
                                                    pass

                                        bnum_m = re.search(r"ck_code=([0-9]+)", mutong_text)
                                        if not bnum_m:
                                            bnum_m = re.search(r"예약번호[^0-9]*(\d+)", mutong_text)
                                        
                                        bnum = bnum_m.group(1) if bnum_m else ck_code_val
                                        if "rev.make.exe.php" in mutong_text or "rev.make.end" in mutong_text or "완료" in mutong_text or "접수" in mutong_text or "성공" in mutong_text:
                                            final_msg = f"[태스크 {task_idx+1}] 예약 최종 완료! 예약번호: {bnum}"
                                            try:
                                                import webbrowser
                                                webbrowser.open(f"https://zeroworldkorea.com/layout/res/home.php?go=rev.make.end&code={rev_code}")
                                            except Exception:
                                                pass
                                            try:
                                                time_str = time.strftime('%Y-%m-%d %H:%M:%S', time.localtime())
                                                with open("success_reservations.txt", "a", encoding="utf-8") as sf:
                                                    sf.write(f"[{time_str}] 제로월드(신) | 날짜: {rev_days} | 시간: {target_time} | 이름: {name} | 예약번호: {bnum}\n")
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
                                            final_msg = f"[태스크 {task_idx+1}] 예약 선점 성공! 예약번호: {bnum} / 코드: {rev_code} (결제확인 응답 재확인 필요)"
                                else:
                                    final_msg = f"[태스크 {task_idx+1}] 예약 선점 성공! (코드 추출 실패 - 수동 확인 필요)"
                            except Exception as e:
                                final_msg = f"[태스크 {task_idx+1}] 예약 선점 성공! (자동확인 오류: {e})"
                            self.log(f"🎉 {final_msg}", "success")
                            self.stop_event.set()
                            if self.success_callback:
                                self.success_callback()
                            break
                        else:
                            err_msg = "선점 실패"
                            alert_match = re.search(r"alert\s*\(\s*['\"](.*?)['\"]\s*\)", act_text)
                            if alert_match:
                                err_msg = alert_match.group(1)
                            else:
                                err_soup = BeautifulSoup(act_text, 'html.parser')
                                for s in err_soup.find_all('script'):
                                    if "alert" in s.text:
                                        inner_match = re.search(r"alert\(['\"](.*?)['\"]\)", s.text)
                                        if inner_match:
                                            err_msg = inner_match.group(1)
                                            break
                                        
                            show_err = False
                            now = time.time()
                            with self._log_lock:
                                if err_msg != self._last_err_msg or (now - self._last_err_time) > 2.0:
                                    self._last_err_msg = err_msg
                                    self._last_err_time = now
                                    show_err = True
                            if show_err and not self.stop_event.is_set():
                                self.log(f"제출 대기 중: {err_msg}", "warning")
                                
                            if self.stop_event.is_set():
                                break
                            await asyncio.sleep(0.5)
                finally:
                    if lock_acquired and hasattr(self, "submission_lock"):
                        try:
                            self.submission_lock.release()
                        except Exception:
                            pass
                        
            except Exception as e:
                if self.stop_event.is_set():
                    break
                now = time.time()
                show_conn_err = False
                with self._log_lock:
                    if "connection_error" != self._last_err_msg or (now - self._last_err_time) > 2.0:
                        self._last_err_msg = "connection_error"
                        self._last_err_time = now
                        show_conn_err = True
                if show_conn_err and not self.stop_event.is_set():
                    self.log(f"통신 에러 발생: {e} - 세션 재연결 시도", "warning")
                try:
                    await session.close()
                except Exception:
                    pass
                
                headers_re = {
                    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
                    "Content-Type": "application/x-www-form-urlencoded",
                    "Referer": f"https://zeroworldkorea.com/layout/res/home.php?go=rev.make&s_subj={s_subj}&zizum_num={zizum_num}&rev_days={rev_days}"
                }
                session = aiohttp.ClientSession(headers=headers_re)
                if hasattr(self, "session_pool") and len(self.session_pool) > 0:
                    local_idx = task_idx % len(self.session_pool)
                    self.session_pool[local_idx] = (session, self.session_pool[local_idx][1])
                if self.stop_event.is_set():
                    break
                await asyncio.sleep(0.5)
                
        is_pooled = hasattr(self, "session_pool") and len(self.session_pool) > 0
        if not is_pooled:
            await session.close()

    async def pre_fetch_sessions_async(self, num_sessions, reservation_data):
        self.session_pool = []
        self.log(f"Pre-fetching {num_sessions} sessions for Sinbiweb API...", "info")
        home_url = "https://zeroworldkorea.com/layout/res/home.php"
        for _ in range(num_sessions):
            headers = {
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
            }
            session = aiohttp.ClientSession(headers=headers)
            try:
                await session.get(home_url, timeout=5)
            except Exception:
                pass
            self.session_pool.append((session, None))
