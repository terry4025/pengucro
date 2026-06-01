import requests
from bs4 import BeautifulSoup
import json
from engines.base_engine import BaseEngine

class JigubyeolEngine(BaseEngine):
    def __init__(self, log_callback, success_callback=None, site_url=None):
        super().__init__(log_callback, success_callback)
        self.base_url = site_url if site_url else 'https://www.xn--2e0b040a4xj.com'

    def get_csrf_token(self, session):
        response = session.get(f'{self.base_url}/reservation')
        soup = BeautifulSoup(response.text, 'html.parser')
        csrf_token = soup.find('meta', {'name': 'csrf-token'})['content']
        return csrf_token

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
        response = session.post(f'{self.base_url}/reservation/create', data=form_data, headers=headers)
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
            
        response = session.post(endpoint, data=form_data, headers=headers)
        return response

    def make_reservation_thread(self, reservation_data):
        session = requests.Session()
        csrf_token = None
        
        while not self.stop_event.is_set():
            try:
                if not csrf_token:
                    csrf_token = self.get_csrf_token(session)
                
                step1_response = self.submit_time_selection(session, csrf_token, reservation_data)
                
                if step1_response.status_code == 419:
                    csrf_token = None
                    continue
                
                if step1_response.status_code == 200:
                    soup = BeautifulSoup(step1_response.text, 'html.parser')
                    meta_csrf = soup.find('meta', {'name': 'csrf-token'})
                    if meta_csrf:
                        csrf_token = meta_csrf['content']
                    
                    # Parse payment_method dynamically
                    payment_method = '1'
                    pm_inputs = soup.find_all('input', {'name': 'payment_method'})
                    if pm_inputs:
                        checked_pm = next((i for i in pm_inputs if i.get('checked') is not None), None)
                        if checked_pm:
                            payment_method = checked_pm.get('value', '1')
                        else:
                            payment_method = pm_inputs[0].get('value', '1')
                    
                    if self.stop_event.is_set():
                        break
                    with self.submission_lock:
                        if self.stop_event.is_set():
                            break
                        step2_response = self.submit_reservation(session, csrf_token, reservation_data, payment_method)
                        
                        if step2_response.status_code == 419:
                            csrf_token = None
                            continue
                            
                        if step2_response.status_code == 200 or step2_response.status_code == 201:
                            self.log(f"Success: {step2_response.text[:200]}", "success")
                            self.stop_event.set()
                            if self.success_callback:
                                self.success_callback()
                            break
                        else:
                            self.handle_error(step2_response, reservation_data, '최종예약')
                else:
                    self.handle_error(step1_response, reservation_data, '시간선택')
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
            if 'Message' in error_data:
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
