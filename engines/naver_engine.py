import asyncio
import os
import json
import threading
import time
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

    async def _booking_worker_task(self, worker_id, url, cookies, res_data):
        context = None
        try:
            # Isolated context
            context = await self.browser.new_context(
                viewport={"width": 1280, "height": 800},
                user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
            )
            await context.add_cookies(cookies)
            page = await context.new_page()

            # Set short timeouts for aggressive booking attempts
            page.set_default_timeout(5000)
            page.set_default_navigation_timeout(8000)

            target_date = res_data.get("reservationDate") # YYYY-MM-DD
            target_time = res_data.get("reservationTime")[:5] # HH:MM

            # Navigate to booking page
            await page.goto(url)
            await page.wait_for_load_state("domcontentloaded")

            self.log(f"[{worker_id}번 기기] 사이트 로드 완료. 날짜: {target_date}, 시간: {target_time}", "info")

            attempt = 0
            while not self.stop_event.is_set():
                attempt += 1
                try:
                    # 1. Click target date to trigger React SPA timetable refresh
                    # Naver booking dates are represented by 'calendar-date' or data-date attribute
                    # e.g., <a data-date="2026-06-05"> or similar elements
                    date_selectors = [
                        f'a[data-date="{target_date}"]',
                        f'td[data-date="{target_date}"]',
                        f'button[data-date="{target_date}"]',
                        f'div[data-date="{target_date}"]',
                        f'[aria-label*="{target_date}"]',
                    ]
                    
                    date_element = None
                    for sel in date_selectors:
                        try:
                            el = page.locator(sel).first
                            if await el.is_visible():
                                date_element = el
                                break
                        except Exception:
                            continue

                    if not date_element:
                        # Parse day from date (e.g. 2026-06-05 -> 5)
                        day = str(int(target_date.split("-")[2]))
                        fallback_sel = f"//a[contains(text(), '{day}') or span[contains(text(), '{day}')]]"
                        el = page.locator(fallback_sel).first
                        if await el.is_visible():
                            date_element = el

                    if date_element:
                        await date_element.click()
                        # Short delay for AJAX DOM update
                        await asyncio.sleep(0.1)
                    else:
                        self.silent_tick("달력 날짜 요소를 찾을 수 없음")
                        await asyncio.sleep(0.5)
                        continue

                    # 2. Check if the target time is visible and clickable
                    # Time selectors can vary, but generally contain the time string in button or list items
                    time_selectors = [
                        f'button:has-text("{target_time}")',
                        f'a:has-text("{target_time}")',
                        f'span:has-text("{target_time}")',
                        f'li:has-text("{target_time}")',
                        f'.time_btn:has-text("{target_time}")',
                        f'[aria-label*="{target_time}"]',
                    ]

                    time_element = None
                    for sel in time_selectors:
                        try:
                            el = page.locator(sel).first
                            if await el.is_visible() and await el.is_enabled():
                                time_element = el
                                break
                        except Exception:
                            continue

                    if not time_element:
                        # Time slot not open or sold out
                        self.silent_tick(f"{target_time} 시간 예약 비활성화 (대기 중)")
                        # Wait a bit before repeating date click
                        await asyncio.sleep(0.2)
                        continue

                    # Target time found! Click it.
                    self.log(f"✓ [{worker_id}번 기기] {target_time} 시간표 활성화 감지! 클릭 시도 중...", "warning")
                    await time_element.click()
                    await asyncio.sleep(0.1)

                    # 3. Click NEXT / BOOK button
                    # Typical next buttons: "다음", "예약하기", "다음단계"
                    next_selectors = [
                        'a:has-text("다음")',
                        'button:has-text("다음")',
                        'a:has-text("예약하기")',
                        'button:has-text("예약하기")',
                        'a:has-text("다음단계")',
                        'button:has-text("다음단계")',
                    ]

                    next_element = None
                    for sel in next_selectors:
                        try:
                            el = page.locator(sel).first
                            if await el.is_visible():
                                next_element = el
                                break
                        except Exception:
                            continue

                    if next_element:
                        await next_element.click()
                        await page.wait_for_load_state("domcontentloaded")
                    else:
                        # Some sites direct to booking forms immediately upon time click.
                        pass

                    # 4. Fill Booking Form (If redirected)
                    # Check all required check-boxes (terms, agreements)
                    try:
                        checkboxes = await page.locator("input[type='checkbox']").all()
                        for cb in checkboxes:
                            try:
                                if not await cb.is_checked():
                                    await cb.check(force=True)
                            except Exception:
                                pass
                    except Exception:
                        pass

                    # Populate user inputs if empty
                    inputs = {
                        "name": ["input[name*='name']", "input[placeholder*='이름']", "input[id*='name']"],
                        "phone": ["input[name*='phone']", "input[name*='tel']", "input[placeholder*='전화번호']", "input[placeholder*='휴대폰']"]
                    }

                    for key, val in [("name", res_data.get("name")), ("phone", res_data.get("phone"))]:
                        for sel in inputs[key]:
                            try:
                                el = page.locator(sel).first
                                if await el.is_visible():
                                    current_val = await el.input_value()
                                    if not current_val:
                                        await el.fill(val)
                                    break
                            except Exception:
                                continue

                    # 5. Click final Submit Button
                    submit_selectors = [
                        'button:has-text("예약")',
                        'a:has-text("예약")',
                        'button:has-text("결제")',
                        'a:has-text("결제")',
                        'button[type="submit"]',
                    ]

                    submit_element = None
                    for sel in submit_selectors:
                        try:
                            el = page.locator(sel).first
                            if await el.is_visible():
                                submit_element = el
                                break
                        except Exception:
                            continue

                    if submit_element:
                        await submit_element.click()
                        # Wait for booking detail page or success URL redirect
                        await page.wait_for_load_state("networkidle")
                        
                        # Verify Success
                        current_url = page.url
                        if "booking-detail" in current_url or "success" in current_url or "complete" in current_url:
                            self.log(f"🎉 [{worker_id}번 기기] 네이버 예약 성공!! URL: {current_url}", "success")
                            self.stop_event.set()
                            if self.success_callback:
                                self.success_callback()
                            break
                        else:
                            self.log(f"⚠️ [{worker_id}번 기기] 폼 제출 후 주소: {current_url}", "warning")
                    else:
                        self.log(f"❌ [{worker_id}번 기기] 최종 예약 완료 버튼을 찾지 못함", "error")

                except Exception as e:
                    self.silent_tick(f"에러: {str(e)[:50]}")
                    await asyncio.sleep(0.5)

        except Exception as e:
            self.log(f"❌ [{worker_id}번 기기] 중단 에러: {e}", "error")
        finally:
            if context:
                await context.close()
