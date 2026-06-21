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
            
            # 주소가 로그인 페이지 도메인으로 바뀔 때까지 최대 5초 대기 (이전 주소 오진 방지)
            try:
                await page.wait_for_url("**/nidlogin**", timeout=5000)
            except Exception:
                pass
            
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
                    await asyncio.sleep(1)
                    continue
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
            target_open_timestamp = None

            while not self.stop_event.is_set():
                attempt += 1
                self.silent_tick(f"{target_time} 대기")
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
                            self.log(f"⚠️ [{worker_id}번 기기] '동의하고 예약하기' 클릭 실패: {str(e)[:40]}", "warning")
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
                            # 단 한 번의 evaluate 호출로 모든 variant 요소의 상태를 읽어옴 (CDP roundtrip 병목 제거)
                            # has-text와 같은 Playwright 고유 선택자는 JS의 querySelector에서 사용할 수 없으므로, 표준 태그 선택 후 JS 단에서 매칭을 수행합니다.
                            js_time_scan = """
                            ([timeVariants, targetTime]) => {
                                const els = Array.from(document.querySelectorAll('button, a, li'));
                                return els.map((el, idx) => {
                                    const txt = (el.innerText || "").trim();
                                    
                                    // 1. 시간 패턴이 1개만 존재하는지 확인하여 전체 시간 목록을 담고 있는 컨테이너 요소 제외
                                    const timeMatches = txt.match(/\\d{1,2}\\s*:\\s*\\d{2}/g) || [];
                                    if (timeMatches.length !== 1) return null;
                                    
                                    // 2. 시간 파싱
                                    const m = txt.match(/(\\d{1,2})\\s*:\\s*(\\d{2})/);
                                    if (!m) return null;
                                    const hour = parseInt(m[1], 10);
                                    const minute = m[2];
                                    const rawTimeStr = `${hour}:${minute}`;
                                    
                                    // 3. 오전/오후 12시간제와 24시간제 매칭 로직 처리
                                    let normalizedTime = null;
                                    if (txt.includes("오후") && hour < 12) {
                                        normalizedTime = `${hour + 12}:${minute}`;
                                    } else if (txt.includes("오전") && hour === 12) {
                                        normalizedTime = `0:${minute}`;
                                    } else if (txt.includes("오전") || txt.includes("오후")) {
                                        normalizedTime = `${hour}:${minute}`;
                                    }
                                    
                                    let isMatch = false;
                                    if (normalizedTime !== null) {
                                        const targetParts = targetTime.split(":");
                                        const targetH = parseInt(targetParts[0], 10);
                                        const targetM = targetParts[1];
                                        const targetNormalized = `${targetH}:${targetM}`;
                                        isMatch = (normalizedTime === targetNormalized);
                                    } else {
                                        // 4. 오전/오후 텍스트가 없는 경우 variants와 정확하게 일치하는지 비교 (18:00가 8:00에 오매칭되는 것 방지)
                                        isMatch = timeVariants.includes(rawTimeStr);
                                    }
                                    
                                    if (!isMatch) return null;
                                    
                                    const rect = el.getBoundingClientRect();
                                    const visible = !!(rect.top || rect.bottom || rect.width || rect.height);
                                    if (!visible) return null;
                                    return {
                                        idx: idx,
                                        txt: txt,
                                        cls: el.className || "",
                                        aria: el.getAttribute("aria-disabled") || "",
                                        disabled: el.hasAttribute("disabled")
                                    };
                                }).filter(Boolean);
                            }
                            """
                            scan_res = await page.evaluate(js_time_scan, [time_variants, target_time])
                            for item in scan_res:
                                txt = item["txt"]
                                cls = item["cls"]
                                aria = item["aria"]
                                disabled = item["disabled"]
                                if (
                                    "매진" not in txt
                                    and "disabled" not in cls.lower()
                                    and aria != "true"
                                    and not disabled
                                ):
                                    time_el = page.evaluate_handle('document.querySelectorAll("button, a, li")[' + str(item["idx"]) + ']').as_element()
                                    match = re.search(r'\d{1,2}\s*:\s*\d{2}', txt)
                                    if match:
                                        selected_time_str = match.group(0)
                                    break
                        except Exception as scan_err:
                            # 시도 횟수 중복 증가 방지를 위해 silent_tick 대신 단순 로그만 남깁니다.
                            self.log(f"⚠️ 시간 버튼 evaluate 에러: {scan_err}", "warning")

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
                                        await date_el.click(force=True)
                                        await asyncio.sleep(0.05) # Reduced sleep
                                        await wait_for_loading()
                                        date_clicked = True
                            except Exception:
                                pass

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
                            
                            
                            # 새로고침 규칙:
                            # 1. 이미 날짜 클릭이 성공해서 시간표가 떠 있어야 하는 상황(`date_clicked = True`)이라면 새로고침 절대 금지!
                            # 2. 날짜가 아직 비활성화 상태인 경우, 정각 오픈 시간 근처(앞뒤 15초)인 경우에만 1.2초마다 새로고침 시도
                            # 3. 그 외 평시에는 새로고침(page.reload) 없이 계속 날짜 활성화 탐색/클릭 시도
                            if not date_clicked:
                                is_near_open_time = False
                                if target_open_timestamp is not None:
                                    server_now = time.time() + naver_time_offset
                                    # 타겟 오픈 시각 기준 전 15초 ~ 후 15초 사이일 때 집중 새로고침 세션 가동
                                    is_near_open_time = abs(server_now - target_open_timestamp) <= 15.0
                                
                                if is_near_open_time:
                                    now = time.time()
                                    if now - last_reload_time >= 1.2:
                                        self.log(f"🔄 [{worker_id}번 기기] 오픈 임박! 날짜 대기 새로고침 (시도: {attempt}회)", "info")
                                        last_reload_time = now
                                        await page.reload()
                                        await page.wait_for_load_state("domcontentloaded")
                                        await wait_for_loading()
                                    else:
                                        # 새로고침 쿨다운 대기
                                        await asyncio.sleep(0.02)
                                else:
                                    # 평시: 새로고침 없이 고속으로 날짜 탐색/클릭 진행 (CPU 절약용 미세 딜레이)
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
                            self.log(f"⚠️ [{worker_id}번 기기] '다음' 버튼 미발견", "warning")
                            continue

                except Exception as e:
                    self.log(f"⚠️ [{worker_id}번 기기] 루프 에러: {str(e)[:60]}", "warning")
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
