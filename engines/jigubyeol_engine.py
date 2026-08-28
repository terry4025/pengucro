import asyncio
import hashlib
import json
import re
import threading
import time
from collections import OrderedDict

import aiohttp
import requests
from bs4 import BeautifulSoup

from engines.async_hot_path import (
    create_isolated_session,
    create_shared_connector,
    drain_response,
)
from engines.base_engine import BaseEngine
from pengucro.diagnostics import format_exception


class JigubyeolEngine(BaseEngine):
    # Every HTTP call must be bounded. Without a timeout a worker that hits a
    # stalled connection blocks forever, BaseEngine._monitor_threads never
    # finishes joining it, is_running stays True and the GUI's stop button
    # locks up permanently.
    LOOKUP_TIMEOUT = 5
    SUBMIT_TIMEOUT = 8
    FINAL_RECONCILE_TIMEOUT = 3
    USE_ASYNC_HOT_PATH = True
    ERROR_CACHE_SIZE = 64
    _INPUT_TAG_RE = re.compile(r"<input\b[^>]*>", re.IGNORECASE)
    _NAME_ATTR_RE = re.compile(
        r"\bname\s*=\s*(['\"]?)payment_method\1(?:\s|/?>)",
        re.IGNORECASE,
    )
    _VALUE_ATTR_RE = re.compile(
        r"\bvalue\s*=\s*(?:['\"]([^'\"]*)['\"]|([^\s>]+))",
        re.IGNORECASE,
    )

    def __init__(self, log_callback, success_callback=None, site_url=None):
        super().__init__(log_callback, success_callback)
        self.base_url = site_url if site_url else 'https://www.xn--2e0b040a4xj.com'
        self._async_error_cache: OrderedDict[str, str] = OrderedDict()
        self._final_submission_state = "idle"
        self._time_selection_headers = {
            'Content-Type': 'application/x-www-form-urlencoded',
            'X-Requested-With': 'XMLHttpRequest',
            'Referer': f'{self.base_url}/reservation',
            'Origin': self.base_url,
        }

    def _cached_error_message(self, decoded_text, reservation_data=None):
        raw = str(decoded_text or "")
        digest = hashlib.blake2s(raw.encode("utf-8", errors="replace"), digest_size=12).hexdigest()
        cached = self._async_error_cache.get(digest)
        if cached is not None:
            self._async_error_cache.move_to_end(digest)
            return cached
        message = self._safe_error_message(raw, reservation_data)
        self._async_error_cache[digest] = message
        self._async_error_cache.move_to_end(digest)
        while len(self._async_error_cache) > self.ERROR_CACHE_SIZE:
            self._async_error_cache.popitem(last=False)
        return message

    @classmethod
    def _payment_method_from_html(cls, decoded_html):
        """Read the one hot-path hidden value without a full DOM allocation."""

        text = str(decoded_html or "")
        for tag in cls._INPUT_TAG_RE.findall(text):
            if not cls._NAME_ATTR_RE.search(tag):
                continue
            value = cls._VALUE_ATTR_RE.search(tag)
            if value:
                return value.group(1) if value.group(1) is not None else value.group(2)
            return "1"
        return "1"

    @staticmethod
    def _worker_name(task_idx=None):
        if task_idx is not None:
            return f"태스크 {task_idx + 1}"
        thread_name = threading.current_thread().name
        if thread_name.startswith("BookingThread-"):
            return f"작업 {thread_name.rsplit('-', 1)[-1]}"
        return "작업 1"

    @staticmethod
    def _elapsed_ms(started):
        return max(0.0, (time.perf_counter() - started) * 1000.0)

    @staticmethod
    def _redact_sensitive_text(value, reservation_data=None):
        text = re.sub(r"\s+", " ", str(value or "")).strip()
        if reservation_data:
            for key in ("name", "phone"):
                raw = str(reservation_data.get(key, "")).strip()
                if raw:
                    text = text.replace(raw, "[숨김]")
                digits = "".join(character for character in raw if character.isdigit())
                if len(digits) >= 7:
                    text = text.replace(digits, "[숨김]")
        text = re.sub(r"(?i)\b[\w.+-]+@[\w.-]+\.[a-z]{2,}\b", "[이메일 숨김]", text)
        text = re.sub(r"(?<!\d)01[016789][ -]?\d{3,4}[ -]?\d{4}(?!\d)", "[전화번호 숨김]", text)
        text = re.sub(r"(?<!\d)\d{7,}(?!\d)", "[긴 숫자 숨김]", text)
        text = re.sub(
            r"(?i)\b(token|csrf|cookie|session|name|phone|mobile|account|amount)\s*[=:]\s*[^\s,;]+",
            lambda match: f"{match.group(1)}=[숨김]",
            text,
        )
        return text[:240]

    @classmethod
    def _format_exception(cls, exc, reservation_data=None):
        return cls._redact_sensitive_text(format_exception(exc), reservation_data)

    @classmethod
    def _safe_error_message(cls, decoded_text, reservation_data=None):
        try:
            error_data = json.loads(decoded_text)
        except (json.JSONDecodeError, TypeError, ValueError):
            error_data = None

        if isinstance(error_data, dict):
            if isinstance(error_data.get('errors'), dict):
                err_details = []
                for field, messages in error_data['errors'].items():
                    if isinstance(messages, list):
                        message_text = ", ".join(str(message) for message in messages)
                    else:
                        message_text = str(messages)
                    err_details.append(f"{field}: {message_text}")
                main_message = error_data.get('message', '입력값 검증 실패')
                message = f"{main_message} ({'; '.join(err_details)})"
            else:
                message = error_data.get('Message') or error_data.get('message') or "JSON 오류 응답"
        else:
            soup = BeautifulSoup(str(decoded_text or ""), 'html.parser')
            message = soup.get_text(" ", strip=True) or "응답 본문에 오류 설명 없음"
        return cls._redact_sensitive_text(message, reservation_data)

    @staticmethod
    def _success_message(done_text):
        soup = BeautifulSoup(done_text, 'html.parser')
        booking_id = ""
        for row in soup.select('table tr'):
            heading = row.find('th')
            value = row.find('td')
            if heading and value and '예약번호' in heading.get_text(" ", strip=True):
                booking_id = value.get_text(" ", strip=True)
                break

        if "임시로" in done_text or "입금전" in done_text:
            message = "예약 성공! (가상계좌 임시 예약 완료 · 결제 정보는 사이트에서 확인)"
        else:
            message = "예약 성공! (예약 확정 완료)"
        if booking_id:
            message += f" · 예약번호 {booking_id}"
        return message

    @staticmethod
    def _completion_evidence(done_text):
        """Return a booking id only when the done page proves a reservation exists."""

        soup = BeautifulSoup(str(done_text or ""), 'html.parser')
        for row in soup.select('table tr'):
            heading = row.find('th')
            value = row.find('td')
            if not heading or not value:
                continue
            if '예약번호' in heading.get_text(" ", strip=True):
                booking_id = value.get_text(" ", strip=True)
                if booking_id:
                    return booking_id
        return ""

    async def _reconcile_final_submission(self, session, worker_name):
        """Read the session's done page without issuing another reservation POST."""

        done_url = f"{self.base_url}/reservation/done"
        try:
            async with asyncio.timeout(self.FINAL_RECONCILE_TIMEOUT):
                async with session.get(done_url) as done_res:
                    done_text = await done_res.text()
                    booking_id = self._completion_evidence(done_text)
                    if booking_id:
                        self._log_http(worker_name, "예약 결과 재확인", done_res.status, 0.0)
                        return done_text
        except Exception:
            return ""
        return ""

    def _log_http(self, worker_name, stage, status, rtt_ms):
        self.log(
            f"[{worker_name}] {stage} 응답 · HTTP {status} · RTT {rtt_ms:.0f}ms",
            "info",
        )

    def get_csrf_token(self, session, worker_name="작업 1"):
        started = time.perf_counter()
        response = session.get(f'{self.base_url}/reservation', timeout=self.LOOKUP_TIMEOUT)
        self._log_http(worker_name, "CSRF 준비", response.status_code, self._elapsed_ms(started))
        soup = BeautifulSoup(response.text, 'html.parser')
        meta = soup.find('meta', {'name': 'csrf-token'})
        if meta is None or not meta.get('content'):
            # Previously this raised TypeError from a subscript on None and was
            # swallowed by the generic handler as a "connection error".
            raise ValueError('CSRF 토큰을 찾지 못했습니다. 사이트 구조가 변경되었을 수 있습니다.')
        return meta['content']

    def submit_time_selection(self, session, csrf_token, reservation_data):
        headers = {
            'Content-Type': 'application/x-www-form-urlencoded',
            'X-Requested-With': 'XMLHttpRequest',
            'Referer': f'{self.base_url}/reservation',
            'Origin': self.base_url,
        }
        time_val = reservation_data['reservationTime']
        if len(time_val) == 8 and time_val.endswith(':00'):
            time_val = time_val[:5]

        form_data = {
            'branch': reservation_data['branch'],
            'theme': reservation_data['themePK'],
            'date': reservation_data['reservationDate'],
            'time': time_val,
            '_token': csrf_token,
        }
        response = session.post(
            f'{self.base_url}/reservation/create',
            data=form_data,
            headers=headers,
            timeout=self.LOOKUP_TIMEOUT,
        )
        return response

    def submit_reservation(self, session, csrf_token, reservation_data, payment_method):
        headers = {
            'Content-Type': 'application/x-www-form-urlencoded',
            'X-CSRF-TOKEN': csrf_token,
            'X-Requested-With': 'XMLHttpRequest',
            'Accept': 'application/json, text/javascript, */*; q=0.01',
            'Referer': f'{self.base_url}/reservation/create',
            'Origin': self.base_url,
        }
        time_val = reservation_data['reservationTime']
        if len(time_val) == 8 and time_val.endswith(':00'):
            time_val = time_val[:5]

        form_data = {
            'branch': reservation_data['branch'],
            'theme': reservation_data['themePK'],
            'date': reservation_data['reservationDate'],
            'time': time_val,
            'name': reservation_data['name'],
            'phone': reservation_data['phone'],
            'people': reservation_data['people'],
            'payment_method': payment_method,
            'policy': 'on',
            '_token': csrf_token,
        }
        
        endpoint = f'{self.base_url}/reservation'
        if payment_method != '1':
            endpoint = f'{self.base_url}/reservation/payment'
            
        response = session.post(
            endpoint, data=form_data, headers=headers, timeout=self.SUBMIT_TIMEOUT
        )
        return response

    def make_reservation_thread(self, reservation_data):
        session = requests.Session()
        csrf_token = None
        worker_name = self._worker_name()
        target_time = reservation_data['reservationTime'][:5]
        self.log(
            f"[{worker_name}] 지구별 예약 감시 시작 · 지점 {reservation_data.get('branch', '')} · "
            f"테마 {reservation_data.get('themePK', '')} · {reservation_data.get('reservationDate', '')} "
            f"{target_time}",
            "info",
        )
        
        while not self.stop_event.is_set():
            stage = "CSRF 준비"
            try:
                if not csrf_token:
                    csrf_token = self.get_csrf_token(session, worker_name)
                
                if self.stop_event.is_set():
                    break
                with self.submission_lock:
                    if self.stop_event.is_set():
                        break
                    
                    # Step 1: 시간 선택 선등록
                    stage = "시간 선택"
                    started = time.perf_counter()
                    step1_response = self.submit_time_selection(session, csrf_token, reservation_data)
                    step1_rtt = self._elapsed_ms(started)
                    if step1_response.status_code in (200, 201):
                        self._log_http(worker_name, stage, step1_response.status_code, step1_rtt)
                    if step1_response.status_code == 419:
                        self.log(
                            f"[{worker_name}] {target_time} 시도 중... "
                            f"({stage} · HTTP 419 CSRF 만료 · 토큰 갱신 후 재시도)",
                            "warning",
                        )
                        csrf_token = None
                        continue
                    if step1_response.status_code not in (200, 201):
                        self.handle_error(
                            step1_response,
                            reservation_data,
                            stage,
                            worker_name=worker_name,
                            rtt_ms=step1_rtt,
                        )
                        continue
                    
                    # Step 2: 최종 예약 완료
                    try:
                        decoded_html = step1_response.content.decode('utf-8')
                    except Exception:
                        decoded_html = step1_response.text
                    
                    payment_method = self._payment_method_from_html(decoded_html)
                    
                    stage = "최종 예약"
                    started = time.perf_counter()
                    step2_response = self.submit_reservation(session, csrf_token, reservation_data, payment_method)
                    step2_rtt = self._elapsed_ms(started)
                    if step2_response.status_code in (200, 201):
                        self._log_http(worker_name, stage, step2_response.status_code, step2_rtt)
                    
                    if step2_response.status_code == 419:
                        self.log(
                            f"[{worker_name}] {target_time} 시도 중... "
                            f"({stage} · HTTP 419 CSRF 만료 · 토큰 갱신 후 재시도)",
                            "warning",
                        )
                        csrf_token = None
                        continue
                        
                    if step2_response.status_code in (200, 201):
                        try:
                            stage = "완료 정보 확인"
                            done_url = f"{self.base_url}/reservation/done"
                            started = time.perf_counter()
                            done_res = session.get(done_url, timeout=self.LOOKUP_TIMEOUT)
                            self._log_http(
                                worker_name,
                                stage,
                                done_res.status_code,
                                self._elapsed_ms(started),
                            )
                            done_res.encoding = 'utf-8'
                            self.log(
                                f"[{worker_name}] {self._success_message(done_res.text)}",
                                "success",
                            )
                        except Exception as e:
                            self.log(
                                f"[{worker_name}] 예약 성공! (완료 정보 확인 실패 · "
                                f"{self._format_exception(e, reservation_data)})",
                                "success",
                            )
                            
                        self.notify_success()
                        break
                    else:
                        self.handle_error(
                            step2_response,
                            reservation_data,
                            stage,
                            worker_name=worker_name,
                            rtt_ms=step2_rtt,
                        )
            except Exception as e:
                csrf_token = None
                self.log(
                    f"[{worker_name}] {target_time} 시도 중... ({stage} 오류 · "
                    f"{self._format_exception(e, reservation_data)} · CSRF 초기화 후 재시도)",
                    "warning",
                )

    def handle_error(
        self,
        response,
        reservation_data,
        step_name,
        worker_name="작업 1",
        rtt_ms=0.0,
    ):
        time_slot = reservation_data['reservationTime'][:5]

        try:
            decoded_text = response.content.decode('utf-8')
        except Exception:
            decoded_text = response.text

        error_message = self._safe_error_message(decoded_text, reservation_data)
        response_meta = f"{step_name} 거절 · HTTP {response.status_code} · RTT {rtt_ms:.0f}ms"

        if "이미 예약" in error_message:
            retry_reason = "이미 예약된 시간대 · 다시 열릴 때까지 재시도"
        elif "결제 수단" in error_message or "결제수단" in error_message:
            retry_reason = "이미 예약되었거나 결제 수단 오류 · 재시도"
        else:
            retry_reason = f"{error_message} · 재시도"
        self.log(
            f"[{worker_name}] {time_slot} 시도 중... ({response_meta} · {retry_reason})",
            "warning",
        )

    async def make_reservation_async_task(self, reservation_data, task_idx):
        session = None
        csrf_token = None
        worker_name = self._worker_name(task_idx)
        target_time = reservation_data['reservationTime'][:5]
        self.log(
            f"[{worker_name}] 지구별 예약 감시 시작 · 지점 {reservation_data.get('branch', '')} · "
            f"테마 {reservation_data.get('themePK', '')} · {reservation_data.get('reservationDate', '')} "
            f"{target_time}",
            "info",
        )
        
        if hasattr(self, "session_pool") and len(self.session_pool) > 0:
            local_idx = task_idx % len(self.session_pool)
            session, csrf_token = self.session_pool[local_idx]
            
        if not session:
            headers = {
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
            }
            # A session-level ClientTimeout bounds every request made through it,
            # so no individual call site can hang indefinitely.
            session = aiohttp.ClientSession(
                headers=headers,
                timeout=aiohttp.ClientTimeout(total=self.SUBMIT_TIMEOUT),
            )
            
        try:
            while not self.stop_event.is_set():
                stage = "CSRF 준비"
                try:
                    if not csrf_token:
                        refresh_gate = getattr(
                            self,
                            "async_csrf_semaphore",
                            self.async_csrf_lock,
                        )
                        async with refresh_gate:
                            csrf_token = await self.get_csrf_token_async(session, worker_name)
                    
                    if self.stop_event.is_set():
                        break

                    # Step 1: 시간 선택 선등록
                    stage = "시간 선택"
                    await self.wait_async_scan_turn()
                    if self.stop_event.is_set():
                        break
                    started = time.perf_counter()
                    step1_response = await self.submit_time_selection_async(session, csrf_token, reservation_data)
                    step1_rtt = self._elapsed_ms(started)
                    recovery_delay = self.observe_async_response(step1_response, step1_rtt)
                    if step1_response.status in (200, 201):
                        self._log_http(worker_name, stage, step1_response.status, step1_rtt)
                    if step1_response.status == 419:
                        await drain_response(step1_response)
                        self.log(
                            f"[{worker_name}] {target_time} 시도 중... "
                            f"({stage} · HTTP 419 CSRF 만료 · 토큰 갱신 후 재시도)",
                            "warning",
                        )
                        csrf_token = None
                        continue
                    if step1_response.status not in (200, 201):
                        await self.handle_error_async(
                            step1_response,
                            reservation_data,
                            stage,
                            worker_name=worker_name,
                            rtt_ms=step1_rtt,
                            recovery_delay=recovery_delay,
                        )
                        continue

                    # Step 2: 최종 예약 완료
                    try:
                        decoded_html = await step1_response.text()
                    except Exception:
                        await drain_response(step1_response)
                        decoded_html = str(step1_response)

                    payment_method = self._payment_method_from_html(decoded_html)

                    stage = "최종 예약"
                    # Keep timetable/time-selection observation concurrent, but
                    # allow exactly one final reservation mutation at a time.
                    # If another worker succeeds while this one waits, do not
                    # send a duplicate final POST.
                    async with self.async_submission_lock:
                        if self.stop_event.is_set() or self._final_submission_state in {
                            "success", "uncertain",
                        }:
                            break
                        self._final_submission_state = "inflight"
                        try:
                            started = time.perf_counter()
                            step2_response = await self.submit_reservation_async(
                                session,
                                csrf_token,
                                reservation_data,
                                payment_method,
                            )
                        except Exception as exc:
                            done_text = await self._reconcile_final_submission(
                                session, worker_name
                            )
                            if done_text:
                                self._final_submission_state = "success"
                                self.log(
                                    f"[{worker_name}] {self._success_message(done_text)}",
                                    "success",
                                )
                                self.notify_success()
                                break
                            self._final_submission_state = "uncertain"
                            self.stop_event.set()
                            self.log(
                                f"[{worker_name}] [중복 방지 정지] 최종 예약 요청 뒤 응답을 "
                                f"확정하지 못했습니다. 추가 POST 없이 예약내역을 확인해주세요. "
                                f"({self._format_exception(exc, reservation_data)})",
                                "error",
                            )
                            break

                        step2_rtt = self._elapsed_ms(started)
                        final_recovery_delay = self.observe_async_response(
                            step2_response, step2_rtt
                        )
                        if step2_response.status == 419:
                            await drain_response(step2_response)
                            self._final_submission_state = "idle"
                            self.log(
                                f"[{worker_name}] {target_time} 시도 중... "
                                f"({stage} · HTTP 419 CSRF 만료 · 토큰 갱신 후 재시도)",
                                "warning",
                            )
                            csrf_token = None
                            continue
                        if step2_response.status not in (200, 201):
                            self._final_submission_state = "idle"
                            await self.handle_error_async(
                                step2_response,
                                reservation_data,
                                stage,
                                worker_name=worker_name,
                                rtt_ms=step2_rtt,
                                recovery_delay=final_recovery_delay,
                            )
                            continue

                        self._log_http(
                            worker_name, stage, step2_response.status, step2_rtt
                        )
                        await drain_response(step2_response)
                        done_text = await self._reconcile_final_submission(
                            session, worker_name
                        )
                        if not done_text:
                            self._final_submission_state = "uncertain"
                            self.stop_event.set()
                            self.log(
                                f"[{worker_name}] [중복 방지 정지] HTTP "
                                f"{step2_response.status} 응답 뒤 예약번호를 확인하지 못했습니다. "
                                "추가 POST 없이 예약내역을 확인해주세요.",
                                "error",
                            )
                            break
                        self._final_submission_state = "success"
                        self.log(
                            f"[{worker_name}] {self._success_message(done_text)}",
                            "success",
                        )
                        self.notify_success()
                        break
                except Exception as e:
                    if self.stop_event.is_set():
                        break
                    csrf_token = None
                    recovery_delay = self.observe_async_network_failure()
                    self.log(
                        f"[{worker_name}] {target_time} 시도 중... ({stage} 오류 · "
                        f"{self._format_exception(e, reservation_data)} · "
                        f"복구 간격 {max(0.1, recovery_delay):.1f}초 · "
                        "CSRF 초기화 후 재시도)",
                        "warning",
                    )
                    await asyncio.sleep(max(0.1, recovery_delay))
        finally:
            is_pooled = hasattr(self, "session_pool") and len(self.session_pool) > 0
            if not is_pooled:
                await session.close()

    async def get_csrf_token_async(self, session, worker_name="작업 1"):
        started = time.perf_counter()
        async with session.get(f'{self.base_url}/reservation') as resp:
            text = await resp.text()
            self._log_http(worker_name, "CSRF 준비", resp.status, self._elapsed_ms(started))
            from bs4 import BeautifulSoup
            soup = BeautifulSoup(text, 'html.parser')
            meta = soup.find('meta', {'name': 'csrf-token'})
            if meta is None or not meta.get('content'):
                raise ValueError('CSRF 토큰을 찾지 못했습니다. 사이트 구조가 변경되었을 수 있습니다.')
            return meta['content']

    async def submit_time_selection_async(self, session, csrf_token, reservation_data):
        time_val = reservation_data['reservationTime']
        if len(time_val) == 8 and time_val.endswith(':00'):
            time_val = time_val[:5]
            
        form_data = {
            'branch': reservation_data['branch'],
            'theme': reservation_data['themePK'],
            'date': reservation_data['reservationDate'],
            'time': time_val,
            '_token': csrf_token,
        }
        
        return await session.post(
            f'{self.base_url}/reservation/create',
            data=form_data,
            headers=self._time_selection_headers,
        )

    async def submit_reservation_async(self, session, csrf_token, reservation_data, payment_method):
        time_val = reservation_data['reservationTime']
        if len(time_val) == 8 and time_val.endswith(':00'):
            time_val = time_val[:5]
            
        form_data = {
            'branch': reservation_data['branch'],
            'theme': reservation_data['themePK'],
            'date': reservation_data['reservationDate'],
            'time': time_val,
            'name': reservation_data['name'],
            'phone': reservation_data['phone'],
            'people': reservation_data['people'],
            'payment_method': payment_method,
            'policy': 'on',
            '_token': csrf_token,
        }
        
        headers = {
            'Content-Type': 'application/x-www-form-urlencoded',
            'X-CSRF-TOKEN': csrf_token,
            'X-Requested-With': 'XMLHttpRequest',
            'Accept': 'application/json, text/javascript, */*; q=0.01',
            'Referer': f'{self.base_url}/reservation/create',
            'Origin': self.base_url,
        }
        
        endpoint = f'{self.base_url}/reservation'
        if payment_method != '1':
            endpoint = f'{self.base_url}/reservation/payment'
            
        return await session.post(endpoint, data=form_data, headers=headers)

    async def handle_error_async(
        self,
        response,
        reservation_data,
        step_name,
        worker_name="작업 1",
        rtt_ms=0.0,
        recovery_delay=0.0,
    ):
        time_slot = reservation_data['reservationTime'][:5]
        try:
            decoded_text = await response.text()
        except Exception:
            await drain_response(response)
            decoded_text = str(response)
            
        error_message = self._cached_error_message(decoded_text, reservation_data)
        response_meta = f"{step_name} 거절 · HTTP {response.status} · RTT {rtt_ms:.0f}ms"
        if recovery_delay:
            response_meta += f" · 서버 복구 간격 {recovery_delay:.1f}초"
            
        if "이미 예약" in error_message:
            retry_reason = "이미 예약된 시간대 · 다시 열릴 때까지 재시도"
        elif "결제 수단" in error_message or "결제수단" in error_message:
            retry_reason = "이미 예약되었거나 결제 수단 오류 · 재시도"
        else:
            retry_reason = f"{error_message} · 재시도"
        self.log(
            f"[{worker_name}] {time_slot} 시도 중... ({response_meta} · {retry_reason})",
            "warning",
        )

    async def pre_fetch_sessions_async(self, num_sessions, reservation_data):
        self.log(f"지구별 연결 예열 시작 · 세션 {num_sessions}개", "info")
        self._final_submission_state = "idle"
        
        self.session_pool = []
        self._shared_connector = create_shared_connector(num_sessions)
        
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
                csrf = await self.get_csrf_token_async(session, f"예열 {idx + 1}")
                return session, csrf
            except Exception as e:
                self.log(
                    f"[예열 {idx + 1}] CSRF 준비 실패 · {self._format_exception(e)} · "
                    "실행 중 해당 세션 자동 복구",
                    "warning",
                )
                return session, None
                
        tasks = [fetch_one(i) for i in range(num_sessions)]
        results = await asyncio.gather(*tasks)
        
        self.session_pool.extend(results)
                
        prepared = sum(1 for _session, token in self.session_pool if token)
        self.log(
            f"지구별 연결 예열 완료 · CSRF 준비 {prepared}/{num_sessions} · "
            "전체 세션 연속 스캔 시작",
            "info",
        )
