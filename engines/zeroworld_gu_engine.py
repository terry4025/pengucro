import asyncio
import aiohttp
import requests
import json
import time
from bs4 import BeautifulSoup
from engines.base_engine import BaseEngine

class ZeroWorldGuEngine(BaseEngine):
    def __init__(self, site_url, log_callback, success_callback=None):
        """
        ZeroWorld Old (Laravel-based) Booking Engine.
        """
        super().__init__(log_callback, success_callback)
        self.site_url = site_url

    def get_csrf_token(self, session):
        response = session.get(self.site_url)
        soup = BeautifulSoup(response.text, 'html.parser')
        csrf_token = soup.find('meta', {'name': 'csrf-token'})['content']
        return csrf_token

    def make_reservation_thread(self, reservation_data):
        session = requests.Session()
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
        }
        session.headers.update(headers)

        target_time = reservation_data.get("reservationTime")[:5]
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
                    'reservationDate': reservation_data.get('reservationDate'),
                    'name': reservation_data.get('name'),
                    'phone': reservation_data.get('phone'),
                    'people': reservation_data.get('people'),
                    'paymentType': '1',
                    'themePK': reservation_data.get('themePK'),
                    'reservationTime': reservation_data.get('reservationTime'),
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
        session = None
        csrf_token = None
        
        if hasattr(self, "session_pool") and len(self.session_pool) > 0:
            local_idx = task_idx % len(self.session_pool)
            session, csrf_token = self.session_pool[local_idx]
            
        if not session:
            headers = {
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
            }
            session = aiohttp.ClientSession(headers=headers)
            
        target_time = reservation_data.get("reservationTime")[:5]
        
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
                    'reservationDate': reservation_data.get('reservationDate'),
                    'name': reservation_data.get('name'),
                    'phone': reservation_data.get('phone'),
                    'people': reservation_data.get('people'),
                    'paymentType': '1',
                    'themePK': reservation_data.get('themePK'),
                    'reservationTime': reservation_data.get('reservationTime'),
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
                if self.stop_event.is_set():
                    break
                csrf_token = None
                self.silent_tick(f"연결 오류: {e}")
                await asyncio.sleep(0.2)
                
        is_pooled = hasattr(self, "session_pool") and len(self.session_pool) > 0
        if not is_pooled:
            await session.close()

    async def get_csrf_token_async(self, session):
        async with session.get(self.site_url) as resp:
            text = await resp.text()
            soup = BeautifulSoup(text, 'html.parser')
            csrf_token = soup.find('meta', {'name': 'csrf-token'})['content']
            return csrf_token

    async def pre_fetch_sessions_async(self, num_sessions, reservation_data):
        self.session_pool = []
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
