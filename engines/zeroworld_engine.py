import asyncio
import aiohttp
import requests
import json
import re
import time
from bs4 import BeautifulSoup
from engines.base_engine import BaseEngine

class ZeroWorldEngine(BaseEngine):
    def __init__(self, site_url, log_callback, success_callback=None, is_shin=False):
        """
        ZeroWorld high-speed Booking Engine.
        Supports both Old (Laravel-based) and New (Sinbiweb-based) sites.
        """
        super().__init__(log_callback, success_callback)
        self.site_url = site_url
        self.is_shin = is_shin

    def get_csrf_token(self, session):
        response = session.get(self.site_url)
        soup = BeautifulSoup(response.text, 'html.parser')
        csrf_token = soup.find('meta', {'name': 'csrf-token'})['content']
        return csrf_token

    def make_reservation_thread(self, reservation_data):
        if self.is_shin:
            self._make_reservation_shin_sync(reservation_data)
        else:
            self._make_reservation_old_sync(reservation_data)

    def _make_reservation_shin_sync(self, res_data):
        session = requests.Session()
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
        }
        session.headers.update(headers)

        zizum_num = res_data.get("branch", "4")
        rev_days = res_data.get("reservationDate")
        theme_num = res_data.get("themePK")
        target_time = res_data.get("reservationTime")[:5]
        name = res_data.get("name")
        phone_digits = "".join(c for c in res_data.get("phone", "") if c.isdigit())
        people = res_data.get("people", "2")
        s_subj = "B" if zizum_num == "2" else "A"

        sel_url = "https://zeroworldkorea.com/core/res/rev.make.sel.php"
        act_url = "https://zeroworldkorea.com/core/res/rev.act.php"

        post_headers = {
            "Content-Type": "application/x-www-form-urlencoded",
            "Referer": f"https://zeroworldkorea.com/layout/res/home.php?go=rev.make&s_subj={s_subj}&zizum_num={zizum_num}&rev_days={rev_days}"
        }

        self.log(f"신 제로월드 고속 감시 시작 (스레드): {target_time}", "info")

        slot_id = None
        while not self.stop_event.is_set():
            try:
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
                for a in soup.find_all('a'):
                    if target_time in a.text:
                        classes = a.get('class', [])
                        if any(c in classes for c in ['disable', 'close', 'sold-out']):
                            continue
                        
                        href = a.get('href', '')
                        match = re.search(r"fun_theme_time_select\('(\d+)'", href)
                        if match:
                            found_slot = match.group(1)
                            break

                if not found_slot:
                    self.silent_tick(f"{target_time} 슬롯 대기")
                    time.sleep(0.1)
                    continue

                slot_id = found_slot
                self.log(f"스레드 {target_time} 슬롯 발견! ID: {slot_id}. 예약 제출 중...", "info")

                # 2. Select slot
                sel_payload = f"act=theme_time_select&theme_time_num={slot_id}"
                session.post(sel_url, data=sel_payload, headers=post_headers, timeout=5)

                # 3. Finalize reservation
                act_data = {
                    "name": name,
                    "mobile": phone_digits,
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
                if "rev.pay" in final_url or any("rev.pay" in u for u in history_urls) or "rev.pay" in act_text:
                    success = True
                elif "결제" in act_text or "toss" in act_text.lower() or "vbank" in act_text.lower():
                    success = True

                if success:
                    self.log(f"🎉 예약 성공! (가상계좌 결제 대기)", "success")
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
                    self.log(f"제출 실패: {err_msg} - 재시도", "warning")
                    time.sleep(0.5)

            except Exception as e:
                self.log(f"통신 에러 발생: {e} - 재시도", "warning")
                time.sleep(0.1)

    def _make_reservation_old_sync(self, res_data):
        session = requests.Session()
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
        }
        session.headers.update(headers)

        target_time = res_data.get("reservationTime")[:5]
        csrf_token = None

        while not self.stop_event.is_set():
            try:
                if not csrf_token:
                    csrf_token = self.get_csrf_token(session)

                post_headers = {
                    'Content-Type': 'application/json',
                    'X-CSRF-TOKEN': csrf_token
                }

                laravel_payload = {
                    'reservationDate': res_data.get('reservationDate'),
                    'name': res_data.get('name'),
                    'phone': res_data.get('phone'),
                    'people': res_data.get('people'),
                    'paymentType': '1',
                    'themePK': res_data.get('themePK'),
                    'reservationTime': res_data.get('reservationTime'),
                    'policy': 'true'
                }

                resp = session.post(self.site_url, json=laravel_payload, headers=post_headers, timeout=8)
                if resp.status_code == 419:
                    csrf_token = None
                    continue

                if resp.status_code == 200:
                    try:
                        success_info = resp.json()
                    except Exception:
                        success_info = resp.text
                    self.log(f"🎉 예약 성공: {str(success_info)[:200]}", "success")
                    self.stop_event.set()
                    if self.success_callback:
                        self.success_callback()
                    break
                else:
                    try:
                        decoded_text = resp.text
                    except Exception:
                        decoded_text = str(resp)

                    try:
                        error_data = json.loads(decoded_text)
                        error_message = error_data.get('Message', decoded_text[:200])
                    except Exception:
                        error_message = decoded_text[:200]

                    if "이미 예약" in error_message or "already" in error_message.lower():
                        self.silent_tick(f"{target_time} 슬롯 이미 예약 완료")
                    elif "결제 수단" in error_message or "결제수단" in error_message:
                        self.silent_tick(f"{target_time} 결제수단 에러 / 마감")
                    else:
                        self.silent_tick(f"{target_time} 오류: {error_message}")
                    time.sleep(0.1)

            except Exception as e:
                csrf_token = None
                self.silent_tick(f"연결 오류: {e}")
                time.sleep(0.2)

    async def make_reservation_async_task(self, reservation_data, task_idx):
        if self.is_shin:
            await self._make_reservation_shin_async(reservation_data, task_idx)
        else:
            await self._make_reservation_old_async(reservation_data, task_idx)

    async def _make_reservation_shin_async(self, res_data, task_idx):
        session = None
        if hasattr(self, "session_pool") and task_idx < len(self.session_pool):
            session, _ = self.session_pool[task_idx]
            
        if not session:
            headers = {
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
            }
            session = aiohttp.ClientSession(headers=headers)
            
        zizum_num = res_data.get("branch", "4")
        rev_days = res_data.get("reservationDate")
        theme_num = res_data.get("themePK")
        target_time = res_data.get("reservationTime")[:5]
        name = res_data.get("name")
        phone_digits = "".join(c for c in res_data.get("phone", "") if c.isdigit())
        people = res_data.get("people", "2")
        s_subj = "B" if zizum_num == "2" else "A"
        
        sel_url = "https://zeroworldkorea.com/core/res/rev.make.sel.php"
        act_url = "https://zeroworldkorea.com/core/res/rev.act.php"
        
        headers = {
            "Content-Type": "application/x-www-form-urlencoded",
            "Referer": f"https://zeroworldkorea.com/layout/res/home.php?go=rev.make&s_subj={s_subj}&zizum_num={zizum_num}&rev_days={rev_days}"
        }
        
        self.log(f"[태스크 {task_idx+1}] 신 제로월드 고속 감시 시작: {target_time}", "info")
        
        slot_id = None
        while not self.stop_event.is_set():
            try:
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
                    for a in soup.find_all('a'):
                        if target_time in a.text:
                            classes = a.get('class', [])
                            if any(c in classes for c in ['disable', 'close', 'sold-out']):
                                continue
                            
                            href = a.get('href', '')
                            match = re.search(r"fun_theme_time_select\('(\d+)'", href)
                            if match:
                                found_slot = match.group(1)
                                break
                                
                    if not found_slot:
                        self.silent_tick(f"{target_time} 슬롯 대기")
                        await asyncio.sleep(0.1)
                        continue
                        
                    slot_id = found_slot
                    self.log(f"[태스크 {task_idx+1}] {target_time} 슬롯 발견! ID: {slot_id}. 예약 제출 중...", "info")
                    
                # 2. Select slot
                sel_payload = f"act=theme_time_select&theme_time_num={slot_id}"
                async with session.post(sel_url, data=sel_payload, headers=headers, timeout=5):
                    pass
                
                # 3. Finalize reservation
                act_data = {
                    "name": name,
                    "mobile": phone_digits,
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
                    if "rev.pay" in final_url or any("rev.pay" in u for u in history_urls) or "rev.pay" in act_text:
                        success = True
                    elif "결제" in act_text or "toss" in act_text.lower() or "vbank" in act_text.lower():
                        success = True
                        
                    if success:
                        self.log(f"🎉 [태스크 {task_idx+1}] 예약 선점 성공! (가상계좌 결제 대기)", "success")
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
                                    
                        self.log(f"[태스크 {task_idx+1}] 제출 실패: {err_msg} - 재시도", "warning")
                        await asyncio.sleep(0.5)
                        
            except Exception as e:
                self.log(f"[태스크 {task_idx+1}] 통신 에러 발생: {e} - 재시도", "warning")
                await asyncio.sleep(0.1)
                
        if not hasattr(self, "session_pool") or task_idx >= len(self.session_pool):
            await session.close()

    async def _make_reservation_old_async(self, res_data, task_idx):
        session = None
        csrf_token = None
        
        if hasattr(self, "session_pool") and task_idx < len(self.session_pool):
            session, csrf_token = self.session_pool[task_idx]
            
        if not session:
            headers = {
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
            }
            session = aiohttp.ClientSession(headers=headers)
            
        target_time = res_data.get("reservationTime")[:5]
        
        while not self.stop_event.is_set():
            try:
                if not csrf_token:
                    csrf_token = await self.get_csrf_token_async(session)
                    
                if self.stop_event.is_set():
                    break
                    
                headers = {
                    'Content-Type': 'application/json',
                    'X-CSRF-TOKEN': csrf_token
                }
                
                laravel_payload = {
                    'reservationDate': res_data.get('reservationDate'),
                    'name': res_data.get('name'),
                    'phone': res_data.get('phone'),
                    'people': res_data.get('people'),
                    'paymentType': '1',
                    'themePK': res_data.get('themePK'),
                    'reservationTime': res_data.get('reservationTime'),
                    'policy': 'true'
                }
                
                async with session.post(self.site_url, json=laravel_payload, headers=headers, timeout=8) as resp:
                    if resp.status == 419:
                        csrf_token = None
                        continue
                        
                    if resp.status == 200:
                        try:
                            res_json = await resp.json()
                            success_info = str(res_json)
                        except Exception:
                            success_info = await resp.text()
                            
                        self.log(f"🎉 [태스크 {task_idx+1}] 예약 성공: {success_info[:200]}", "success")
                        self.stop_event.set()
                        if self.success_callback:
                            self.success_callback()
                        break
                    else:
                        try:
                            decoded_text = await resp.text()
                        except Exception:
                            decoded_text = str(resp)
                            
                        try:
                            error_data = json.loads(decoded_text)
                            error_message = error_data.get('Message', decoded_text[:200])
                        except Exception:
                            error_message = decoded_text[:200]
                            
                        if "이미 예약" in error_message or "already" in error_message.lower():
                            self.silent_tick(f"{target_time} 슬롯 이미 예약 완료")
                        elif "결제 수단" in error_message or "결제수단" in error_message:
                            self.silent_tick(f"{target_time} 결제수단 에러 / 마감")
                        else:
                            self.silent_tick(f"{target_time} 오류: {error_message}")
                            
                        await asyncio.sleep(0.1)
            except Exception as e:
                csrf_token = None
                self.silent_tick(f"연결 오류: {e}")
                await asyncio.sleep(0.2)
                
        if not hasattr(self, "session_pool") or task_idx >= len(self.session_pool):
            await session.close()

    async def get_csrf_token_async(self, session):
        async with session.get(self.site_url) as resp:
            text = await resp.text()
            soup = BeautifulSoup(text, 'html.parser')
            csrf_token = soup.find('meta', {'name': 'csrf-token'})['content']
            return csrf_token

    async def pre_fetch_sessions_async(self, num_sessions, reservation_data):
        import aiohttp
        import asyncio
        
        self.session_pool = []
        if not self.is_shin:
            self.log(f"Pre-fetching {num_sessions} sessions and CSRF tokens...", "info")
            async def fetch_one(idx):
                headers = {
                    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
                }
                session = aiohttp.ClientSession(headers=headers)
                try:
                    csrf = await self.get_csrf_token_async(session)
                    return session, csrf
                except Exception as e:
                    await session.close()
                    self.log(f"Pre-fetch session {idx} failed: {e}", "warning")
                    return None
            tasks = [fetch_one(i) for i in range(num_sessions)]
            results = await asyncio.gather(*tasks)
            for res in results:
                if res:
                    self.session_pool.append(res)
            self.log(f"Pre-fetched {len(self.session_pool)}/{num_sessions} sessions successfully.", "info")
        else:
            self.log(f"Pre-fetching {num_sessions} sessions for Sinbiweb API...", "info")
            for _ in range(num_sessions):
                headers = {
                    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
                }
                session = aiohttp.ClientSession(headers=headers)
                self.session_pool.append((session, None))
