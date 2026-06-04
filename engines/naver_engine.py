import asyncio
import os
import json
import threading
import time
import urllib.parse
from datetime import datetime
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

        # Try to stop loop if running
        if self.loop and self.loop.is_running():
            try:
                self.loop.call_soon_threadsafe(self.loop.stop)
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
            self.log("예약 백그라운드 태스크 기동 중...", "info")
            self.browser = await self._launch_browser(p, headless=True)

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

            # Build booking URL with startDateTime to pre-select the target date
            start_dt = f"{target_date}T00:00:00+09:00"
            sep = "&" if "?" in url else "?"
            booking_url = f"{url}{sep}startDateTime={urllib.parse.quote(start_dt)}"

            await page.goto(booking_url)
            await page.wait_for_load_state("domcontentloaded")
            self.log(
                f"[{worker_id}번 기기] 페이지 로드 완료 "
                f"(날짜: {target_date}, 시간: {target_time}, 인원: {people}인)",
                "info"
            )

            attempt = 0
            while not self.stop_event.is_set():
                attempt += 1
                try:
                    # ── Detect which page we are on ──────────────
                    on_request_page = False
                    try:
                        submit_loc = page.locator(
                            'button:has-text("동의하고 예약하기"), '
                            'a:has-text("동의하고 예약하기")'
                        ).first
                        on_request_page = await submit_loc.is_visible(timeout=300)
                    except Exception:
                        pass

                    if on_request_page:
                        # ═══════════════════════════════════════════
                        #  REQUEST / CONFIRMATION PAGE  (Phases 3-5)
                        # ═══════════════════════════════════════════

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
                                        await asyncio.sleep(0.2)
                                        # Click the matching option
                                        opt = page.locator(
                                            f'li:has-text("{target_label}"), '
                                            f'div:has-text("{target_label}"), '
                                            f'span:has-text("{target_label}")'
                                        ).first
                                        if await opt.is_visible(timeout=400):
                                            await opt.click()
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
                                        await asyncio.sleep(0.2)
                                        opt = page.locator(
                                            f'li:has-text("{target_label}"), '
                                            f'div:has-text("{target_label}"), '
                                            f'span:has-text("{target_label}")'
                                        ).first
                                        if await opt.is_visible(timeout=400):
                                            await opt.click()
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

                        # Phase 1 — Find & click target time slot ──
                        time_el = None
                        for tv in time_variants:
                            for sel in [
                                f'button:has-text("{tv}")',
                                f'a:has-text("{tv}")',
                                f'li:has-text("{tv}")',
                            ]:
                                try:
                                    el = page.locator(sel).first
                                    if await el.is_visible(timeout=200):
                                        txt = await el.inner_text()
                                        cls = (await el.get_attribute("class")) or ""
                                        aria = (await el.get_attribute("aria-disabled")) or ""
                                        if (
                                            "매진" not in txt
                                            and "disabled" not in cls
                                            and aria != "true"
                                        ):
                                            time_el = el
                                            break
                                except Exception:
                                    continue
                            if time_el:
                                break

                        if not time_el:
                            # 20회 시도(약 3초)마다 로그를 출력하여 재시도가 활발히 진행 중임을 표시
                            if attempt == 1 or attempt % 20 == 0:
                                self.log(
                                    f"⚠️ [{worker_id}번 기기] {target_time} 비활성화/매진 — 재시도 중... (시도: {attempt}회)",
                                    "warning"
                                )
                            else:
                                self.silent_tick(
                                    f"{target_time} 비활성화/매진 — 대기"
                                )
                            # Periodic reload to refresh timetable
                            if attempt % 30 == 0:
                                await page.reload()
                                await page.wait_for_load_state("domcontentloaded")
                            else:
                                await asyncio.sleep(0.15)
                            continue

                        self.log(
                            f"✓ [{worker_id}번 기기] {target_time} 활성화 감지! "
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
