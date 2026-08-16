import asyncio
import aiohttp
import requests
import json
import os
import re
import time
import urllib.parse
from pathlib import Path
from bs4 import BeautifulSoup
from engines.base_engine import BaseEngine
from pengucro.diagnostics import format_exception
from pengucro.storage import append_history

class DoomEscapeEngine(BaseEngine):
    REQUEST_TIMEOUT_SECONDS = 5
    RECOVERY_INITIAL_SECONDS = 0.25
    RECOVERY_MAX_SECONDS = 3.0
    REQUEST_HEADERS = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/120.0.0.0 Safari/537.36"
        )
    }
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
        self.site_url = site_url or "https://doomescape.com"
        parsed = urllib.parse.urlparse(self.site_url)
        self.base_url = f"{parsed.scheme}://{parsed.netloc}" if parsed.netloc else self.site_url.rstrip("/")
        
        import threading
        self._log_lock = threading.Lock()
        self._notified_bypass = False
        self._notified_found = False
        self._last_err_msg = ""
        self._last_err_time = 0
        self._last_wait_time = 0
        self._site_recovering = False
        self._site_recovery_event = None
        self._outage_started_at = 0.0
        self._diagnostic_log_state = {}
        self._sync_worker_index = 0
        self._recovery_probe_diag = None

    @staticmethod
    def _describe_exception(exc):
        message = format_exception(exc)
        # Doom's completion request carries ck_code in the query string.  Some
        # HTTP client exceptions include the URL, so remove that one-time value
        # before either the UI or persistent logger can see it.
        return re.sub(
            r"(?i)(?P<prefix>[?&]ck_code=)[^&#\s]+",
            r"\g<prefix>[redacted]",
            message,
        )

    def _next_sync_worker_label(self):
        with self._log_lock:
            self._sync_worker_index += 1
            return f"동기 작업 {self._sync_worker_index}"

    def _log_http_diagnostic(
        self,
        worker,
        stage,
        method,
        status,
        elapsed_seconds,
        *,
        detail="",
        force=False,
    ):
        """Emit bounded, non-sensitive request diagnostics.

        Slot polling can run many times per second, so successful repeats are
        summarized at most once every five seconds.  No URL, request body,
        response body, cookie, token, name, phone number, or payment value is
        included.
        """
        status_text = str(status) if status is not None else "연결 실패"
        detail_text = str(detail).strip()
        key = (str(worker), str(stage), str(method), status_text, detail_text)
        now = time.monotonic()
        with self._log_lock:
            state = self._diagnostic_log_state.setdefault(
                key, {"last": 0.0, "count": 0}
            )
            state["count"] += 1
            should_log = force or not state["last"] or now - state["last"] >= 5.0
            if not should_log:
                return
            attempts = state["count"]
            state["last"] = now
            state["count"] = 0

        repeat = f" · 최근 {attempts}회" if attempts > 1 else ""
        suffix = f" · {detail_text}" if detail_text else ""
        level = "info" if str(status_text).startswith("2") else "warning"
        self.log(
            f"[{worker}] [HTTP] {stage} · {method} · status={status_text} · "
            f"RTT {max(0.0, elapsed_seconds) * 1000:.0f}ms{repeat}{suffix}",
            level,
        )

    @staticmethod
    def _safe_response_markers(text):
        lowered = (text or "").casefold()
        return {
            "has_completion_marker": any(
                marker in lowered
                for marker in ("rev.make.exe.php", "rev.make.end", "완료", "성공")
            ),
            "has_alert": "alert(" in lowered or "alert (" in lowered,
            "has_meta_refresh": "http-equiv=\"refresh\"" in lowered
            or "http-equiv='refresh'" in lowered,
        }

    def _write_safe_failure_summary(
        self,
        *,
        worker,
        stage,
        status,
        response_text,
        slot_id="",
        order_id="",
    ):
        """Persist metadata only; raw reservation HTML is never written."""
        try:
            diagnostic_dir = Path("scratch")
            diagnostic_dir.mkdir(parents=True, exist_ok=True)
            payload = {
                "recorded_at": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime()),
                "engine": "doomescape",
                "worker": str(worker),
                "stage": str(stage),
                "http_status": status,
                "response_bytes": len((response_text or "").encode("utf-8", errors="ignore")),
                "slot_id": str(slot_id),
                "order_id": str(order_id),
                "markers": self._safe_response_markers(response_text),
            }
            target = diagnostic_dir / "last_mutong_diagnostic.json"
            temporary = diagnostic_dir / "last_mutong_diagnostic.json.tmp"
            temporary.write_text(
                json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
            )
            os.replace(temporary, target)
            self.log(
                f"[{worker}] [진단] {stage} 응답은 원문 대신 민감정보를 제외한 요약으로 저장했습니다.",
                "warning",
            )
        except Exception as exc:
            self.log(
                f"[{worker}] [진단] 안전 진단 요약 저장 실패 · {self._describe_exception(exc)}",
                "warning",
            )

    @staticmethod
    def _reservation_page_looks_healthy(html_text):
        lowered = (html_text or "").lower()
        return bool(
            "tm_box" in lowered
            or "rev_days" in lowered
            or "go=rev.make" in lowered
        )

    @staticmethod
    def _is_transient_site_error(exc):
        if isinstance(exc, (asyncio.TimeoutError, aiohttp.ClientError, ConnectionError)):
            return True
        message = str(exc)
        return message.startswith("HTTP ") or message.startswith("INVALID_RESERVATION_PAGE")

    def _reset_async_recovery_state(self):
        self._site_recovery_event = asyncio.Event()
        self._site_recovery_event.set()
        self._site_recovering = False
        self._outage_started_at = 0.0

    async def _probe_reservation_page(self, list_url):
        session = aiohttp.ClientSession(headers=self.REQUEST_HEADERS)
        started = time.perf_counter()
        try:
            async with session.get(
                list_url, timeout=self.REQUEST_TIMEOUT_SECONDS
            ) as response:
                status = response.status
                if response.status != 200:
                    await response.read()
                    self._recovery_probe_diag = (
                        status,
                        time.perf_counter() - started,
                        "예약 페이지 HTTP 오류",
                    )
                    return False
                body = await response.read()
            html_text = body.decode("utf-8", errors="ignore")
            healthy = self._reservation_page_looks_healthy(html_text)
            self._recovery_probe_diag = (
                status,
                time.perf_counter() - started,
                "정상 형식" if healthy else "예약 페이지 형식 없음",
            )
            return healthy
        except (asyncio.TimeoutError, aiohttp.ClientError, OSError) as exc:
            self._recovery_probe_diag = (
                None,
                time.perf_counter() - started,
                self._describe_exception(exc),
            )
            return False
        finally:
            await session.close()

    async def _wait_for_site_recovery(self, list_url, task_idx, exc, stage="시간표 조회"):
        """Let one worker probe a failed site while every other worker waits.

        This avoids turning a temporary outage into a five-worker request storm.
        Once the representative probe sees the real reservation page again, all
        configured workers are released together and resume their normal fast
        polling.  The configured worker count itself is deliberately unchanged.
        """
        if self._site_recovery_event is None:
            self._reset_async_recovery_state()

        if self._site_recovering:
            while not self.stop_event.is_set():
                try:
                    await asyncio.wait_for(
                        self._site_recovery_event.wait(), timeout=0.25
                    )
                    return
                except asyncio.TimeoutError:
                    continue
            return

        self._site_recovering = True
        self._site_recovery_event.clear()
        self._outage_started_at = time.monotonic()
        reason = self._describe_exception(exc)
        self.log(
            f"[태스크 {task_idx + 1}] 서버 응답 장애 감지 · 단계={stage} · {reason} · "
            "대표 연결 1개로 복구 여부를 확인합니다.",
            "warning",
        )
        delay = max(0.0, self.RECOVERY_INITIAL_SECONDS)
        probe_count = 0
        try:
            while not self.stop_event.is_set():
                if probe_count:
                    # sleep(0) still yields so workers that failed together can
                    # join the same recovery gate instead of starting a second
                    # probe cycle immediately after the first one.
                    await asyncio.sleep(delay)
                probe_count += 1
                recovered = await self._probe_reservation_page(list_url)
                probe_diag = self._recovery_probe_diag
                if probe_diag is not None:
                    status, probe_rtt, probe_detail = probe_diag
                    self._log_http_diagnostic(
                        f"복구 확인 {task_idx + 1}",
                        "예약 페이지 복구 확인",
                        "GET",
                        status,
                        probe_rtt,
                        detail=f"{probe_detail} · 재시도 {probe_count}회",
                        force=recovered or probe_count == 1,
                    )
                if recovered:
                    elapsed = max(0.0, time.monotonic() - self._outage_started_at)
                    self.log(
                        f"[정보] 둠이스케이프 서버 응답 복구 확인 · "
                        f"{elapsed:.1f}초 · 모든 작업을 즉시 재개합니다.",
                        "success",
                    )
                    return
                delay = min(
                    self.RECOVERY_MAX_SECONDS,
                    max(self.RECOVERY_INITIAL_SECONDS, delay * 2 or 0.01),
                )
        finally:
            self._site_recovering = False
            self._outage_started_at = 0.0
            self._site_recovery_event.set()

    def make_reservation_thread(self, reservation_data):
        worker_label = self._next_sync_worker_label()
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

        theme_name = reservation_data.get("themeLabel") or self.THEME_ID_TO_NAME.get(theme_num, "")

        list_url = f"{self.base_url}/layout/res/home.php?go=rev.make&s_zizum={zizum_num}&rev_days={rev_days}"
        act_url = f"{self.base_url}/core/res/rev.act.php"

        self.log(f"[{worker_label}] 둠이스케이프 동기 작업 시작 (지점: {zizum_num}, 테마: {theme_name}({theme_num}), 날짜: {rev_days}, 시간: {target_time})", "info")

        while not self.stop_event.is_set():
            current_stage = "시간표 조회"
            try:
                # 1. Fetch reservation page to list slots
                request_started = time.perf_counter()
                resp = session.get(list_url, timeout=5)
                request_rtt = time.perf_counter() - request_started
                self._log_http_diagnostic(
                    worker_label,
                    current_stage,
                    "GET",
                    resp.status_code,
                    request_rtt,
                    detail="100ms 후 재시도" if resp.status_code != 200 else "",
                    force=resp.status_code != 200,
                )
                if resp.status_code != 200:
                    self.silent_tick(
                        f"[{worker_label}] 시간표 조회 실패 · HTTP {resp.status_code} · 100ms 후 재시도"
                    )
                    time.sleep(0.1)
                    continue

                html_text = resp.content.decode('utf-8', errors='ignore')
                
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
                        self.log(
                            f"[{worker_label}] [재시도] 시간표 조회 · {target_time} 슬롯이 없거나 마감 상태 · 100ms 후 재조회",
                            "warning",
                        )
                    time.sleep(0.1)
                    continue

                slot_id = found_slot
                show_found = False
                with self._log_lock:
                    if not self._notified_found:
                        self._notified_found = True
                        show_found = True
                if show_found:
                    self.log(
                        f"[{worker_label}] [슬롯 확인] 시간 {target_time} · slotId={slot_id} · 예약 제출 단계로 이동",
                        "info",
                    )

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
                    current_stage = "예약 입력 화면 조회"
                    input_url = f"{self.base_url}/layout/res/home.php?go=rev.make.input&rev_days={rev_days}&theme_time_num={slot_id}"
                    request_started = time.perf_counter()
                    resp_input = session.get(input_url, timeout=5)
                    self._log_http_diagnostic(
                        worker_label,
                        current_stage,
                        "GET",
                        resp_input.status_code,
                        time.perf_counter() - request_started,
                        detail=f"slotId={slot_id}",
                        force=True,
                    )
                    input_html = resp_input.content.decode('utf-8', errors='ignore')

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
                        "Origin": self.base_url
                    }

                    current_stage = "예약 주문 생성"
                    request_started = time.perf_counter()
                    act_resp = session.post(act_url, data=post_data, headers=post_headers, timeout=8)
                    self._log_http_diagnostic(
                        worker_label,
                        current_stage,
                        "POST",
                        act_resp.status_code,
                        time.perf_counter() - request_started,
                        detail=f"slotId={slot_id}",
                        force=True,
                    )
                    act_text = act_resp.content.decode('utf-8', errors='ignore')

                    # Parse response to find num
                    num_m = re.search(r"num=(\d+)", act_text)
                    if num_m:
                        num = num_m.group(1)
                        self.log(
                            f"[{worker_label}] [주문 생성] slotId={slot_id} · orderId={num}",
                            "info",
                        )
                        kcp_url = f"{self.base_url}/layout/res/home.php?go=rev.kcp&num={num}"
                        
                        # 4. Fetch KCP page to extract ck_code
                        current_stage = "결제 준비 화면 조회"
                        request_started = time.perf_counter()
                        kcp_resp = session.get(kcp_url, timeout=8)
                        self._log_http_diagnostic(
                            worker_label,
                            current_stage,
                            "GET",
                            kcp_resp.status_code,
                            time.perf_counter() - request_started,
                            detail=f"orderId={num}",
                            force=True,
                        )
                        kcp_text = kcp_resp.content.decode('utf-8', errors='ignore')
                        
                        ck_m = re.search(r"name=['\"]?ck_code['\"]?\s*value=['\"]?([^'\"'>\s]+)", kcp_text)
                        ck_code_val = ck_m.group(1) if ck_m else ""

                        # 5. Submit mutong.php (using GET or POST - GET is browser default)
                        mutong_url = f"{self.base_url}/core/res/rev.make.mutong.php"
                        mutong_params = {
                            "num": num,
                            "ck_code": ck_code_val,
                            "layout_folder": "layout/res",
                            "payment": "D"  # 'D' is mutong for Doom Escape
                        }
                        
                        # Request using GET
                        query_str = urllib.parse.urlencode(mutong_params)
                        mutong_get_url = f"{mutong_url}?{query_str}"
                        current_stage = "무통장 예약 확정"
                        request_started = time.perf_counter()
                        mutong_resp = session.get(mutong_get_url, timeout=8)
                        mutong_status = mutong_resp.status_code
                        self._log_http_diagnostic(
                            worker_label,
                            current_stage,
                            "GET",
                            mutong_status,
                            time.perf_counter() - request_started,
                            detail=f"orderId={num}",
                            force=True,
                        )
                        mutong_text = mutong_resp.content.decode('utf-8', errors='ignore')

                        # Follow meta refresh -> rev.make.exe.php
                        refresh_m = re.search(r"url=([^'\"\>]+)", mutong_text, re.I)
                        if refresh_m:
                            next_url = refresh_m.group(1).strip()
                            if not next_url.startswith("http"):
                                next_url = urllib.parse.urljoin(mutong_url, next_url)
                            try:
                                current_stage = "예약 완료 화면 확인"
                                request_started = time.perf_counter()
                                exe_resp = session.get(next_url, timeout=8)
                                self._log_http_diagnostic(
                                    worker_label,
                                    current_stage,
                                    "GET",
                                    exe_resp.status_code,
                                    time.perf_counter() - request_started,
                                    detail=f"orderId={num}",
                                    force=True,
                                )
                                exe_text = exe_resp.content.decode('utf-8', errors='ignore')
                                mutong_text += exe_text
                            except Exception as exc:
                                self.log(
                                    f"[{worker_label}] [재시도 불가] {current_stage} · {self._describe_exception(exc)} · 이전 응답으로 결과 판정",
                                    "warning",
                                )

                        # Extract final booking number (ck_code)
                        bnum_m = re.search(r"ck_code=(\d+)", mutong_text)
                        booking_number = bnum_m.group(1) if bnum_m else ""
                        completion_code = booking_number or ck_code_val

                        if "rev.make.exe.php" in mutong_text or "rev.make.end" in mutong_text or "완료" in mutong_text or "성공" in mutong_text:
                            final_msg = (
                                f"예약 최종 완료! 예약번호: {booking_number}"
                                if booking_number
                                else "예약 최종 완료! 예약번호는 완료 화면에서 확인해주세요."
                            )
                            try:
                                import webbrowser
                                webbrowser.open(f"{self.base_url}/layout/res/home.php?go=rev.make.end&num={num}&ck_code={completion_code}")
                            except Exception:
                                pass
                            try:
                                append_history({
                                    "timestamp": time.strftime('%Y-%m-%d %H:%M:%S', time.localtime()),
                                    "site": "둠이스케이프",
                                    "date": rev_days,
                                    "time": target_time,
                                    "booking_number": booking_number,
                                })
                            except Exception:
                                pass
                        else:
                            self._write_safe_failure_summary(
                                worker=worker_label,
                                stage="무통장 예약 결과 판정",
                                status=mutong_status,
                                response_text=mutong_text,
                                slot_id=slot_id,
                                order_id=num,
                            )
                            final_msg = (
                                f"예약 선점 성공! 예약번호: {booking_number} / 임시번호: {num} "
                                "(결제확인 응답 재확인 필요)"
                                if booking_number
                                else f"예약 선점 성공! 임시번호: {num} (결제확인 응답 재확인 필요)"
                            )
                    else:
                        err_msg = "선점 실패"
                        alert_match = re.search(r"alert\s*\(\s*['\"](.*?)['\"]\s*\)", act_text)
                        if alert_match:
                            err_msg = alert_match.group(1)
                        raise Exception(err_msg)

                    self.log(f"🎉 {final_msg}", "success")
                    self.notify_success()
                    break

                finally:
                    if hasattr(self, "submission_lock"):
                        try:
                            self.submission_lock.release()
                        except RuntimeError:
                            pass

            except Exception as e:
                err_str = self._describe_exception(e)
                now = time.time()
                show_log = False
                with self._log_lock:
                    if err_str != self._last_err_msg or (now - self._last_err_time) > 3.0:
                        self._last_err_msg = err_str
                        self._last_err_time = now
                        show_log = True
                if show_log:
                    self.log(
                        f"[{worker_label}] [오류] {current_stage} · {err_str} · 100ms 후 재시도",
                        "error",
                    )
                time.sleep(0.1)

    async def make_reservation_async_task(self, reservation_data, task_idx):
        """
        Doom Escape Asynchronous Booking Path
        """
        import aiohttp
        import asyncio
        headers = dict(self.REQUEST_HEADERS)
        
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

        theme_name = reservation_data.get("themeLabel") or self.THEME_ID_TO_NAME.get(theme_num, "")
        
        list_url = f"{self.base_url}/layout/res/home.php?go=rev.make&s_zizum={zizum_num}&rev_days={rev_days}"
        act_url = f"{self.base_url}/core/res/rev.act.php"

        session = None
        if hasattr(self, "session_pool") and len(self.session_pool) > 0:
            session = self.session_pool[task_idx % len(self.session_pool)]
        if not session:
            session = aiohttp.ClientSession(headers=headers)

        worker_label = f"태스크 {task_idx + 1}"
        self.log(f"[{worker_label}] 둠이스케이프 비동기 작업 시작", "info")

        while not self.stop_event.is_set():
            current_stage = "시간표 조회"
            try:
                # 1. Fetch reservation page to list slots
                request_started = time.perf_counter()
                async with session.get(
                    list_url, timeout=self.REQUEST_TIMEOUT_SECONDS
                ) as resp:
                    list_status = resp.status
                    if resp.status != 200:
                        await resp.read()
                        self._log_http_diagnostic(
                            worker_label,
                            current_stage,
                            "GET",
                            list_status,
                            time.perf_counter() - request_started,
                            detail="서버 복구 확인으로 전환",
                            force=True,
                        )
                        raise RuntimeError(f"HTTP {resp.status}")

                    # Read raw bytes and decode as CP949
                    html_bytes = await resp.read()
                    html_text = html_bytes.decode('utf-8', errors='ignore')

                self._log_http_diagnostic(
                    worker_label,
                    current_stage,
                    "GET",
                    list_status,
                    time.perf_counter() - request_started,
                )

                if not self._reservation_page_looks_healthy(html_text):
                    raise RuntimeError("INVALID_RESERVATION_PAGE: 예약 페이지 형식 없음")

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
                        self.log(
                            f"[{worker_label}] [재시도] 시간표 조회 · {target_time} 슬롯이 없거나 마감 상태 · 100ms 후 재조회",
                            "warning",
                        )
                    await asyncio.sleep(0.1)
                    continue

                slot_id = found_slot
                show_found = False
                with self._log_lock:
                    if not self._notified_found:
                        self._notified_found = True
                        show_found = True
                if show_found:
                    self.log(
                        f"[{worker_label}] [슬롯 확인] 시간 {target_time} · slotId={slot_id} · 예약 제출 단계로 이동",
                        "info",
                    )

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
                    current_stage = "예약 입력 화면 조회"
                    input_url = f"{self.base_url}/layout/res/home.php?go=rev.make.input&rev_days={rev_days}&theme_time_num={slot_id}"
                    request_started = time.perf_counter()
                    async with session.get(input_url, timeout=5) as resp_input:
                        input_status = resp_input.status
                        input_bytes = await resp_input.read()
                        input_html = input_bytes.decode('utf-8', errors='ignore')
                    self._log_http_diagnostic(
                        worker_label,
                        current_stage,
                        "GET",
                        input_status,
                        time.perf_counter() - request_started,
                        detail=f"slotId={slot_id}",
                        force=True,
                    )

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
                        "Origin": self.base_url,
                        "User-Agent": headers["User-Agent"]
                    }

                    current_stage = "예약 주문 생성"
                    request_started = time.perf_counter()
                    async with session.post(act_url, data=post_data, headers=post_headers, timeout=8) as act_resp:
                        act_status = act_resp.status
                        act_bytes = await act_resp.read()
                        act_text = act_bytes.decode('utf-8', errors='ignore')
                    self._log_http_diagnostic(
                        worker_label,
                        current_stage,
                        "POST",
                        act_status,
                        time.perf_counter() - request_started,
                        detail=f"slotId={slot_id}",
                        force=True,
                    )

                    # Parse response to find num
                    num_m = re.search(r"num=(\d+)", act_text)
                    if num_m:
                        num = num_m.group(1)
                        self.log(
                            f"[{worker_label}] [주문 생성] slotId={slot_id} · orderId={num}",
                            "info",
                        )
                        kcp_url = f"{self.base_url}/layout/res/home.php?go=rev.kcp&num={num}"
                        
                        # 4. Fetch KCP page to extract ck_code
                        current_stage = "결제 준비 화면 조회"
                        request_started = time.perf_counter()
                        async with session.get(kcp_url, headers=headers, timeout=8) as kcp_resp:
                            kcp_status = kcp_resp.status
                            kcp_text = await kcp_resp.text(encoding='utf-8', errors='ignore')
                        self._log_http_diagnostic(
                            worker_label,
                            current_stage,
                            "GET",
                            kcp_status,
                            time.perf_counter() - request_started,
                            detail=f"orderId={num}",
                            force=True,
                        )
                        
                        ck_m = re.search(r"name=['\"]?ck_code['\"]?\s*value=['\"]?([^'\"'>\s]+)", kcp_text)
                        ck_code_val = ck_m.group(1) if ck_m else ""

                        # 5. Submit mutong.php (using GET or POST - GET is browser default)
                        mutong_url = f"{self.base_url}/core/res/rev.make.mutong.php"
                        mutong_params = {
                            "num": num,
                            "ck_code": ck_code_val,
                            "layout_folder": "layout/res",
                            "payment": "D"  # 'D' is mutong for Doom Escape
                        }
                        
                        query_str = urllib.parse.urlencode(mutong_params)
                        mutong_get_url = f"{mutong_url}?{query_str}"

                        current_stage = "무통장 예약 확정"
                        request_started = time.perf_counter()
                        async with session.get(mutong_get_url, headers=headers, timeout=8) as mutong_resp:
                            mutong_status = mutong_resp.status
                            mutong_bytes = await mutong_resp.read()
                            mutong_text = mutong_bytes.decode('utf-8', errors='ignore')
                        self._log_http_diagnostic(
                            worker_label,
                            current_stage,
                            "GET",
                            mutong_status,
                            time.perf_counter() - request_started,
                            detail=f"orderId={num}",
                            force=True,
                        )

                        # Follow meta refresh -> rev.make.exe.php
                        refresh_m = re.search(r"url=([^'\"\>]+)", mutong_text, re.I)
                        if refresh_m:
                            next_url = refresh_m.group(1).strip()
                            if not next_url.startswith("http"):
                                next_url = urllib.parse.urljoin(mutong_url, next_url)
                            try:
                                current_stage = "예약 완료 화면 확인"
                                request_started = time.perf_counter()
                                async with session.get(next_url, headers=headers, timeout=8) as exe_resp:
                                    exe_status = exe_resp.status
                                    exe_bytes = await exe_resp.read()
                                    exe_text = exe_bytes.decode('utf-8', errors='ignore')
                                    mutong_text += exe_text
                                self._log_http_diagnostic(
                                    worker_label,
                                    current_stage,
                                    "GET",
                                    exe_status,
                                    time.perf_counter() - request_started,
                                    detail=f"orderId={num}",
                                    force=True,
                                )
                            except Exception as exc:
                                self.log(
                                    f"[{worker_label}] [재시도 불가] {current_stage} · {self._describe_exception(exc)} · 이전 응답으로 결과 판정",
                                    "warning",
                                )

                        # Extract final booking number (ck_code)
                        bnum_m = re.search(r"ck_code=(\d+)", mutong_text)
                        booking_number = bnum_m.group(1) if bnum_m else ""
                        completion_code = booking_number or ck_code_val

                        if "rev.make.exe.php" in mutong_text or "rev.make.end" in mutong_text or "완료" in mutong_text or "성공" in mutong_text:
                            final_msg = (
                                f"[{worker_label}] 예약 최종 완료! 예약번호: {booking_number}"
                                if booking_number
                                else f"[{worker_label}] 예약 최종 완료! 예약번호는 완료 화면에서 확인해주세요."
                            )
                            try:
                                import webbrowser
                                webbrowser.open(f"{self.base_url}/layout/res/home.php?go=rev.make.end&num={num}&ck_code={completion_code}")
                            except Exception:
                                pass
                            try:
                                append_history({
                                    "timestamp": time.strftime('%Y-%m-%d %H:%M:%S', time.localtime()),
                                    "site": "둠이스케이프",
                                    "date": rev_days,
                                    "time": target_time,
                                    "booking_number": booking_number,
                                })
                            except Exception:
                                pass
                        else:
                            self._write_safe_failure_summary(
                                worker=worker_label,
                                stage="무통장 예약 결과 판정",
                                status=mutong_status,
                                response_text=mutong_text,
                                slot_id=slot_id,
                                order_id=num,
                            )
                            final_msg = (
                                f"[{worker_label}] 예약 선점 성공! 예약번호: {booking_number} / 임시번호: {num} "
                                "(결제확인 응답 재확인 필요)"
                                if booking_number
                                else f"[{worker_label}] 예약 선점 성공! 임시번호: {num} (결제확인 응답 재확인 필요)"
                            )
                    else:
                        err_msg = "선점 실패"
                        alert_match = re.search(r"alert\s*\(\s*['\"](.*?)['\"]\s*\)", act_text)
                        if alert_match:
                            err_msg = alert_match.group(1)
                        raise Exception(err_msg)

                    self.log(f"🎉 {final_msg}", "success")
                    self.notify_success()
                    break

                finally:
                    if hasattr(self, "submission_lock"):
                        try:
                            self.submission_lock.release()
                        except RuntimeError:
                            pass

            except Exception as e:
                if self.stop_event.is_set():
                    break
                err_str = self._describe_exception(e)
                now = time.time()
                show_log = False
                with self._log_lock:
                    if err_str != self._last_err_msg or (now - self._last_err_time) > 3.0:
                        self._last_err_msg = err_str
                        self._last_err_time = now
                        show_log = True
                if show_log and not self.stop_event.is_set():
                    retry_text = (
                        "서버 복구 확인 후 재시도"
                        if self._is_transient_site_error(e)
                        else "100ms 후 재시도"
                    )
                    self.log(
                        f"[{worker_label}] [오류] {current_stage} · {err_str} · {retry_text}",
                        "error",
                    )
                
                if self._is_transient_site_error(e):
                    try:
                        await session.close()
                    except Exception:
                        pass

                    session = aiohttp.ClientSession(headers=self.REQUEST_HEADERS)
                    if hasattr(self, "session_pool") and len(self.session_pool) > 0:
                        local_idx = task_idx % len(self.session_pool)
                        self.session_pool[local_idx] = session

                    if self.stop_event.is_set():
                        break
                    await self._wait_for_site_recovery(
                        list_url, task_idx, e, stage=current_stage
                    )
                else:
                    await asyncio.sleep(0.1)
                
        is_pooled = hasattr(self, "session_pool") and len(self.session_pool) > 0
        if not is_pooled:
            try:
                await session.close()
            except Exception:
                pass

    async def pre_fetch_sessions_async(self, num_sessions, reservation_data):
        self._reset_async_recovery_state()
        self.session_pool = []
        self.log(f"Pre-fetching {num_sessions} sessions for Doom Escape...", "info")
        home_url = f"{self.base_url}/layout/res/home.php?go=main"

        async def warm_one():
            session = aiohttp.ClientSession(headers=self.REQUEST_HEADERS)
            warmed = False
            status = None
            error = ""
            started = time.perf_counter()
            try:
                async with session.get(
                    home_url, timeout=self.REQUEST_TIMEOUT_SECONDS
                ) as response:
                    await response.read()
                    status = response.status
                    warmed = response.status == 200
            except Exception as exc:
                error = self._describe_exception(exc)
            return session, warmed, status, time.perf_counter() - started, error

        results = await asyncio.gather(
            *(warm_one() for _ in range(num_sessions))
        )
        self.session_pool = [session for session, _warmed, _status, _rtt, _error in results]
        warmed_count = sum(
            1 for _session, warmed, _status, _rtt, _error in results if warmed
        )
        status_counts = {}
        for _session, _warmed, status, _rtt, error in results:
            label = str(status) if status is not None else (error or "연결 실패")
            status_counts[label] = status_counts.get(label, 0) + 1
        status_summary = ", ".join(
            f"{key}={value}" for key, value in sorted(status_counts.items())
        )
        max_rtt = max((rtt for _session, _warmed, _status, rtt, _error in results), default=0.0)
        if warmed_count == num_sessions:
            self.log(
                f"[정보] 둠이스케이프 연결 {warmed_count}/{num_sessions}개 병렬 예열 완료 · "
                f"HTTP {status_summary} · 최대 RTT {max_rtt * 1000:.0f}ms",
                "info",
            )
        else:
            self.log(
                f"[경고] 둠이스케이프 연결 예열 {warmed_count}/{num_sessions}개 완료 · "
                f"HTTP {status_summary} · 최대 RTT {max_rtt * 1000:.0f}ms · "
                "서버가 복구되면 자동으로 전체 작업을 재개합니다.",
                "warning",
            )
