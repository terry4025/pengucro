import asyncio
import os
import threading
import time
from datetime import datetime
from pathlib import Path
from PIL import Image
from engines.base_engine import BaseEngine


def _scratch_dir() -> Path:
    """Working directory for captcha screenshots, created on demand."""
    directory = Path.cwd() / "scratch"
    directory.mkdir(parents=True, exist_ok=True)
    return directory


async def ocr_image_winsdk(img_path):
    from winsdk.windows.storage import StorageFile
    from winsdk.windows.graphics.imaging import BitmapDecoder
    from winsdk.windows.media.ocr import OcrEngine
    from winsdk.windows.globalization import Language

    abs_path = os.path.abspath(img_path)
    file = await StorageFile.get_file_from_path_async(abs_path)
    stream = await file.open_async(1) # 1 = FileAccessMode.Read
    decoder = await BitmapDecoder.create_async(stream)
    bitmap = await decoder.get_software_bitmap_async()
    
    lang = Language("en-US")
    engine = OcrEngine.try_create_from_language(lang)
    if not engine:
        engine = OcrEngine.try_create_from_user_profile_languages()
        
    result = await engine.recognize_async(bitmap)
    return result.text

class PhobiaDungeonEngine(BaseEngine):
    def __init__(self, log_callback, success_callback=None, status_callback=None, log_batch_callback=None):
        """
        Playwright-based parallel Phobia Dungeon Booking Engine.
        """
        super().__init__(log_callback, success_callback, status_callback, log_batch_callback)
        self.playwright_thread = None
        self.loop = None
        self.browser = None

    def start_reservation(self, reservation_data, num_threads, is_async=False):
        if self.is_running:
            self.log("예약 엔진이 이미 실행 중입니다.", "warning")
            return

        self.is_running = True
        self.stop_event.clear()
        self._attempt_count = 0
        self._last_error = ""
        self._seen_errors = set()
        self._success_fired = False

        self.log(f"비트포비아 던전 예약을 시작합니다. (병렬 인스턴스: {num_threads}개)", "info")

        # Start Playwright automation loop in a background thread
        self.playwright_thread = threading.Thread(
            target=self._run_playwright_loop,
            args=(reservation_data, num_threads),
            name="PhobiaPlaywrightThread"
        )
        self.playwright_thread.daemon = True
        self.playwright_thread.start()

        # Monitor thread
        monitor = threading.Thread(target=self._monitor_playwright_thread, name="PhobiaMonitorThread")
        monitor.daemon = True
        monitor.start()

    def _monitor_playwright_thread(self):
        if self.playwright_thread:
            self.playwright_thread.join()
        self.is_running = False
        message = "비트포비아 예약 작업이 성공적으로 종료되었습니다." if self._success_fired else "비트포비아 예약 작업이 종료되었습니다."
        self.log(message, "success" if self._success_fired else "info")

    def stop_reservation(self):
        if not self.is_running:
            return
        self.log("비트포비아 예약을 정지합니다...", "info")
        self.stop_event.set()

        # Close browser inside the loop threadsafe
        if self.browser and self.loop and self.loop.is_running():
            try:
                asyncio.run_coroutine_threadsafe(self.browser.close(), self.loop)
            except Exception:
                pass

    def _run_playwright_loop(self, reservation_data, num_threads):
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

    async def _run_async_tasks(self, reservation_data, num_threads):
        from playwright.async_api import async_playwright
        
        async with async_playwright() as p:
            is_dev = reservation_data.get('devMode', False)
            self.log(f"예약 백그라운드 태스크 기동 중... (개발자 모드: {is_dev})", "info")
            
            # headless=False if devMode is True so user can see it
            self.browser = await self._launch_browser(p, headless=not is_dev)

            tasks = []
            for i in range(num_threads):
                tasks.append(
                    self._booking_worker_task(i + 1, reservation_data)
                )

            await asyncio.gather(*tasks)

            if self.browser:
                await self.browser.close()
                self.browser = None

    async def _booking_worker_task(self, worker_id, res_data):
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
            page = await context.new_page()

            # Auto-dismiss dialogs
            async def handle_dialog(dialog):
                msg = dialog.message[:80] if dialog.message else ""
                self.log(f"⚠️ [{worker_id}번 기기] 경고창: {msg}", "warning")
                await dialog.dismiss()
            page.on("dialog", handle_dialog)

            page.set_default_timeout(10000)
            page.set_default_navigation_timeout(15000)

            # Extract info
            branch_id = res_data.get("branch", "3")
            target_date = res_data.get("reservationDate")
            target_time = res_data.get("reservationTime")[:5]
            target_theme = res_data.get("themePK")
            name = res_data.get("name")
            phone = res_data.get("phone")
            people = res_data.get("people", "2")
            dev_mode = res_data.get("devMode", False)

            # Phone processing
            phone_digits = "".join(c for c in phone if c.isdigit())
            if len(phone_digits) == 11:
                m1, m2, m3 = phone_digits[:3], phone_digits[3:7], phone_digits[7:]
            elif len(phone_digits) == 10:
                m1, m2, m3 = phone_digits[:3], phone_digits[3:6], phone_digits[6:]
            else:
                m1, m2, m3 = "010", "", ""

            booking_url = f"https://xdungeon.net/layout/res/home.php?go=rev.main&s_zizum={branch_id}&rev_days={target_date}"
            self.log(f"[{worker_id}번 기기] 예약 페이지로 이동 중... ({booking_url})", "info")
            await page.goto(booking_url)
            await page.wait_for_load_state("domcontentloaded")

            attempt = 0
            while not self.stop_event.is_set():
                attempt += 1
                self.silent_tick(f"{target_time} 대기")

                # Phase 1: Timetable checking/refresh loop
                if "go=rev.make" not in page.url:
                    time_link = None
                    try:
                        theme_boxes = page.locator("div.thm_box div.box")
                        count = await theme_boxes.count()
                        for i in range(count):
                            box = theme_boxes.nth(i)
                            tit_elem = box.locator("p.tit")
                            if await tit_elem.count() > 0:
                                tit_text = await tit_elem.inner_text()
                                if target_theme in tit_text:
                                    time_links = box.locator("div.time_box ul li a[href*='go=rev.make']")
                                    lc = await time_links.count()
                                    for j in range(lc):
                                        link = time_links.nth(j)
                                        txt = await link.inner_text()
                                        if target_time in txt:
                                            time_link = link
                                            break
                            if time_link:
                                break
                    except Exception:
                        pass

                    if time_link:
                        self.log(f"[{worker_id}번 기기] {target_theme} {target_time} 슬롯 발견! 클릭을 시도합니다.", "info")
                        await time_link.click()
                        try:
                            await page.wait_for_url("**/home.php?go=rev.make**", timeout=3000)
                        except Exception:
                            pass
                        continue

                    # Reload page if slot not found
                    if attempt == 1 or attempt % 15 == 0:
                        self.log(f"[{worker_id}번 기기] {target_theme} {target_time} 슬롯 비활성화/매진 - 재시도 ({attempt}회)", "warning")
                    
                    await page.reload()
                    await page.wait_for_load_state("domcontentloaded")
                    await asyncio.sleep(0.3)
                    continue

                # Phase 2: Form entry page
                self.log(f"[{worker_id}번 기기] 예약 정보 입력 단계 진입", "info")
                
                try:
                    await page.select_option("#person", value=str(people))
                    await asyncio.sleep(0.1)
                except Exception as e:
                    self.log(f"인원 선택 오류: {e}", "warning")

                try:
                    await page.fill('input[name="name"]', name)
                    await page.fill('input[name="mobile1"]', m1)
                    await page.fill('input[name="mobile2"]', m2)
                    await page.fill('input[name="mobile3"]', m3)
                except Exception as e:
                    self.log(f"정보 입력 오류: {e}", "warning")

                # CAPTCHA solving loop
                captcha_solved = False
                for cap_att in range(1, 15):
                    if self.stop_event.is_set():
                        return
                    try:
                        captcha_elem = page.locator("#captcha_img")
                        await captcha_elem.wait_for(state="visible", timeout=3000)
                        old_src = await captcha_elem.get_attribute("src")

                        # scratch/ is in .gitignore, so on a fresh clone the
                        # directory does not exist and every screenshot raised,
                        # burning all 15 captcha attempts without a usable error.
                        temp_dir = _scratch_dir()
                        temp_raw = str(temp_dir / f"temp_phobia_raw_{worker_id}.png")
                        temp_proc = str(temp_dir / f"temp_phobia_proc_{worker_id}.png")

                        await captcha_elem.screenshot(path=temp_raw)

                        # Explicit close so the file is not held open by PIL's
                        # lazy loader while the next iteration overwrites it.
                        with Image.open(temp_raw) as img:
                            gray = img.convert("L")
                            scaled = gray.resize(
                                (img.size[0] * 4, img.size[1] * 4), Image.Resampling.LANCZOS
                            )
                        binarized = scaled.point(lambda p: 255 if p > 130 else 0)
                        binarized.save(temp_proc)

                        text = await ocr_image_winsdk(temp_proc)
                        digits = "".join(c for c in text if c.isdigit())

                        if len(digits) == 5:
                            self.log(f"[{worker_id}번 기기] 자동 캡차 판독 성공: {digits}", "info")
                            await page.fill('input[name="input_captcha"]', digits)
                            captcha_solved = True
                            break
                        else:
                            await page.click("#captcha_img")
                            for _ in range(30):
                                if self.stop_event.is_set():
                                    return
                                new_src = await captcha_elem.get_attribute("src")
                                if new_src and new_src != old_src:
                                    break
                                await asyncio.sleep(0.1)
                    except Exception as ce:
                        self.log(f"[{worker_id}번 기기] 캡차 처리 예외 발생: {ce}", "warning")
                        await asyncio.sleep(0.5)

                if not captcha_solved:
                    self.log(f"[{worker_id}번 기기] 캡차 해독 실패. 페이지를 다시 불러옵니다.", "warning")
                    await page.goto(booking_url)
                    await page.wait_for_load_state("domcontentloaded")
                    continue

                try:
                    await page.check('input[name="agree_a"]')
                    await page.check('input[name="agree_b"]')
                except Exception as e:
                    self.log(f"동의 약관 체크 실패: {e}", "warning")

                # Submit Form
                self.log(f"[{worker_id}번 기기] '예약하기' 제출 시도", "info")
                await page.click('div.btn_box a')

                # Handle Popup Confirm
                try:
                    popup_ok = page.locator('#cancel_popup button.ok, #cancel_popup button')
                    await popup_ok.wait_for(state="visible", timeout=3000)
                    await popup_ok.click()
                except Exception as e:
                    self.log(f"팝업 창 확인 버튼 에러: {e}", "warning")

                # Wait for Redirect to Payment Page
                try:
                    await page.wait_for_url("**/rev.pay**", timeout=8000)
                except Exception:
                    pass

                if "rev.pay" not in page.url:
                    self.log(f"[{worker_id}번 기기] 결제 페이지 이동 실패 (캡차 불일치 혹은 시간 선점). 처음부터 다시 시도합니다.", "warning")
                    await page.goto(booking_url)
                    await page.wait_for_load_state("domcontentloaded")
                    continue

                # Phase 3: Payment Gateway
                self.log(f"[{worker_id}번 기기] 결제하기 버튼 클릭", "info")
                pay_btn = page.locator('a:has-text("결제하기"), button:has-text("결제하기"), a:has-text("결제")')
                await pay_btn.wait_for(state="visible", timeout=5000)
                await pay_btn.click()

                # Wait for iframe load
                iframe_selector = "#__tosspayments_payment-gateway_iframe__"
                await page.wait_for_selector(iframe_selector, timeout=10000)

                frame = None
                frames = page.frames
                for f in frames:
                    if "__tosspayments" in f.name:
                        frame = f
                        break
                if not frame:
                    frame = page.frame(name="__tosspayments_payment-gateway_iframe__")

                if not frame:
                    self.log(f"[{worker_id}번 기기] Toss Payments iframe 탐색 실패.", "error")
                    return

                self.log(f"[{worker_id}번 기기] Toss 결제창 집입. 은행 선택 및 알림 연락처 입력을 진행합니다.", "info")
                await asyncio.sleep(0.8)

                # Bank Selection
                target_bank = "국민"
                bank_selected = False

                # Strategy A: Native select dropdown
                try:
                    select_loc = frame.locator("select#vbankBankCode")
                    if await select_loc.count() > 0:
                        bank_map = {
                            "농협": "11", "국민": "06", "우리": "20", "신한": "26", 
                            "기업": "03", "경남": "39", "광주": "34", "대구": "31", 
                            "부산": "32", "새마을": "45", "수협": "07", "우체국": "71", "하나": "81"
                        }
                        code = bank_map.get(target_bank, "06")
                        await select_loc.select_option(value=code)
                        bank_selected = True
                        self.log(f"[{worker_id}번 기기] 가상계좌 은행 선택 완료 (select): {target_bank}", "info")
                except Exception:
                    pass

                # Strategy B: UI lists/buttons text match
                if not bank_selected:
                    try:
                        selectors = [
                            f'button:has-text("{target_bank}"):visible',
                            f'a:has-text("{target_bank}"):visible',
                            f'div:has-text("{target_bank}"):visible',
                            f'span:has-text("{target_bank}"):visible',
                            f'li:has-text("{target_bank}"):visible',
                        ]
                        for sel in selectors:
                            loc = frame.locator(sel)
                            if await loc.count() > 0:
                                await loc.first.click()
                                bank_selected = True
                                self.log(f"[{worker_id}번 기기] 가상계좌 은행 선택 완료 (텍스트 매칭): {target_bank}", "info")
                                break
                    except Exception as e:
                        self.log(f"[{worker_id}번 기기] 은행 선택 매칭 클릭 실패: {e}", "warning")

                # Cash receipt opt-out
                try:
                    cash_receipt = frame.locator("#vbankCashReceiptView, input[name*='Receipt'], input[name*='receipt']")
                    if await cash_receipt.count() > 0:
                        if await cash_receipt.first.is_checked():
                            await cash_receipt.first.uncheck(force=True)
                except Exception:
                    pass

                try:
                    label = frame.locator('label:has-text("발행하지 않음"), label:has-text("미발행"), span:has-text("미발행")')
                    if await label.count() > 0:
                        await label.first.click()
                except Exception:
                    pass

                # Input phone number for refund/notification inside iframe
                try:
                    phone_input = frame.locator('input[aria-label="휴대폰번호"], input[placeholder*="휴대폰"], input[type="tel"]')
                    await phone_input.wait_for(state="visible", timeout=3000)
                    await phone_input.fill(phone_digits)
                    self.log(f"[{worker_id}번 기기] 가상계좌 알림 연락처 입력 완료", "info")
                except Exception as e:
                    self.log(f"[{worker_id}번 기기] 가상계좌 알림 연락처 입력칸 탐색 실패: {e}", "warning")

                # DevMode: exit before final submission
                if dev_mode:
                    self.log(f"✓ [{worker_id}번 기기] [개발자 테스트] 결제 최종 승인 직전 멈춤! (제출 우회)", "success")
                    self.notify_success()
                    while not self.stop_event.is_set():
                        await asyncio.sleep(0.5)
                    break

                # Submit final payment details
                self.log(f"[{worker_id}번 기기] 결제 승인 요청 제출 중...", "info")
                for btn_text in ["다음", "결제하기", "결제"]:
                    try:
                        btn = frame.locator(f'button:has-text("{btn_text}"), a:has-text("{btn_text}")')
                        if await btn.count() > 0:
                            await btn.first.click()
                            await asyncio.sleep(0.5)
                    except Exception:
                        pass
                try:
                    done_btn = frame.locator("#payDoneBtn")
                    if await done_btn.count() > 0:
                        await done_btn.click()
                except Exception:
                    pass

                # Verify completion redirect
                self.log(f"[{worker_id}번 기기] 결제 완료 처리 대기 중...", "info")
                await asyncio.sleep(2.5)
                cur_url = page.url
                if "rev.complete" in cur_url or "complete" in cur_url or "ok" in cur_url:
                    self.log(f"🎉 [{worker_id}번 기기] 예약 성공!! 완료 페이지: {cur_url}", "success")
                    self.notify_success()
                    break
                else:
                    self.log(f"[{worker_id}번 기기] 완료 페이지 확인 중: {cur_url}", "info")
                    self.notify_success()
                    break

        except Exception as e:
            self.log(f"[{worker_id}번 기기] 에러 발생: {e}", "error")
        finally:
            if context:
                await context.close()
