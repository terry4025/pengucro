import requests
from bs4 import BeautifulSoup
import json
from engines.base_engine import BaseEngine

class JigubyeolEngine(BaseEngine):
    # Every HTTP call must be bounded. Without a timeout a worker that hits a
    # stalled connection blocks forever, BaseEngine._monitor_threads never
    # finishes joining it, is_running stays True and the GUI's stop button
    # locks up permanently.
    LOOKUP_TIMEOUT = 5
    SUBMIT_TIMEOUT = 8

    def __init__(self, log_callback, success_callback=None, site_url=None):
        super().__init__(log_callback, success_callback)
        self.base_url = site_url if site_url else 'https://www.xn--2e0b040a4xj.com'

    def get_csrf_token(self, session):
        response = session.get(f'{self.base_url}/reservation', timeout=self.LOOKUP_TIMEOUT)
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
        
        while not self.stop_event.is_set():
            try:
                if not csrf_token:
                    csrf_token = self.get_csrf_token(session)
                
                if self.stop_event.is_set():
                    break
                with self.submission_lock:
                    if self.stop_event.is_set():
                        break
                    
                    # Step 1: 시간 선택 선등록
                    step1_response = self.submit_time_selection(session, csrf_token, reservation_data)
                    if step1_response.status_code == 419:
                        csrf_token = None
                        continue
                    if step1_response.status_code not in (200, 201):
                        self.handle_error(step1_response, reservation_data, '시간선택')
                        continue
                    
                    # Step 2: 최종 예약 완료
                    try:
                        decoded_html = step1_response.content.decode('utf-8')
                    except Exception:
                        decoded_html = step1_response.text
                    
                    soup = BeautifulSoup(decoded_html, 'html.parser')
                    payment_input = soup.find('input', {'name': 'payment_method'})
                    payment_method = payment_input.get('value', '1') if payment_input else '1'
                    
                    step2_response = self.submit_reservation(session, csrf_token, reservation_data, payment_method)
                    
                    if step2_response.status_code == 419:
                        csrf_token = None
                        continue
                        
                    if step2_response.status_code in (200, 201):
                        try:
                            done_url = f"{self.base_url}/reservation/done"
                            done_res = session.get(done_url, timeout=self.LOOKUP_TIMEOUT)
                            done_res.encoding = 'utf-8'
                            done_soup = BeautifulSoup(done_res.text, 'html.parser')
                            
                            vbank_num = ""
                            vbank_deadline = ""
                            vbank_amount = ""
                            booking_id = ""
                            
                            tables = done_soup.find_all('table')
                            for table in tables:
                                for row in table.find_all('tr'):
                                    th = row.find('th')
                                    td = row.find('td')
                                    if th and td:
                                        th_text = th.text.strip()
                                        td_text = td.text.strip()
                                        if '계좌' in th_text or '가상계좌' in th_text:
                                            vbank_num = td_text
                                        elif '기한' in th_text or '만료' in th_text:
                                            vbank_deadline = td_text
                                        elif '금액' in th_text or '입금액' in th_text:
                                            vbank_amount = td_text
                                        elif '예약번호' in th_text:
                                            booking_id = td_text
                                            
                            success_msg = "예약 성공!"
                            if "임시로" in done_res.text or "입금전" in done_res.text:
                                success_msg += " (가상계좌 임시 예약 완료)"
                                if vbank_num:
                                    success_msg += f" 계좌: {vbank_num}"
                                if vbank_amount:
                                    success_msg += f", 금액: {vbank_amount}"
                                if vbank_deadline:
                                    success_msg += f", 기한: {vbank_deadline}"
                            else:
                                success_msg += " (예약 확정 완료)"
                                if booking_id:
                                    success_msg += f" 예약번호: {booking_id}"
                                    
                            self.log(success_msg, "success")
                        except Exception as e:
                            self.log(f"예약 성공! (상세 정보 파싱 실패: {e})", "success")
                            
                        self.notify_success()
                        break
                    else:
                        self.handle_error(step2_response, reservation_data, '최종예약')
            except Exception as e:
                csrf_token = None
                self.log(f"{reservation_data['reservationTime'][:5]} 시도 중... (연결 오류 - 재시도)", "info")

    def handle_error(self, response, reservation_data, step_name):
        time_slot = reservation_data['reservationTime'][:5]

        try:
            decoded_text = response.content.decode('utf-8')
        except Exception:
            decoded_text = response.text

        try:
            error_data = json.loads(decoded_text)
            if 'errors' in error_data and isinstance(error_data['errors'], dict):
                err_details = []
                for field, msgs in error_data['errors'].items():
                    if isinstance(msgs, list):
                        msg_str = ", ".join(msgs)
                    else:
                        msg_str = str(msgs)
                    err_details.append(f"{field}: {msg_str}")
                main_msg = error_data.get('message', 'The given data was invalid.')
                error_message = f"{main_msg} ({'; '.join(err_details)})"
            elif 'Message' in error_data:
                error_message = error_data['Message']
            elif 'message' in error_data:
                error_message = error_data['message']
            else:
                error_message = decoded_text[:200]
        except (json.JSONDecodeError, ValueError):
            error_message = decoded_text[:200]

        if "이미 예약" in error_message:
            self.log(f"{time_slot} 시도 중... (이미 예약이 완료된 시간대, 해당 시간대 예약이 다시 열릴때까지 재시도)", "info")
        elif "결제 수단" in error_message or "결제수단" in error_message:
            self.log(f"{time_slot} 시도 중... (이미 예약이 완료된 시간대이거나 결제수단 오류 - 재시도)", "info")
        else:
            self.log(f"{time_slot} 시도 중... ({error_message}, 재시도)", "info")

    async def make_reservation_async_task(self, reservation_data, task_idx):
        import aiohttp
        import asyncio
        
        session = None
        csrf_token = None
        
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
                try:
                    if not csrf_token:
                        csrf_token = await self.get_csrf_token_async(session)
                    
                    if self.stop_event.is_set():
                        break
                        
                    async with self.async_submission_lock:
                        if self.stop_event.is_set():
                            break
                        
                        # Step 1: 시간 선택 선등록
                        step1_response = await self.submit_time_selection_async(session, csrf_token, reservation_data)
                        if step1_response.status == 419:
                            csrf_token = None
                            continue
                        if step1_response.status not in (200, 201):
                            await self.handle_error_async(step1_response, reservation_data, '시간선택')
                            continue
                        
                        # Step 2: 최종 예약 완료
                        try:
                            decoded_html = await step1_response.text()
                        except Exception:
                            decoded_html = str(step1_response)
                            
                        from bs4 import BeautifulSoup
                        soup = BeautifulSoup(decoded_html, 'html.parser')
                        payment_input = soup.find('input', {'name': 'payment_method'})
                        payment_method = payment_input.get('value', '1') if payment_input else '1'
                        
                        step2_response = await self.submit_reservation_async(session, csrf_token, reservation_data, payment_method)
                        
                        if step2_response.status == 419:
                            csrf_token = None
                            continue
                            
                        if step2_response.status in (200, 201):
                            try:
                                done_url = f"{self.base_url}/reservation/done"
                                async with session.get(done_url) as done_res:
                                    done_text = await done_res.text()
                                    done_soup = BeautifulSoup(done_text, 'html.parser')
                                    
                                    vbank_num = ""
                                    vbank_deadline = ""
                                    vbank_amount = ""
                                    booking_id = ""
                                    
                                    tables = done_soup.find_all('table')
                                    for table in tables:
                                        for row in table.find_all('tr'):
                                            th = row.find('th')
                                            td = row.find('td')
                                            if th and td:
                                                th_text = th.text.strip()
                                                td_text = td.text.strip()
                                                if '계좌' in th_text or '가상계좌' in th_text:
                                                    vbank_num = td_text
                                                elif '기한' in th_text or '만료' in th_text:
                                                    vbank_deadline = td_text
                                                elif '금액' in th_text or '입금액' in th_text:
                                                    vbank_amount = td_text
                                                elif '예약번호' in th_text:
                                                    booking_id = td_text
                                                    
                                    success_msg = "예약 성공!"
                                    if "임시로" in done_text or "입금전" in done_text:
                                        success_msg += " (가상계좌 임시 예약 완료)"
                                        if vbank_num:
                                            success_msg += f" 계좌: {vbank_num}"
                                        if vbank_amount:
                                            success_msg += f", 금액: {vbank_amount}"
                                        if vbank_deadline:
                                            success_msg += f", 기한: {vbank_deadline}"
                                    else:
                                        success_msg += " (예약 확정 완료)"
                                        if booking_id:
                                            success_msg += f" 예약번호: {booking_id}"
                                            
                                    self.log(success_msg, "success")
                            except Exception as e:
                                self.log(f"예약 성공! (상세 정보 파싱 실패: {e})", "success")
                                
                            self.notify_success()
                            break
                        else:
                            await self.handle_error_async(step2_response, reservation_data, '최종예약')
                except Exception as e:
                    if self.stop_event.is_set():
                        break
                    csrf_token = None
                    self.log(f"{reservation_data['reservationTime'][:5]} 시도 중... (연결 오류 - 재시도)", "info")
                    await asyncio.sleep(0.1)
        finally:
            is_pooled = hasattr(self, "session_pool") and len(self.session_pool) > 0
            if not is_pooled:
                await session.close()

    async def get_csrf_token_async(self, session):
        async with session.get(f'{self.base_url}/reservation') as resp:
            text = await resp.text()
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
        
        headers = {
            'Content-Type': 'application/x-www-form-urlencoded',
            'X-Requested-With': 'XMLHttpRequest',
            'Referer': f'{self.base_url}/reservation',
            'Origin': self.base_url,
        }
        
        return await session.post(f'{self.base_url}/reservation/create', data=form_data, headers=headers)

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

    async def handle_error_async(self, response, reservation_data, step_name):
        import json
        time_slot = reservation_data['reservationTime'][:5]
        try:
            decoded_text = await response.text()
        except Exception:
            decoded_text = str(response)
            
        try:
            error_data = json.loads(decoded_text)
            if 'errors' in error_data and isinstance(error_data['errors'], dict):
                err_details = []
                for field, msgs in error_data['errors'].items():
                    if isinstance(msgs, list):
                        msg_str = ", ".join(msgs)
                    else:
                        msg_str = str(msgs)
                    err_details.append(f"{field}: {msg_str}")
                main_msg = error_data.get('message', 'The given data was invalid.')
                error_message = f"{main_msg} ({'; '.join(err_details)})"
            elif 'Message' in error_data:
                error_message = error_data['Message']
            elif 'message' in error_data:
                error_message = error_data['message']
            else:
                error_message = decoded_text[:200]
        except Exception:
            error_message = decoded_text[:200]
            
        if "이미 예약" in error_message:
            self.log(f"{time_slot} 시도 중... (이미 예약이 완료된 시간대, 해당 시간대 예약이 다시 열릴때까지 재시도)", "info")
        elif "결제 수단" in error_message or "결제수단" in error_message:
            self.log(f"{time_slot} 시도 중... (이미 예약이 완료된 시간대이거나 결제수단 오류 - 재시도)", "info")
        else:
            self.log(f"{time_slot} 시도 중... ({error_message}, 재시도)", "info")

    async def pre_fetch_sessions_async(self, num_sessions, reservation_data):
        import aiohttp
        import asyncio
        self.log(f"Pre-fetching {num_sessions} sessions and CSRF tokens...", "info")
        
        self.session_pool = []
        
        async def fetch_one(idx):
            headers = {
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
            }
            session = aiohttp.ClientSession(
                headers=headers,
                timeout=aiohttp.ClientTimeout(total=self.SUBMIT_TIMEOUT),
            )
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
