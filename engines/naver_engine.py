import asyncio
import os
import json
import threading
import time
import urllib.parse
from datetime import datetime, timedelta
from engines.base_engine import BaseEngine

class NaverEngine(BaseEngine):
    def __init__(self, log_callback, success_callback=None, status_callback=None, log_batch_callback=None):
        """
        Playwright-based parallel Naver Reservation Engine.
        """
        super().__init__(log_callback, success_callback, status_callback, log_batch_callback)
        self.playwright_thread = None
        self.loop = None
        self.browser = None
        self.cookies_path = "naver_cookies.json"

    def start_reservation(self, reservation_data, num_threads, is_async=False):
        if self.is_running:
            self.log("예약 엔진이 이미 실행 중입니다.", "warning")
            return

        self.is_running = True
        self.stop_event.clear()
        self._attempt_count = 0
        self._seen_errors = set()

        self.log(f"네이버 (Playwright) 예약을 시작합니다. (병렬 인스턴스: {num_threads}개)", "info")

        # Start Playwright automation loop in a background thread
        self.playwright_thread = threading.Thread(
            target=self._run_playwright_loop,
            args=(reservation_data, num_threads),
            name="NaverPlaywrightThread"
        )
        self.playwright_thread.daemon = True
        self.playwright_thread.start()

        # Monitor thread
        monitor = threading.Thread(target=self._monitor_playwright_thread, name="NaverMonitorThread")
        monitor.daemon = True
        monitor.start()

    def _monitor_playwright_thread(self):
        if self.playwright_thread:
            self.playwright_thread.join()
        self.is_running = False
        self.log("네이버 예약 작업이 중단되었습니다.", "info")

    def stop_reservation(self):
        if not self.is_running:
            return
        self.log("네이버 예약을 정지합니다...", "info")
        self.stop_event.set()

        # Try to close browser asynchronously inside the loop threadsafe first
        if self.browser and self.loop and self.loop.is_running():
            try:
                asyncio.run_coroutine_threadsafe(self.browser.close(), self.loop)
            except Exception:
                pass

    def _run_playwright_loop(self, reservation_data, num_threads):
        # Create a new event loop for this background thread
        self.loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self.loop)
        try:
            self.loop.run_until_complete(self._run_async_tasks(reservation_data, num_threads))
        except Exception as e:
            self.log(f"비동기 실행 루프 에러: {e}", "error")
        finally:
            self.loop.close()
            self.loop = None

    async def _launch_browser(self, playwright, headless=True):
        browser = None
        errors = []
        for channel in ["chrome", "msedge", None]:
            try:
                if channel:
                    browser = await playwright.chromium.launch(channel=channel, headless=headless)
                else:
                    browser = await playwright.chromium.launch(headless=headless)
                if browser:
                    return browser
            except Exception as e:
                errors.append(f"{channel or 'default'}: {e}")
        
        raise Exception(f"브라우저 실행 실패 (에러 목록: {', '.join(errors)})")

    async def _handle_login(self, playwright, custom_url):
        self.log("네이버 로그인 상태를 점검하는 중...", "info")
        
        # We need a browser with headless=False to allow user interaction for login
        browser = await self._launch_browser(playwright, headless=False)
        context = await browser.new_context()
        page = await context.new_page()

        # Load existing cookies if present
        cookies = []
        if os.path.exists(self.cookies_path):
            try:
                with open(self.cookies_path, "r", encoding="utf-8") as f:
                    cookies = json.load(f)
                    await context.add_cookies(cookies)
            except Exception:
                pass

        # Go to booking page or Naver home to check if logged in
        await page.goto("https://booking.naver.com")
        await page.wait_for_load_state("networkidle")

        # Simple check: see if login button is present
        login_btn_visible = False
        try:
            # Login link selectors
            login_selectors = ["a[href*='nid.naver.com/nidlogin']", ".gnb_btn_login", "text='로그인'"]
            for selector in login_selectors:
                if await page.locator(selector).is_visible():
                    login_btn_visible = True
                    break
        except Exception:
            pass

        if login_btn_visible or not cookies:
            self.log("⚠️ 네이버 로그인이 필요합니다. 창에서 로그인을 완료해주세요.", "warning")
            # Redirect to login page
            await page.goto("https://nid.naver.com/nidlogin.login?url=" + custom_url)
            
            # Wait for user to login and navigate back to booking domain
            login_success = False
            for _ in range(300): # Wait up to 5 minutes
                if self.stop_event.is_set():
                    break
                try:
                    current_url = page.url
                    if "booking.naver.com" in current_url and "nidlogin" not in current_url:
                        login_success = True
                        break
                except Exception:
                    break
                await asyncio.sleep(1)

            if login_success:
                # Save new cookies
                new_cookies = await context.cookies()
                with open(self.cookies_path, "w", encoding="utf-8") as f:
                    json.dump(new_cookies, f, ensure_ascii=False, indent=2)
                self.log("✓ 로그인 완료! 세션 쿠키를 성공적으로 저장했습니다.", "success")
            else:
                self.log("❌ 로그인 제한시간이 초과되었거나 취소되었습니다.", "error")
                await browser.close()
                return False
        else:
            self.log("✓ 네이버 세션이 유효합니다. 백그라운드 예약을 시작합니다.", "success")

        await browser.close()
        return True

    async def _run_async_tasks(self, reservation_data, num_threads):
        from playwright.async_api import async_playwright
        
        custom_url = reservation_data.get('themePK') # In custom sites parsing, 'themePK' holds the Naver URL
        if not custom_url or "booking.naver.com" not in custom_url:
            self.log("❌ 유효한 네이버 예약 URL이 아닙니다.", "error")
            return

        async with async_playwright() as p:
            # Step 1: Login verification (Interactive window)
            success = await self._handle_login(p, custom_url)
            if not success or self.stop_event.is_set():
                return

            # Read saved cookies
            cookies = []
            if os.path.exists(self.cookies_path):
                try:
                    with open(self.cookies_path, "r", encoding="utf-8") as f:
                        cookies = json.load(f)
                except Exception as e:
                    self.log(f"쿠키 파일 읽기 실패: {e}", "error")
                    return

            # Step 2: Parallel Headless Booking attempts
            is_dev = reservation_data.get('devMode', False)
            self.log(f"예약 백그라운드 태스크 기동 중... (개발자 모드: {is_dev})", "info")
            self.browser = await self._launch_browser(p, headless=not is_dev)

            tasks = []
            for i in range(num_threads):
                tasks.append(
                    self._booking_worker_task(i + 1, custom_url, cookies, reservation_data)
                )

            await asyncio.gather(*tasks)

            # Close browser when all tasks complete
            if self.browser:
                await self.browser.close()
                self.browser = None

    def _get_time_variants(self, time_str):
        """Convert HH:MM to possible Naver Booking display format variants.
        
        Naver displays times in Korean 12-hour format:
          - 12:10 → "오후 12:10"
          - 13:20 → "오후 1:20"
          - 09:30 → "오전 9:30"
        We generate search strings that will match via has-text().
        """
        parts = time_str.split(":")
        hour = int(parts[0])
        minute = parts[1]

        variants = []
        # Original format
        variants.append(f"{hour}:{minute}")
        # With leading zero
        variants.append(f"{hour:02d}:{minute}")
        # 12-hour conversion for PM hours (13–23)
        if hour > 12:
            variants.append(f"{hour - 12}:{minute}")
        elif hour == 0:
            variants.append(f"12:{minute}")

        # Deduplicate while preserving order
        seen = set()
        unique = []
        for v in variants:
            if v not in seen:
                seen.add(v)
                unique.append(v)
        return unique

    def _time_to_minutes(self, time_str):
        import re
        is_pm = "오후" in time_str
        match = re.search(r'(\d{1,2})\s*:\s*(\d{2})', time_str)
        if not match:
            return None
        hour = int(match.group(1))
        minute = int(match.group(2))
        if is_pm and hour < 12:
            hour += 12
        elif "오전" in time_str and hour == 12:
            hour = 0
        return hour * 60 + minute

    async def _booking_worker_task(self, worker_id, url, cookies, res_data):
        """
        Complete Naver Booking automation:
          Phase 1: Navigate to page → find & click target time slot
          Phase 2: Click "다음" (Next)
          Phase 3: Select "참여인원 설정" (participant count) dropdown
          Phase 4: Click "동의하고 예약하기" (Agree & Book)
          Phase 5: Verify booking success
        """
        context = None
        try:
            context = await self.browser.new_context(
                viewport={"width": 1280, "height": 800},
                user_agent=(
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/120.0.0.0 Safari/537.36"
                )
            )
            await context.add_cookies(cookies)
            page = await context.new_page()

            # Auto-dismiss native alert / confirm dialogs
            async def handle_dialog(dialog):
                msg = dialog.message[:80] if dialog.message else ""
                self.log(f"⚠️ [{worker_id}번 기기] 팝업: {msg}", "warning")
                await dialog.dismiss()
            page.on("dialog", handle_dialog)

            page.set_default_timeout(5000)
            page.set_default_navigation_timeout(10000)

            target_date = res_data.get("reservationDate")      # YYYY-MM-DD
            target_time = res_data.get("reservationTime")[:5]  # HH:MM
            people = res_data.get("people", "2")
            time_variants = self._get_time_variants(target_time)

            # Helper to wait for Naver loading indicator to disappear
            async def wait_for_loading():
                try:
                    loader = page.locator(".loading_area").first
                    if await loader.is_visible(timeout=0):
                        await loader.wait_for(state="hidden", timeout=3000)
                except Exception:
                    pass

            # Build booking URL with startDateTime to pre-select the target date
            start_dt = f"{target_date}T00:00:00+09:00"
            sep = "&" if "?" in url else "?"
            booking_url = f"{url}{sep}startDateTime={urllib.parse.quote(start_dt)}"

            await page.goto(booking_url)
            await page.wait_for_load_state("domcontentloaded")
            await wait_for_loading()
            self.log(
                f"[{worker_id}번 기기] 페이지 로드 완료 "
                f"(날짜: {target_date}, 시간: {target_time}, 인원: {people}인)",
                "info"
            )

            attempt = 0
            date_clicked = False
            last_reload_time = 0.0
            has_awaited_open = False
            naver_time_offset = res_data.get('naver_time_offset', 0.0)

            while not self.stop_event.is_set():
                attempt += 1
                try:
                    # ── Detect which page we are on ──────────────
                    on_request_page = False
                    current_url = page.url
                    if "booking-request" in current_url or "request" in current_url or "step2" in current_url:
                        try:
                            submit_loc = page.locator(
                                'button:has-text("동의하고 예약하기"), '
                                'a:has-text("동의하고 예약하기")'
                            ).first
                            on_request_page = await submit_loc.is_visible(timeout=0)
                        except Exception:
                            pass

                    if on_request_page:
                        # ═══════════════════════════════════════════
                        #  REQUEST / CONFIRMATION PAGE  (Phases 3-5)
                        # ═══════════════════════════════════════════

                        # Debug: Save page source in developer/test mode to analyze DOM
                        if res_data.get('devMode', False) and worker_id == 1:
                            try:
                                debug_html = await page.content()
                                debug_path = r"C:\Users\Administrator\.gemini\antigravity\brain\1de2937b-3d9d-446d-87ed-b04f2d021bc6\scratch\naver_request_debug.html"
                                with open(debug_path, "w", encoding="utf-8") as f:
                                    f.write(debug_html)
                                self.log(f"✓ [{worker_id}번 기기] [디버그] 네이버 요청 페이지 HTML 저장 완료: {debug_path}", "info")
                            except Exception as e:
                                self.log(f"디버그 HTML 저장 실패: {e}", "warning")

                        # Phase 3 — Select 참여인원 설정 dropdown ──
                        target_label = f"{people}인"
                        participant_ok = False

                        # Strategy A: native <select> element
                        try:
                            for sel_el in await page.locator("select").all():
                                opts = await sel_el.locator("option").all_text_contents()
                                if any(target_label in o for o in opts):
                                    await sel_el.select_option(label=target_label)
                                    participant_ok = True
                                    break
                        except Exception:
                            pass

                        # Strategy B: custom React dropdown (by text trigger)
                        if not participant_ok:
                            trigger_texts = [
                                "해당하는 항목을 선택해주세요.",
                                "참여인원 설정",
                                "선택해주세요",
                            ]
                            for tt in trigger_texts:
                                try:
                                    trigger = page.locator(f'text="{tt}"').first
                                    if await trigger.is_visible(timeout=300):
                                        await trigger.click()
                                        await asyncio.sleep(0.3)
                                        
                                        # Find option (prioritize dropdown lists to avoid choosing standard desc texts)
                                        opt = None
                                        selectors = [
                                            f'[class*="dropdown"] li:has-text("{target_label}"):visible',
                                            f'[class*="Dropdown"] li:has-text("{target_label}"):visible',
                                            f'[class*="dropdown"] div:has-text("{target_label}"):visible',
                                            f'[class*="Dropdown"] div:has-text("{target_label}"):visible',
                                            f'[class*="list"] li:has-text("{target_label}"):visible',
                                            f'[class*="list"] div:has-text("{target_label}"):visible',
                                            f'li:has-text("{target_label}"):visible',
                                            f'div:has-text("{target_label}"):visible',
                                            f'span:has-text("{target_label}"):visible',
                                            f'text="{target_label}":visible'
                                        ]
                                        
                                        for opt_sel in selectors:
                                            try:
                                                cand = page.locator(opt_sel).first
                                                if await cand.is_visible(timeout=200):
                                                    opt = cand
                                                    break
                                            except Exception:
                                                continue
                                                
                                        if not opt:
                                            opt = page.locator(
                                                f'li:has-text("{target_label}"):visible, '
                                                f'div:has-text("{target_label}"):visible, '
                                                f'span:has-text("{target_label}"):visible, '
                                                f'text="{target_label}":visible'
                                            ).first
                                        
                                        await opt.wait_for(state="visible", timeout=2000)
                                        try:
                                            await opt.scroll_into_view_if_needed(timeout=400)
                                        except Exception:
                                            pass
                                        
                                        await opt.click(force=True)
                                        participant_ok = True
                                        break
                                except Exception:
                                    continue

                        # Strategy C: custom React dropdown (by class container)
                        if not participant_ok:
                            for sel in [
                                '.UserDropdown__dropdown_area__aOccG',
                                '[class*="UserDropdown__dropdown_area"]',
                                '.booking_user_request'
                            ]:
                                try:
                                    trigger = page.locator(sel).first
                                    if await trigger.is_visible(timeout=300):
                                        await trigger.click()
                                        await asyncio.sleep(0.3)
                                        
                                        opt = None
                                        selectors = [
                                            f'[class*="dropdown"] li:has-text("{target_label}"):visible',
                                            f'[class*="Dropdown"] li:has-text("{target_label}"):visible',
                                            f'[class*="dropdown"] div:has-text("{target_label}"):visible',
                                            f'[class*="Dropdown"] div:has-text("{target_label}"):visible',
                                            f'[class*="list"] li:has-text("{target_label}"):visible',
                                            f'[class*="list"] div:has-text("{target_label}"):visible',
                                            f'li:has-text("{target_label}"):visible',
                                            f'div:has-text("{target_label}"):visible',
                                            f'span:has-text("{target_label}"):visible',
                                            f'text="{target_label}":visible'
                                        ]
                                        
                                        for opt_sel in selectors:
                                            try:
                                                cand = page.locator(opt_sel).first
                                                if await cand.is_visible(timeout=200):
                                                    opt = cand
                                                    break
                                            except Exception:
                                                continue
                                                
                                        if not opt:
                                            opt = page.locator(
                                                f'li:has-text("{target_label}"):visible, '
                                                f'div:has-text("{target_label}"):visible, '
                                                f'span:has-text("{target_label}"):visible, '
                                                f'text="{target_label}":visible'
                                            ).first
                                        
                                        await opt.wait_for(state="visible", timeout=2000)
                                        try:
                                            await opt.scroll_into_view_if_needed(timeout=400)
                                        except Exception:
                                            pass
                                        
                                        await opt.click(force=True)
                                        participant_ok = True
                                        break
                                except Exception:
                                    continue

                        if participant_ok:
                            self.log(
                                f"✓ [{worker_id}번 기기] 참여인원 {target_label} 선택 완료",
                                "info"
                            )
                            await asyncio.sleep(0.2)

                        # Phase 4a — Check agreement checkboxes & custom divs ────
                        # 1. Standard HTML checkbox inputs
                        try:
                            cbs = await page.locator("input[type='checkbox']").all()
                            for cb in cbs:
                                try:
                                    if not await cb.is_checked():
                                        await cb.check(force=True)
                                except Exception:
                                    pass
                        except Exception:
                            pass

                        # 2. Custom agreement divs/texts (which don't use standard input elements)
                        try:
                            custom_cb = None
                            for sel in [
                                '.AgreementDesc__section_inner__Ny+MK',
                                'text="예약 서비스 이용을 위한 개인정보 제3자 제공 규정을 확인하였으며 이에 동의합니다."',
                                '.AgreementDesc__section_agreement_info__hMLOx'
                            ]:
                                try:
                                    el = page.locator(sel).first
                                    if await el.is_visible(timeout=300):
                                        custom_cb = el
                                        break
                                except Exception:
                                    continue
                            
                            if custom_cb:
                                await custom_cb.click(force=True)
                                await asyncio.sleep(0.15)
                        except Exception:
                            pass

                        # Phase 4b — Click "동의하고 예약하기" (Wait for activation if needed) ──
                        try:
                            submit = page.locator(
                                'button:has-text("동의하고 예약하기"), '
                                'a:has-text("동의하고 예약하기")'
                            ).first

                            # Wait up to 1.5s (15 * 100ms) for 'disabled' class to clear
                            activated = False
                            cls = ""
                            for _ in range(15):
                                cls = (await submit.get_attribute("class")) or ""
                                if "disabled" not in cls:
                                    activated = True
                                    break
                                await asyncio.sleep(0.1)

                            if not activated:
                                self.log(
                                    f"⚠️ [{worker_id}번 기기] '동의하고 예약하기' 버튼 비활성화 지속 (클래스: {cls})",
                                    "warning"
                                )

                            # Developer mode: bypass actual submission click and idle
                            if res_data.get('devMode', False):
                                self.log(
                                    f"✓ [{worker_id}번 기기] [개발자 테스트] '동의하고 예약하기' 버튼 직전 멈춤! (제출 우회)",
                                    "success"
                                )
                                while not self.stop_event.is_set():
                                    await asyncio.sleep(0.5)
                                break

                            await submit.click()
                            self.log(
                                f"🚀 [{worker_id}번 기기] '동의하고 예약하기' 클릭!",
                                "warning"
                            )
                        except Exception as e:
                            self.silent_tick(f"'동의하고 예약하기' 클릭 실패: {str(e)[:40]}")
                            await page.goto(booking_url)
                            await page.wait_for_load_state("domcontentloaded")
                            continue

                        # Phase 5 — Verify booking result ─────────
                        try:
                            await page.wait_for_load_state("networkidle", timeout=5000)
                        except Exception:
                            pass
                        await asyncio.sleep(0.5)

                        cur = page.url
                        body_text = ""
                        try:
                            body_text = await page.locator("body").inner_text(timeout=2000)
                        except Exception:
                            pass

                        success_url = ["booking-detail", "success", "complete", "my/"]
                        success_txt = ["예약이 완료", "예약되었습니다", "예약 완료"]

                        if (
                            any(k in cur for k in success_url)
                            or any(k in body_text for k in success_txt)
                        ):
                            self.log(
                                f"🎉 [{worker_id}번 기기] 네이버 예약 성공!! URL: {cur}",
                                "success"
                            )
                            self.stop_event.set()
                            if self.success_callback:
                                self.success_callback()
                            break
                        else:
                            self.log(
                                f"⚠️ [{worker_id}번 기기] 제출 후 재확인 — {cur}",
                                "warning"
                            )
                            # Return to booking page and retry
                            await page.goto(booking_url)
                            await page.wait_for_load_state("domcontentloaded")
                            continue

                    else:
                        # ═══════════════════════════════════════════
                        #  BOOKING PAGE  (Phases 1-2)
                        # ═══════════════════════════════════════════

                        # 1. 정각 오픈 전 대기 로직 (하이브리드 대기 - 최초 1회 판별)
                        if not has_awaited_open:
                            server_now = time.time() + naver_time_offset
                            dt_now = datetime.fromtimestamp(server_now)
                            
                            # Calculate next 30-minute interval target
                            minutes_to_add = 30 - (dt_now.minute % 30)
                            dt_target = dt_now.replace(second=0, microsecond=0)
                            if dt_now.minute % 30 != 0 or dt_now.second > 0:
                                dt_target += timedelta(minutes=minutes_to_add)
                                
                            target_open_timestamp = dt_target.timestamp()
                            
                            # Warm-up targets: wake up 10.0 seconds before target_open_timestamp
                            sleep_target = target_open_timestamp - 10.0
                            time_left_to_warmup = sleep_target - server_now
                            
                            # If we are within 180 seconds (3 minutes) before target open time and not yet at warmup point
                            if 0 < (target_open_timestamp - server_now) <= 180 and server_now < sleep_target:
                                self.log(
                                    f"⏰ [{worker_id}번 기기] 정각 오픈 대기 중... "
                                    f"(서버시간: {dt_now.strftime('%H:%M:%S')}, "
                                    f"워밍업 예정: {datetime.fromtimestamp(sleep_target).strftime('%H:%M:%S')}, "
                                    f"남은대기시간: {time_left_to_warmup:.1f}초)",
                                    "info"
                                )
                                
                                while True:
                                    now = time.time() + naver_time_offset
                                    if now >= sleep_target or self.stop_event.is_set():
                                        break
                                        
                                    rem = sleep_target - now
                                    if rem > 3.0:
                                        # Relaxed sleep (0.5s) to prevent CPU lag
                                        await asyncio.sleep(0.5)
                                    else:
                                        # Fine-grained sleep (0.01s) close to warmup point
                                        await asyncio.sleep(0.01)
                                    
                                if not self.stop_event.is_set():
                                    self.log(f"🔄 [{worker_id}번 기기] 오픈 10초 전 감지! 1차 새로고침 실행 (워밍업)...", "info")
                                    await page.reload()
                                    await page.wait_for_load_state("domcontentloaded")
                                    await wait_for_loading()
                                    
                            has_awaited_open = True

                        # Phase 1 — Find & click target time slot ──
                        time_el = None
                        selected_time_str = target_time
                        
                        # Build combined selector to query only once per loop iteration
                        selectors = []
                        for tv in time_variants:
                            selectors.extend([
                                f'button:has-text("{tv}")',
                                f'a:has-text("{tv}")',
                                f'li:has-text("{tv}")'
                            ])
                        combined_sel = ", ".join(selectors)
                        
                        try:
                            import re
                            elements = await page.locator(combined_sel).all()
                            for el in elements:
                                if await el.is_visible(timeout=0):
                                    txt = await el.inner_text()
                                    cls = (await el.get_attribute("class")) or ""
                                    aria = (await el.get_attribute("aria-disabled")) or ""
                                    disabled_attr = await el.get_attribute("disabled")
                                    if (
                                        "매진" not in txt
                                        and "disabled" not in cls.lower()
                                        and aria != "true"
                                        and disabled_attr is None
                                    ):
                                        time_el = el
                                        # Parse text to set selected_time_str
                                        match = re.search(r'\d{1,2}\s*:\s*\d{2}', txt)
                                        if match:
                                            selected_time_str = match.group(0)
                                        break
                        except Exception:
                            pass

                        # --- [대체 시간대(차선책) 탐색 및 새로고침 정지 로직 - v4.1] ---
                        if not time_el:
                            active_slots = []
                            try:
                                # 시간 포맷(콜론)을 가지며 화면에 보이는 활성 버튼/링크 탐색
                                locators = await page.locator('button:has-text(":"):visible, a:has-text(":"):visible').all()
                                for el in locators:
                                    txt = await el.inner_text()
                                    import re
                                    if re.search(r'\d{1,2}\s*:\s*\d{2}', txt):
                                        cls = (await el.get_attribute("class")) or ""
                                        aria = (await el.get_attribute("aria-disabled")) or ""
                                        disabled_attr = await el.get_attribute("disabled")
                                        if (
                                            "매진" not in txt
                                            and "종료" not in txt
                                            and "disabled" not in cls.lower()
                                            and aria != "true"
                                            and disabled_attr is None
                                        ):
                                            active_slots.append((el, txt))
                            except Exception as scan_err:
                                self.silent_tick(f"시간 후보군 스캔 에러: {scan_err}")

                            if active_slots:
                                # 다른 시간대는 활성화되어 있으므로, 페이지 새로고침을 멈춤(date_clicked = True 설정)
                                date_clicked = True
                                
                                target_mins = self._time_to_minutes(target_time)
                                if target_mins is not None:
                                    closest_el = None
                                    min_diff = float('inf')
                                    closest_time_str = ""
                                    
                                    for el, txt in active_slots:
                                        slot_mins = self._time_to_minutes(txt)
                                        if slot_mins is not None:
                                            diff = abs(target_mins - slot_mins)
                                            if diff < min_diff:
                                                min_diff = diff
                                                closest_el = el
                                                closest_time_str = txt
                                    
                                    if closest_el:
                                        self.log(
                                            f"⚠️ [{worker_id}번 기기] 지정 시간({target_time}) 매진 감지! "
                                            f"가장 가까운 활성 시간대 '{closest_time_str.strip()}'(오차: {min_diff}분)로 자동 우회 예약을 진행합니다.",
                                            "warning"
                                        )
                                        time_el = closest_el
                                        selected_time_str = closest_time_str.strip()

                        if not time_el:
                            # Wait for loading indicator to clear
                            await wait_for_loading()
                            
                            # Attempt to detect/click target date cell to trigger timetable loading (if date became active)
                            try:
                                dt = datetime.strptime(target_date, "%Y-%m-%d")
                                year, month, day = dt.year, dt.month, dt.day
                                
                                label_variants = [
                                    f"{year}. {month}. {day}.",
                                    f"{year}.{month:02d}.{day:02d}",
                                    f"{year}년 {month}월 {day}일",
                                    f"{month}월 {day}일",
                                    f"{day}일",
                                ]
                                
                                date_el = None
                                for lv in label_variants:
                                    try:
                                        el = page.locator(f'[aria-label*="{lv}"]').first
                                        if await el.is_visible(timeout=0): # 0ms timeout for ultra-fast check
                                            date_el = el
                                            break
                                    except Exception:
                                        continue
                                        
                                if not date_el:
                                    for calendar_sel in ['[class*="calendar"]', '[class*="Calendar"]', '.calendar_area', '.calendar_wrap']:
                                        try:
                                            el = page.locator(calendar_sel).locator(f'text="{day}"').first
                                            if await el.is_visible(timeout=0): # 0ms timeout for ultra-fast check
                                                date_el = el
                                                break
                                        except Exception:
                                            continue
                                            
                                if date_el:
                                    cls = (await date_el.get_attribute("class")) or ""
                                    aria_disabled = (await date_el.get_attribute("aria-disabled")) or ""
                                    aria_selected = (await date_el.get_attribute("aria-selected")) or ""
                                    disabled_attr = await date_el.get_attribute("disabled")
                                    
                                    cls_lower = cls.lower()
                                    # 만약 이미 날짜가 선택되어 활성화된 상태라면 클릭 생략하고 바로 date_clicked = True 설정
                                    is_already_selected = (
                                        "selected" in cls_lower 
                                        or "active" in cls_lower 
                                        or "on" in cls_lower 
                                        or aria_selected == "true"
                                    )
                                    
                                    if is_already_selected:
                                        date_clicked = True
                                    elif "disabled" not in cls_lower and aria_disabled != "true" and disabled_attr is None:
                                        self.silent_tick(f"{target_date} 날짜 활성화 클릭 시도")
                                        await date_el.click(force=True)
                                        await asyncio.sleep(0.05) # Reduced sleep
                                        await wait_for_loading()
                                        date_clicked = True
                            except Exception as date_err:
                                self.silent_tick(f"날짜 클릭 시도 실패: {date_err}")

                            # 만약 날짜 감지/클릭이 완료되었으나 아직 시간 버튼(time_el)이 없다면 미세한 렌더링 시간 대기 (Smart Wait)
                            if date_clicked:
                                try:
                                    # 시간표 렌더링 완료(보이기) 대기 (최대 600ms)
                                    await page.locator('button:has-text(":"):visible, a:has-text(":"):visible').first.wait_for(state="visible", timeout=600)
                                except Exception:
                                    pass

                            # Print retry status
                            if attempt == 1 or attempt % 20 == 0:
                                self.log(
                                    f"[{worker_id}번 기기] {target_time} 비활성화/매진 - 재시도 ({attempt}회)",
                                    "warning"
                                )
                            else:
                                self.silent_tick(
                                    f"{target_time} 대기"
                                )
                            
                            # 새로고침 규칙:
                            # 1. 이미 날짜 클릭이 성공해서 시간표가 떠 있어야 하는 상황(`date_clicked = True`)이라면 새로고침 절대 금지!
                            # 2. 날짜가 아직 비활성화 상태인 경우에만 1.2초마다 새로고침 시도
                            if not date_clicked:
                                now = time.time()
                                if now - last_reload_time >= 1.2:
                                    self.log(f"🔄 [{worker_id}번 기기] 날짜 대기 새로고침 (시도: {attempt}회)", "info")
                                    last_reload_time = now
                                    await page.reload()
                                    await page.wait_for_load_state("domcontentloaded")
                                    await wait_for_loading()
                                else:
                                    # 1.2초 간격 쿨다운 미도달 시 새로고침을 억제하고 CPU 부하 절감을 위해 대기
                                    await asyncio.sleep(0.02)
                            else:
                                # 날짜가 이미 클릭되었으므로, 새로고침 없이 초고속으로 시간 버튼 검사만 수행 (루프 딜레이 최소화)
                                await asyncio.sleep(0.002)
                            continue

                        self.log(
                            f"✓ [{worker_id}번 기기] {selected_time_str} 활성화 감지! "
                            f"클릭 시도...",
                            "warning"
                        )
                        await time_el.click()
                        await asyncio.sleep(0.15)

                        # Phase 2 — Click "다음" button ────────────
                        next_clicked = False
                        for sel in [
                            'a:has-text("다음")',
                            'button:has-text("다음")',
                        ]:
                            try:
                                el = page.locator(sel).first
                                if await el.is_visible(timeout=800):
                                    await el.click()
                                    next_clicked = True
                                    self.log(
                                        f"✓ [{worker_id}번 기기] '다음' 버튼 클릭",
                                        "info"
                                    )
                                    try:
                                        await page.wait_for_load_state(
                                            "domcontentloaded", timeout=5000
                                        )
                                    except Exception:
                                        pass
                                    await asyncio.sleep(0.3)
                                    break
                            except Exception:
                                continue

                        if not next_clicked:
                            self.silent_tick("'다음' 버튼 미발견")
                            continue

                except Exception as e:
                    self.silent_tick(f"에러: {str(e)[:60]}")
                    date_clicked = False
                    last_reload_time = 0.0
                    try:
                        await page.goto(booking_url)
                        await page.wait_for_load_state("domcontentloaded")
                    except Exception:
                        pass
                    await asyncio.sleep(0.3)

        except Exception as e:
            self.log(f"❌ [{worker_id}번 기기] 중단 에러: {e}", "error")
        finally:
            if context:
                await context.close()
