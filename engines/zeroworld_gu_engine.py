import asyncio
import json
import time

import aiohttp
import requests
from bs4 import BeautifulSoup

from engines.async_hot_path import (
    create_isolated_session,
    create_shared_connector,
)
from engines.base_engine import BaseEngine


class ZeroWorldGuEngine(BaseEngine):
    # The reservation POSTs already passed timeout=8; the CSRF lookups did not,
    # so a stalled connection there left the worker unjoinable and locked the
    # GUI's stop button.
    LOOKUP_TIMEOUT = 5
    SUBMIT_TIMEOUT = 8
    USE_ASYNC_HOT_PATH = True

    def __init__(self, site_url, log_callback, success_callback=None):
        """
        ZeroWorld Old (Laravel-based) Booking Engine.
        """
        super().__init__(log_callback, success_callback)
        self.site_url = site_url

    def get_csrf_token(self, session):
        response = session.get(self.site_url, timeout=self.LOOKUP_TIMEOUT)
        soup = BeautifulSoup(response.text, 'html.parser')
        meta = soup.find('meta', {'name': 'csrf-token'})
        if meta is None or not meta.get('content'):
            raise ValueError('CSRF 토큰을 찾지 못했습니다. 사이트 구조가 변경되었을 수 있습니다.')
        return meta['content']

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
                    self.notify_success()
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
            session = aiohttp.ClientSession(
                headers=headers,
                timeout=aiohttp.ClientTimeout(total=self.SUBMIT_TIMEOUT),
            )
            
        target_time = reservation_data.get("reservationTime")[:5]
        post_headers = {
            'Content-Type': 'application/json',
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
        
        while not self.stop_event.is_set():
            try:
                if not csrf_token:
                    refresh_gate = getattr(
                        self,
                        "async_csrf_semaphore",
                        self.async_csrf_lock,
                    )
                    async with refresh_gate:
                        csrf_token = await self.get_csrf_token_async(session)
                    
                if self.stop_event.is_set():
                    break

                await self.wait_async_scan_turn()
                if self.stop_event.is_set():
                    break
                headers = {**post_headers, 'X-CSRF-TOKEN': csrf_token}
                started = time.perf_counter()
                async with session.post(self.site_url, json=laravel_payload, headers=headers, timeout=8) as resp:
                    rtt_ms = max(0.0, (time.perf_counter() - started) * 1000.0)
                    recovery_delay = self.observe_async_response(resp, rtt_ms)
                    if resp.status == 419:
                        await resp.read()
                        self.silent_tick(f"{target_time} CSRF 만료 · 토큰 갱신 후 재시도")
                        csrf_token = None
                        continue
                        
                    if resp.status == 200:
                        try:
                            res_json = await resp.json()
                            success_info = str(res_json)
                        except Exception:
                            success_info = await resp.text()
                            
                        self.log(f"🎉 [태스크 {task_idx+1}] 예약 성공: {success_info[:200]}", "success")
                        self.notify_success()
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
                            
                        recovery_suffix = (
                            f" · 서버 복구 간격 {recovery_delay:.1f}초"
                            if recovery_delay
                            else ""
                        )
                        if "이미 예약" in error_message or "already" in error_message.lower():
                            self.silent_tick(
                                f"{target_time} 슬롯 이미 예약 완료{recovery_suffix}"
                            )
                        elif "결제 수단" in error_message or "결제수단" in error_message:
                            self.silent_tick(
                                f"{target_time} 결제수단 에러 / 마감{recovery_suffix}"
                            )
                        else:
                            self.silent_tick(
                                f"{target_time} 오류: {error_message}{recovery_suffix}"
                            )
            except Exception as e:
                if self.stop_event.is_set():
                    break
                csrf_token = None
                recovery_delay = self.observe_async_network_failure()
                self.silent_tick(
                    f"연결 오류: {e} · 복구 간격 {max(0.2, recovery_delay):.1f}초"
                )
                await asyncio.sleep(max(0.2, recovery_delay))
                
        is_pooled = hasattr(self, "session_pool") and len(self.session_pool) > 0
        if not is_pooled:
            await session.close()

    async def get_csrf_token_async(self, session):
        async with session.get(self.site_url) as resp:
            text = await resp.text()
            soup = BeautifulSoup(text, 'html.parser')
            meta = soup.find('meta', {'name': 'csrf-token'})
            if meta is None or not meta.get('content'):
                raise ValueError('CSRF 토큰을 찾지 못했습니다. 사이트 구조가 변경되었을 수 있습니다.')
            return meta['content']

    async def pre_fetch_sessions_async(self, num_sessions, reservation_data):
        self.session_pool = []
        self._shared_connector = create_shared_connector(num_sessions)
        self.log(f"Pre-fetching {num_sessions} sessions and CSRF tokens...", "info")
        async def fetch_one(idx):
            headers = {
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
            }
            session = create_isolated_session(
                self._shared_connector,
                headers=headers,
                timeout=aiohttp.ClientTimeout(total=self.SUBMIT_TIMEOUT),
            )
            try:
                await self.wait_async_scan_turn()
                csrf = await self.get_csrf_token_async(session)
                return session, csrf
            except Exception as e:
                self.log(f"Pre-fetch session {idx} failed: {e}", "warning")
                return session, None
        tasks = [fetch_one(i) for i in range(num_sessions)]
        results = await asyncio.gather(*tasks)
        self.session_pool.extend(results)
        prepared = sum(1 for _session, token in self.session_pool if token)
        self.log(
            f"제로월드 구형 연결 예열 완료 · CSRF 준비 {prepared}/{num_sessions} · "
            "전체 세션 연속 시도 시작",
            "info",
        )
