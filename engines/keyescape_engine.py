import time
import requests
import threading
import asyncio
import os
import sys
import shutil
import tempfile
from datetime import datetime, timezone, timedelta
from engines.base_engine import BaseEngine

class KeyescapeEngine(BaseEngine):
    def __init__(self, log_callback, success_callback=None, site_url=None):
        """
        Playwright-based browser engine for Keyescape (bypasses calendar directly to Step 2).
        """
        super().__init__(log_callback, success_callback)
        self.browser_thread = None
        self.site_url = site_url if site_url else 'https://www.keyescape.com'

    def start_reservation(self, reservation_data, num_threads, is_async=False):
        self.log("키이스케이프는 구글 캡차 제한 및 세션 제약으로 인해 단일 브라우저 스레드로 동작합니다.", "info")
        # Enforce single thread and standard mode
        super().start_reservation(reservation_data, num_threads=1, is_async=False)

    def make_reservation_thread(self, reservation_data):
        self.browser_thread = threading.Thread(
            target=self._run_browser_booking,
            args=(reservation_data,),
            name="KeyescapeBrowserThread"
        )
        self.browser_thread.daemon = True
        self.browser_thread.start()

    def _run_browser_booking(self, reservation_data):
        import asyncio
        asyncio.run(self._run_browser_booking_async(reservation_data))

    async def _run_browser_booking_async(self, reservation_data):
        from playwright.async_api import async_playwright
        
        target_date = reservation_data['reservationDate']
        target_time = reservation_data['reservationTime'][:5]
        zizum_num = reservation_data['branch']
        theme_info_num = reservation_data['themePK']  # info_num
        
        # Look up correct themeNum and name from KEYESCAPE_THEMES mapping
        from data.themes import KEYESCAPE_THEMES
        theme_num = ""
        theme_name = ""
        
        for b_id, t_dict in KEYESCAPE_THEMES.items():
            if b_id == zizum_num:
                for t_name, ids in t_dict.items():
                    if ids.get("info_num") == theme_info_num:
                        theme_num = ids.get("theme_num")
                        theme_name = t_name
                        break
                break

        if not theme_num:
            theme_num = theme_info_num
            theme_name = "테마"

        self.log(f"키이스케이프 예약 시작 (날짜: {target_date}, 시간: {target_time}, 테마: {theme_name})", "info")

        # Step 0: Fetch 'doing' value (advance booking days) from theme info API
        doing_days = 0
        try:
            r = requests.post("https://www.keyescape.com/controller/run_proc.php", data={
                't': 'get_theme_info_list',
                'zizum_num': zizum_num
            }, timeout=5)
            if r.status_code == 200:
                data = r.json()
                if data.get("status") and data.get("data"):
                    for theme in data["data"]:
                        if str(theme.get("info_num")) == str(theme_info_num):
                            doing_days = int(theme.get("doing", 0))
                            break
        except Exception:
            pass

        # Calculate the earliest datetime when booking becomes possible (midnight KST)
        target_date_obj = datetime.strptime(target_date, "%Y-%m-%d").date()
        kst = timezone(timedelta(hours=9))
        if doing_days > 0:
            min_booking_date = target_date_obj - timedelta(days=doing_days - 1)
            min_booking_datetime = datetime(min_booking_date.year, min_booking_date.month, min_booking_date.day, 0, 0, 0, tzinfo=kst)
            now_kst = datetime.now(kst)
            time_remaining = min_booking_datetime - now_kst
            if time_remaining.total_seconds() > 0:
                days_r = time_remaining.days
                hours_r = time_remaining.seconds // 3600
                mins_r = (time_remaining.seconds % 3600) // 60
                self.log(f"예약 오픈 감지 설정: doing={doing_days}일, 오픈 예정={min_booking_date} 00:00 KST ({days_r}일 {hours_r}시간 {mins_r}분 남음)", "info")
            else:
                self.log(f"예약 오픈 감지 설정: doing={doing_days}일, 오픈일={min_booking_date} (이미 오픈 가능 시간)", "info")
        else:
            min_booking_date = None
            min_booking_datetime = None
            self.log("doing 값을 조회하지 못했습니다. 실시간 백엔드 감시로 전환합니다.", "warning")

        self.log("예약 1단계 우회용 Time Slot ID(themeTimeNum)를 조회 중...", "info")

        # Step 1: Query API to find the exact themeTimeNum
        theme_time_num = ""
        available_slots = []
        date_is_open = False
        try:
            r = requests.post("https://www.keyescape.com/controller/run_proc.php", data={
                't': 'get_theme_time',
                'date': target_date,
                'zizumNum': zizum_num,
                'themeNum': theme_num,
                'endDay': '0'
            }, timeout=5)
            if r.status_code == 200:
                data = r.json()
                if data.get("status") and data.get("data"):
                    date_is_open = True
                    for slot in data["data"]:
                        slot_time = f"{int(slot.get('hh', 0)):02d}:{int(slot.get('mm', 0)):02d}"
                        available_slots.append(slot_time)
                        if slot_time == target_time:
                            theme_time_num = str(slot.get("num", ""))
                            break
        except Exception as e:
            self.log(f"Time Slot ID 조회 중 오류 발생: {e}", "warning")

        if not theme_time_num:
            if date_is_open and available_slots:
                self.log(f"[경고] 해당 날짜는 오픈되었으나 입력하신 시간({target_time}) 슬롯이 존재하지 않습니다.", "warning")
                self.log(f"[알림] 오픈된 시간대 목록: {', '.join(available_slots)}", "info")
            self.log("경고: 해당 날짜/시간의 Time Slot ID를 찾지 못했습니다. 임의 번호(9999)로 우회를 시도합니다.", "warning")
            theme_time_num = "9999"
        else:
            self.log(f"Time Slot ID 매핑 성공: {theme_time_num}", "info")

        async with async_playwright() as p:
            browser = None
            context = None
            
            launch_methods = [
                {"channel": "chrome", "desc": "Google Chrome"},
                {"channel": "msedge", "desc": "Microsoft Edge"},
                {"channel": None, "desc": "Chromium (기본)"}
            ]
            
            for method in launch_methods:
                self.log(f"{method['desc']} 브라우저 가동을 시도합니다...", "info")
                try:
                    kwargs = {
                        "headless": False,
                        "args": [
                            "--disable-blink-features=AutomationControlled",
                            "--disable-infobars",
                            "--no-sandbox",
                            "--disable-dev-shm-usage"
                        ]
                    }
                    if method["channel"]:
                        kwargs["channel"] = method["channel"]
                        
                    browser = await p.chromium.launch(**kwargs)
                    self.log(f"{method['desc']} 브라우저 가동 성공", "success")
                    break
                except Exception as e:
                    self.log(f"{method['desc']} 가동 실패: {e}", "warning")
                    
            if not browser:
                self.log("최종 브라우저 가동 실패: 모든 브라우저를 켤 수 없습니다.", "error")
                return

            context = await browser.new_context(
                viewport={"width": 1280, "height": 800},
                user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
            )

            # Inject fingerprint stealth script to bypass reCAPTCHA scoring and detection
            await context.add_init_script("""
                try {
                    Object.defineProperty(navigator, 'webdriver', { get: () => undefined });
                } catch (e) {}

                window.chrome = {
                    runtime: {},
                    loadTimes: function() {},
                    csi: function() {},
                    app: {}
                };

                try {
                    const mockPlugins = [
                        { name: 'PDF Viewer', filename: 'internal-pdf-viewer', description: 'Portable Document Format' },
                        { name: 'Chrome PDF Viewer', filename: 'mhjfbmdlmjbbpaocdefihkbeameidjfa', description: 'Redirects PDF requests' },
                        { name: 'Chromium PDF Viewer', filename: 'internal-pdf-viewer', description: 'Portable Document Format' }
                    ];
                    Object.defineProperty(navigator, 'plugins', {
                        get: () => {
                            const pluginsList = mockPlugins.map(p => {
                                const plugin = Object.create(Plugin.prototype);
                                Object.defineProperties(plugin, {
                                    name: { get: () => p.name },
                                    filename: { get: () => p.filename },
                                    description: { get: () => p.description },
                                    length: { get: () => 0 }
                                });
                                return plugin;
                            });
                            Object.defineProperty(pluginsList, 'length', { get: () => mockPlugins.length });
                            pluginsList.item = function(index) { return this[index]; };
                            pluginsList.namedItem = function(name) { return this.find(p => p.name === name) || null; };
                            return pluginsList;
                        }
                    });
                } catch (e) {}

                try {
                    Object.defineProperty(navigator, 'languages', {
                        get: () => ['ko-KR', 'ko', 'en-US', 'en']
                    });
                } catch (e) {}

                try {
                    Object.defineProperty(navigator, 'hardwareConcurrency', {
                        get: () => 8
                    });
                    Object.defineProperty(navigator, 'deviceMemory', {
                        get: () => 8
                    });
                } catch (e) {}

                try {
                    const originalQuery = navigator.permissions.query;
                    navigator.permissions.query = (parameters) => 
                        parameters.name === 'notifications' ?
                            Promise.resolve({ state: Notification.permission }) :
                            originalQuery(parameters);
                } catch (e) {}
            """)

            page = await context.new_page()
            page.set_default_timeout(15000)

            # Register dialog handler - only stop on genuine success, retry on booking errors
            last_dialog_msg = ""
            async def handle_dialog(dialog):
                nonlocal last_dialog_msg
                msg = dialog.message
                last_dialog_msg = msg
                self.log(f"[Alert 감지] 웹사이트 메시지: {msg}", "warning")
                # Only treat success completion as stop signal
                if "완료" in msg and "예약" in msg and "오류" not in msg and "실패" not in msg:
                    self.log("예약 완료 메시지가 감지되었습니다!", "success")
                    self.stop_event.set()
                # Booking-not-open errors → continue retrying (do NOT stop)
                elif any(kw in msg for kw in ["날짜가 아닙니다", "예약 가능", "정원이"]):
                    self.log("예약 오픈 대기 중 - 백엔드 감시를 계속합니다...", "info")
                # Genuine fatal errors → stop
                elif any(kw in msg for kw in ["불가능", "차단", "접근 거부"]):
                    self.log("치명적 오류가 감지되어 종료합니다.", "error")
                    self.stop_event.set()
                try:
                    await dialog.accept()
                except Exception:
                    pass
            page.on("dialog", handle_dialog)

            # Intercept and mock devtools-detector to completely bypass it
            import re
            async def handle_route(route):
                self.log("devtools-detector.min.js 스크립트 감지 및 무력화(Mocking) 수행 완료.", "info")
                await route.fulfill(
                    status=200,
                    content_type="application/javascript",
                    body="window.devtoolsDetector = { addListener: function() {}, removeListener: function() {}, launch: function() {}, stop: function() {}, isLaunch: function() { return false; }, isOpen: false };"
                )
            await page.route(re.compile(r"devtools-detector"), handle_route)

            # We let the headful browser handle dialogs natively so the user can see alerts (like "예약 가능 한 날짜가 아닙니다").

            # Auto-submit POST to reservation2.php to load Step 2 directly
            html_content = f"""
            <html>
            <body>
            <form id="f" action="https://www.keyescape.com/reservation2.php" method="POST">
                <input type="hidden" name="zizumNum" value="{zizum_num}">
                <input type="hidden" name="themeNum" value="{theme_num}">
                <input type="hidden" name="themeInfoNum" value="{theme_info_num}">
                <input type="hidden" name="revDays" value="{target_date}">
                <input type="hidden" name="themeTimeNum" value="{theme_time_num}">
                <input type="hidden" name="revTimes" value="{target_time}">
                <input type="hidden" name="themeName" value="{theme_name}">
            </form>
            <script>document.getElementById('f').submit();</script>
            </body>
            </html>
            """
            
            self.log("Step 2 예약 정보 입력 화면으로 우회 진입을 시도합니다.", "info")
            await page.set_content(html_content)
            await page.wait_for_load_state("domcontentloaded")

            # Fill reservation form fields
            try:
                # 1. Select people
                people_val = reservation_data.get('people', '2')
                await page.locator('select#person').select_option(value=people_val)
                
                # 2. Fill Name
                await page.locator('input#name_input').fill(reservation_data['name'])
                
                # 3. Fill Phone (mobile2, mobile3)
                phone_parts = reservation_data['phone'].split('-')
                if len(phone_parts) >= 3:
                    await page.locator('input[name=mobile2]').fill(phone_parts[1])
                    await page.locator('input[name=mobile3]').fill(phone_parts[2])
                
                # 4. Check Agree All via JS evaluation to avoid hidden-element click failures
                await page.evaluate("""() => {
                    const agreeAll = document.getElementById('agree_all');
                    if (agreeAll) {
                        agreeAll.checked = true;
                        agreeAll.dispatchEvent(new Event('change', { bubbles: true }));
                    }
                }""")
                
                self.log("예약자 이름, 전화번호 기입 및 전체 동의 체크를 완료했습니다.", "info")
            except Exception as e:
                self.log(f"예약 양식 기입 중 오류 발생 (수동 기입 필요): {e}", "warning")

            # Locate reCAPTCHA anchor frame and checkbox using language-independent src selector
            captcha_frame = page.frame_locator('iframe[src*="api2/anchor"]')
            checkbox = captcha_frame.locator('#recaptcha-anchor')

            self.log("[대기] 자동등록방지(reCAPTCHA) '로봇이 아닙니다' 체크박스 자동 클릭을 시도합니다...", "warning")
            self.log("[경고] 구글 캡차 인증은 완료 후 2분이 지나면 자동 초기화(만료)됩니다. 예약 오픈시간 1~2분 전에 완료하시는 것이 가장 안전합니다.", "warning")

            try:
                # Wait for the checkbox to be visible and click it
                await checkbox.wait_for(state="visible", timeout=5000)
                await checkbox.click()
                self.log("reCAPTCHA 체크박스 자동 클릭 완료", "success")
            except Exception as e:
                self.log(f"reCAPTCHA 체크박스 자동 클릭 실패 (수동 체크 필요): {e}", "warning")
            
            captcha_solved = False
            captcha_solve_time = 0  # Track when captcha was solved for 2-min expiration detection
            submit_clicked = False
            is_preset = (theme_time_num != "9999")
            backend_opened = False  # ALWAYS verify via backend API before submit
            last_check_time = 0
            backend_check_count = 0
            captcha_retry_count = 0  # Track how many times captcha was re-solved

            # YesCaptcha API integration configuration
            yescaptcha_token = ""
            yescaptcha_client_key = "3dae6d235caf5de1a4b368179556645b80e698d4127415"
            website_key = "6Le0ObMqAAAAAF7j701m2aQsHLQFe_KDYpKvw3jQ"
            website_url = "https://www.keyescape.com/reservation2.php"

            async def solve_captcha_via_api():
                nonlocal yescaptcha_token
                self.log("[YesCaptcha] 캡차 자동 해결 API 요청을 전송합니다...", "info")
                loop = asyncio.get_running_loop()

                # Try NoCaptchaTaskProxyless first (15pt, faster), fallback to RecaptchaV2TaskProxyless (20pt)
                task_types = ["NoCaptchaTaskProxyless", "RecaptchaV2TaskProxyless"]

                for attempt_idx, task_type in enumerate(task_types):
                    if self.stop_event.is_set() or yescaptcha_token:
                        break

                    try:
                        def call_create_task(tt=task_type):
                            r = requests.post("https://api.yescaptcha.com/createTask", json={
                                "clientKey": yescaptcha_client_key,
                                "task": {
                                    "type": tt,
                                    "websiteURL": website_url,
                                    "websiteKey": website_key
                                }
                            }, timeout=10)
                            return r.json() if r.status_code == 200 else None

                        res = await loop.run_in_executor(None, call_create_task)

                        if not res or res.get("errorId") != 0:
                            err_code = res.get("errorCode") if res else "HTTP 연결 오류"
                            self.log(f"[YesCaptcha] 태스크 접수 실패 ({task_type}): {err_code}", "warning")
                            continue

                        task_id = res.get("taskId")
                        self.log(f"[YesCaptcha] 태스크 생성 성공 (ID: {task_id}, 방식: {task_type}). 캡차 분석 중...", "info")

                        def call_get_result():
                            r = requests.post("https://api.yescaptcha.com/getTaskResult", json={
                                "clientKey": yescaptcha_client_key,
                                "taskId": task_id
                            }, timeout=10)
                            return r.json() if r.status_code == 200 else None

                        poll_start = time.time()
                        solved = False
                        # Poll: 3-second interval (API doc minimum), 40 iterations = 120 seconds (system timeout)
                        for i in range(40):
                            if self.stop_event.is_set():
                                break
                            await asyncio.sleep(3)

                            res_result = await loop.run_in_executor(None, call_get_result)
                            if res_result and res_result.get("errorId") == 0:
                                status = res_result.get("status")
                                if status == "ready":
                                    token = res_result.get("solution", {}).get("gRecaptchaResponse")
                                    if token:
                                        elapsed = int(time.time() - poll_start)
                                        yescaptcha_token = token
                                        self.log(f"[YesCaptcha] 캡차 자동 해결 완료! ({elapsed}초 소요)", "success")
                                        solved = True
                                        break
                                elif status == "processing":
                                    # Progress log every 15 seconds (every 5 polls)
                                    if (i + 1) % 5 == 0:
                                        elapsed = int(time.time() - poll_start)
                                        self.log(f"[YesCaptcha] 캡차 분석 진행 중... ({elapsed}초 경과)", "info")
                                else:
                                    self.log(f"[YesCaptcha] 해결 실패 (상태: {status})", "warning")
                                    break
                            else:
                                self.log("[YesCaptcha] 결과 조회 중 통신 오류 발생, 재시도...", "warning")
                                continue  # Retry on network error instead of breaking

                        if solved:
                            break

                        if not yescaptcha_token and attempt_idx < len(task_types) - 1:
                            elapsed = int(time.time() - poll_start)
                            self.log(f"[YesCaptcha] {task_type} 방식 실패 ({elapsed}초). 대체 방식({task_types[attempt_idx + 1]})으로 재시도합니다...", "warning")

                    except Exception as e:
                        self.log(f"[YesCaptcha] 자동 해결 중 오류 발생: {e}", "warning")

                if not yescaptcha_token and not self.stop_event.is_set():
                    self.log("[YesCaptcha] 자동 해결 실패. 수동으로 캡차를 해결해주세요.", "warning")

            # Start background solving task
            api_task = asyncio.create_task(solve_captcha_via_api())

            try:
                while not self.stop_event.is_set():
                    # 0. Check captcha expiration (token valid for 2 min = 120s, re-solve at 105s)
                    if captcha_solved and not submit_clicked and captcha_solve_time > 0:
                        elapsed_since_solve = time.time() - captcha_solve_time
                        if elapsed_since_solve >= 105:
                            captcha_retry_count += 1
                            self.log(f"[경고] 캡차 토큰 만료 ({int(elapsed_since_solve)}초 경과). 자동 재해결을 시작합니다... (#{captcha_retry_count})", "warning")
                            captcha_solved = False
                            captcha_solve_time = 0
                            yescaptcha_token = ""
                            # Re-click checkbox
                            try:
                                await checkbox.click()
                                self.log("reCAPTCHA 체크박스 재클릭 완료", "info")
                            except Exception:
                                self.log("reCAPTCHA 체크박스 재클릭 실패", "warning")
                            # Launch new API solve task
                            api_task = asyncio.create_task(solve_captcha_via_api())
                            self.log("[YesCaptcha] 캡차 자동 재해결 태스크를 시작했습니다.", "info")

                    # 1. Update Captcha Solved State (Accept either Playwright manual check or YesCaptcha API token)
                    if yescaptcha_token:
                        if not captcha_solved:
                            captcha_solved = True
                            captcha_solve_time = time.time()
                            self.log("[YesCaptcha] 우회 토큰을 브라우저에 주입합니다.", "success")
                            await page.evaluate(f"""() => {{
                                const textareas = document.querySelectorAll('textarea[name="g-recaptcha-response"]');
                                textareas.forEach(t => {{
                                    t.value = "{yescaptcha_token}";
                                }});
                                if (window.grecaptcha) {{
                                    window.grecaptcha.getResponse = () => "{yescaptcha_token}";
                                }}
                                // Hide the reCAPTCHA challenge popup visually
                                const bframes = document.querySelectorAll('iframe[src*="api2/bframe"]');
                                bframes.forEach(f => {{
                                    if (f.parentElement && f.parentElement.parentElement) {{
                                        f.parentElement.parentElement.style.display = 'none';
                                    }}
                                }});
                            }}""")
                            remaining = 120 - int(time.time() - captcha_solve_time)
                            self.log(f"[YesCaptcha] 우회 토큰 주입 완료! 토큰 유효시간: {remaining}초 (실시간 백엔드 감시 중)", "success")
                    else:
                        try:
                            checked = await checkbox.get_attribute("aria-checked", timeout=100)
                            if checked == "true":
                                if not captcha_solved:
                                    captcha_solved = True
                                    captcha_solve_time = time.time()
                                    self.log("reCAPTCHA 구글 인증 성공을 확인했습니다. (예약 오픈 대기 중...)", "success")
                            else:
                                if captcha_solved:
                                    captcha_solved = False
                                    captcha_solve_time = 0
                                    self.log("reCAPTCHA가 해제되었습니다. 자동 재해결을 시작합니다...", "warning")
                                    yescaptcha_token = ""
                                    captcha_retry_count += 1
                                    # Re-click checkbox
                                    try:
                                        await checkbox.click()
                                        self.log("reCAPTCHA 체크박스 재클릭 완료", "info")
                                    except Exception:
                                        pass
                                    # Launch new API solve task
                                    api_task = asyncio.create_task(solve_captcha_via_api())
                                    self.log(f"[YesCaptcha] 캡차 자동 재해결 태스크 시작 (#{captcha_retry_count})", "info")
                        except Exception:
                            pass

                    # 2. Monitor site backend opening - date-based + API verification
                    if not backend_opened:
                        now = time.time()
                        if now - last_check_time >= 0.05:
                            last_check_time = now
                            backend_check_count += 1

                            # Phase A: Check if current time has reached the booking open datetime
                            now_kst = datetime.now(kst)
                            date_in_window = True  # Default: assume open if no doing info
                            if min_booking_datetime and now_kst < min_booking_datetime:
                                date_in_window = False
                                # Progress log every 200 checks (~10 seconds)
                                if backend_check_count % 200 == 0:
                                    remaining = min_booking_datetime - now_kst
                                    total_secs = int(remaining.total_seconds())
                                    days_r = remaining.days
                                    hours_r = remaining.seconds // 3600
                                    mins_r = (remaining.seconds % 3600) // 60
                                    secs_r = remaining.seconds % 60
                                    if days_r > 0:
                                        self.log(f"[백엔드 감시] 예약 오픈까지 {days_r}일 {hours_r}시간 {mins_r}분 남음 (오픈: {min_booking_date} 00:00)", "info")
                                    elif hours_r > 0:
                                        self.log(f"[백엔드 감시] 예약 오픈까지 {hours_r}시간 {mins_r}분 남음", "info")
                                    else:
                                        self.log(f"[백엔드 감시] 예약 오픈까지 {mins_r}분 {secs_r}초 남음!", "warning")

                            # Phase B: When date is in window, verify via API with rapid polling
                            if date_in_window:
                                # Only call API every 0.1s (skip every other tick)
                                if backend_check_count % 2 == 0:
                                    try:
                                        r = requests.post("https://www.keyescape.com/controller/run_proc.php", data={
                                            't': 'get_theme_time',
                                            'date': target_date,
                                            'zizumNum': zizum_num,
                                            'themeNum': theme_num,
                                            'endDay': '0'
                                        }, timeout=0.5)
                                        if r.status_code == 200:
                                            data = r.json()
                                            if data.get("status") and data.get("data"):
                                                for slot in data["data"]:
                                                    slot_time = f"{int(slot.get('hh', 0)):02d}:{int(slot.get('mm', 0)):02d}"
                                                    if slot_time == target_time:
                                                        real_num = str(slot.get("num", ""))
                                                        if real_num:
                                                            theme_time_num = real_num
                                                            backend_opened = True
                                                            self.log(f"[백엔드 감지] 예약 오픈 확인! ID: {theme_time_num} (날짜 검증 통과)", "success")
                                                            await page.evaluate(f"() => {{ document.getElementsByName('themeTimeNum')[0].value = '{theme_time_num}'; }}")
                                                            self.log(f"페이지 예약 데이터 동적 치환 완료 (themeTimeNum -> {theme_time_num})", "info")
                                                            break
                                    except Exception:
                                        pass
                                    # Progress log every 100 API checks (~10 seconds)
                                    api_count = backend_check_count // 2
                                    if not backend_opened and api_count > 0 and api_count % 100 == 0:
                                        self.log(f"[백엔드 감시] 예약 오픈 대기 중... (API {api_count}회 조회)", "info")

                    # 3. Click Submit only when Captcha is solved and Backend is opened
                    if captcha_solved and backend_opened and not submit_clicked:
                        submit_clicked = True
                        self.log("캡차 완료 및 예약 오픈 확인! 예약하기 버튼을 클릭합니다.", "info")
                        try:
                            # Submit the form using JavaScript programmatic click
                            await page.evaluate("""() => {
                                const btn = document.querySelector('button.submit.pc') || document.querySelector('button.submit');
                                if (btn) {
                                    btn.click();
                                } else {
                                    throw new Error("Submit button not found");
                                }
                            }""")
                            
                            # Wait for Success page
                            await page.wait_for_url("**/reservation3.php**", timeout=5000)
                            self.log("예약 완료! 성공 페이지(reservation3.php) 진입 성공.", "success")
                            
                            # Extract Reservation Number
                            try:
                                body_text = await page.inner_text("body")
                                import re
                                match = re.search(r"(?:예약\s*번호|예약번호)\s*:\s*([A-Za-z0-9-]+)", body_text)
                                if not match:
                                    match = re.search(r"\bK\d{8}-\d+\b", body_text)
                                if match:
                                    rev_num = match.group(1) if len(match.groups()) > 0 else match.group(0)
                                    self.log(f"★ 예약 완료 번호: {rev_num} ★", "success")
                                else:
                                    self.log("성공 페이지에서 예약번호 패턴을 파싱하지 못했습니다. 수동 확인이 필요합니다.", "warning")
                            except Exception as ex:
                                self.log(f"예약번호 추출 중 오류 발생: {ex}", "warning")

                            self.stop_event.set()
                            if self.success_callback:
                                self.success_callback()
                            break
                        except Exception as e:
                            # Submit failed (alert error or timeout) - reset and keep trying
                            self.log(f"제출 후 대기 중 오류 (자동 재시도): {e}", "warning")
                            submit_clicked = False

                            # Case 1: Date not open → reset backend + re-monitor
                            if "날짜" in last_dialog_msg or "아닙니다" in last_dialog_msg:
                                backend_opened = False
                                last_dialog_msg = ""
                                self.log("예약 오픈 전 상태 - 백엔드 감시를 재개합니다...", "info")

                            # Case 2: "잘못된 접근" = captcha consumed → re-solve captcha
                            if "잘못된 접근" in last_dialog_msg or "접근" in last_dialog_msg:
                                self.log("[경고] 제출로 캡차가 소모되었습니다. 자동 재해결을 시작합니다...", "warning")
                                captcha_solved = False
                                captcha_solve_time = 0
                                yescaptcha_token = ""
                                last_dialog_msg = ""
                                captcha_retry_count += 1
                                try:
                                    await checkbox.click()
                                    self.log("reCAPTCHA 체크박스 재클릭 완료", "info")
                                except Exception:
                                    pass
                                api_task = asyncio.create_task(solve_captcha_via_api())
                                self.log(f"[YesCaptcha] 캡차 자동 재해결 태스크 시작 (#{captcha_retry_count})", "info")

                            await asyncio.sleep(0.1)

                    await asyncio.sleep(0.05)
            finally:
                pass

            await asyncio.sleep(3)
            try:
                await context.close()
            except Exception:
                pass
            try:
                await browser.close()
            except Exception:
                pass
