import requests
from bs4 import BeautifulSoup
import time
import json
import threading

# Function to get a new CSRF token
def get_csrf_token(session):
    response = session.get('https://zerogangnam.com/reservation')
    soup = BeautifulSoup(response.text, 'html.parser')
    csrf_token = soup.find('meta', {'name': 'csrf-token'})['content']
    return csrf_token

# Function to send a POST request
def send_post_request(session, csrf_token, reservation_data):
    headers = {
        'Content-Type': 'application/json',
        'X-CSRF-TOKEN': csrf_token
    }
    response = session.post('https://zerogangnam.com/reservation', json=reservation_data, headers=headers)
    return response

# Function to try making a reservation
def make_reservation(reservation_data, stop_event):
    session = requests.Session()

    while not stop_event.is_set():
        csrf_token = get_csrf_token(session)  # Always get a new CSRF token
        response = send_post_request(session, csrf_token, reservation_data)
        if response.status_code == 200:
            print('Success:', response.json())
            stop_event.set()  # Signal other threads to stop
            break
        else:
            # 원본 오류 메시지
            original_message = response.text

            # JSON 데이터로 로드하여 'Message' 값 추출
            try:
                error_data = json.loads(original_message)
                if 'Message' in error_data:
                    error_message = error_data['Message']
                else:
                    error_message = original_message
            except json.JSONDecodeError:
                error_message = original_message

            # 오류 메시지를 디코딩
            decoded_message = bytes(error_message, 'utf-8').decode('unicode_escape')

            print(f'Failed for time slot {reservation_data["reservationTime"]}:', response.status_code, decoded_message)
            # Wait for a short period before retrying
            #time.sleep(2)  # Adjust the delay as necessary

# Original reservation data
original_reservation_data = {
    'reservationDate': '2026-05-29',
    'name': '장석환',
    'phone': '010-2194-7760',
    'people': '2',
    'paymentType': '1',
    'themePK': '27',
    'reservationTime': '16:20:00',
    'policy': 'true'
}
# themePK:
# 평일 
# ALIVE  51 / 사랑하는감 54 / 깜방탈출 WAYOUT 56 / NOX 58 / 층간소음 60
# 링 23  /  나비효과 25 / 콜러 27 / 어느겨울밤2 29 / 아이엠 31 / 제로호텔L 37 / DONE 41 /포레스트 43 / 헐! 45
# 제로호텔 1 / 탈옥:특별수용소 3 / 어느겨울밤 5 / 인형괴담 7 / 성역전설 9 / 최면 13 / 복희네사진관 15 / 피노키오 대탈출 17 / 검은사원 19 /해리포터의 모험 21
 # 주말
# ALIVE  52 / 사랑하는감 55 / 깜방탈출 WAYOUT 57 / NOX 59 / 층간소음 61    
# 링 24  / 나비효과 26 / 콜러 28 / 어느겨울밤2 30 / 아이엠 32 / 제로호텔L 38 / DONE 42 /포레스트 44 / 헐! 46 
# 제로호텔 2 / 탈옥:특별수용소 4 / 어느겨울밤 6 / 인형괴담 8 / 성역전설 10 / 최면 14 / 복희네사진관 16 / 피노키오 대탈출 18 / 검은사원 20 /해리포터의 모험 22

# Number of threads you want to run
num_threads = 30

# Create a list of reservation data for the desired number of threads
reservation_data_list = [original_reservation_data for _ in range(num_threads)]

# Event to signal when a reservation is successful
stop_event = threading.Event()

# Create and start a thread for each reservation data
threads = []
for reservation_data in reservation_data_list:
    thread = threading.Thread(target=make_reservation, args=(reservation_data, stop_event))
    threads.append(thread)
    thread.start()

# Wait for all threads to complete
for thread in threads:
    thread.join()

print('Reservation attempt finished.')
