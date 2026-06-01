import requests
from bs4 import BeautifulSoup
import json
from engines.base_engine import BaseEngine

class ZeroWorldEngine(BaseEngine):
    def __init__(self, site_url, log_callback, success_callback=None):
        """
        Engine for Zeroworld (Gangnam/Hongdae).
        
        :param site_url: Base url for reservation (e.g. 'https://zerogangnam.com/reservation')
        """
        super().__init__(log_callback, success_callback)
        self.site_url = site_url

    def get_csrf_token(self, session):
        response = session.get(self.site_url)
        soup = BeautifulSoup(response.text, 'html.parser')
        csrf_token = soup.find('meta', {'name': 'csrf-token'})['content']
        return csrf_token

    def send_post_request(self, session, csrf_token, reservation_data):
        headers = {
            'Content-Type': 'application/json',
            'X-CSRF-TOKEN': csrf_token
        }
        response = session.post(self.site_url, json=reservation_data, headers=headers)
        return response

    def make_reservation_thread(self, reservation_data):
        session = requests.Session()
        csrf_token = None
        
        while not self.stop_event.is_set():
            try:
                if not csrf_token:
                    csrf_token = self.get_csrf_token(session)
                
                response = self.send_post_request(session, csrf_token, reservation_data)
                
                if response.status_code == 419:
                    csrf_token = None
                    continue
                
                if response.status_code == 200:
                    try:
                        success_info = response.json()
                    except Exception:
                        success_info = response.text
                    
                    self.log(f"Success: {success_info}", "success")
                    self.stop_event.set()
                    if self.success_callback:
                        self.success_callback()
                    break
                else:
                    time_slot = reservation_data['reservationTime'][:5]
                    try:
                        decoded_text = response.content.decode('utf-8')
                    except Exception:
                        decoded_text = response.text

                    try:
                        error_data = json.loads(decoded_text)
                        if 'Message' in error_data:
                            error_message = error_data['Message']
                        else:
                            error_message = decoded_text[:200]
                    except json.JSONDecodeError:
                        error_message = decoded_text[:200]

                    if "이미 예약" in error_message or "already" in error_message.lower():
                        self.log(f"{time_slot} 시도 중... (이미 예약이 완료된 시간대, 해당 시간대 예약이 다시 열릴때까지 재시도)", "info")
                    elif "결제 수단" in error_message or "결제수단" in error_message:
                        self.log(f"{time_slot} 시도 중... (이미 예약이 완료된 시간대이거나 결제수단 오류 - 재시도)", "info")
                    else:
                        self.log(f"{time_slot} 시도 중... ({error_message}, 재시도)", "info")
            except Exception as e:
                time_slot = reservation_data['reservationTime'][:5]
                # If network error occurs, clear token just in case session got broken
                csrf_token = None
                self.log(f"{time_slot} 시도 중... (연결 오류 - 재시도)", "info")

    async def make_reservation_async_task(self, reservation_data, task_idx):
        import aiohttp
        import asyncio
        import json
        from bs4 import BeautifulSoup
        
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
        }
        
        async with aiohttp.ClientSession(headers=headers) as session:
            csrf_token = None
            while not self.stop_event.is_set():
                try:
                    if not csrf_token:
                        csrf_token = await self.get_csrf_token_async(session)
                        
                    if self.stop_event.is_set():
                        break
                        
                    response = await self.send_post_request_async(session, csrf_token, reservation_data)
                    
                    if response.status == 419:
                        csrf_token = None
                        continue
                        
                    if response.status == 200:
                        try:
                            res_json = await response.json()
                            success_info = str(res_json)
                        except Exception:
                            success_info = await response.text()
                            
                        self.log(f"Success: {success_info[:200]}", "success")
                        self.stop_event.set()
                        if self.success_callback:
                            self.success_callback()
                        break
                    else:
                        time_slot = reservation_data['reservationTime'][:5]
                        try:
                            decoded_text = await response.text()
                        except Exception:
                            decoded_text = str(response)
                            
                        try:
                            error_data = json.loads(decoded_text)
                            if 'Message' in error_data:
                                error_message = error_data['Message']
                            else:
                                error_message = decoded_text[:200]
                        except Exception:
                            error_message = decoded_text[:200]
                            
                        if "이미 예약" in error_message or "already" in error_message.lower():
                            self.log(f"{time_slot} 시도 중... (이미 예약이 완료된 시간대, 해당 시간대 예약이 다시 열릴때까지 재시도)", "info")
                        elif "결제 수단" in error_message or "결제수단" in error_message:
                            self.log(f"{time_slot} 시도 중... (이미 예약이 완료된 시간대이거나 결제수단 오류 - 재시도)", "info")
                        else:
                            self.log(f"{time_slot} 시도 중... ({error_message}, 재시도)", "info")
                except Exception as e:
                    time_slot = reservation_data['reservationTime'][:5]
                    csrf_token = None
                    self.log(f"{time_slot} 시도 중... (연결 오류 - 재시도)", "info")
                    await asyncio.sleep(0.1)

    async def get_csrf_token_async(self, session):
        async with session.get(self.site_url) as resp:
            text = await resp.text()
            from bs4 import BeautifulSoup
            soup = BeautifulSoup(text, 'html.parser')
            csrf_token = soup.find('meta', {'name': 'csrf-token'})['content']
            return csrf_token

    async def send_post_request_async(self, session, csrf_token, reservation_data):
        headers = {
            'Content-Type': 'application/json',
            'X-CSRF-TOKEN': csrf_token
        }
        return await session.post(self.site_url, json=reservation_data, headers=headers)
