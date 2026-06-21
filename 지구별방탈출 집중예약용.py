import requests
from bs4 import BeautifulSoup
import time
import json
import threading

# ============================================================
# 지구별방탈출 집중예약 프로그램
# 사이트: https://www.xn--2e0b040a4xj.com
# ============================================================

BASE_URL = 'https://www.xn--2e0b040a4xj.com'

# ============================================================
# 지점 정보
# ============================================================
# 1 = 대구점
# 2 = 홍대어드벤처점
# 4 = 홍대라스트시티점

# ============================================================
# 테마 PK (themePK) 목록
# ============================================================
# [대구점 (branch=1)]
# 잉카 20 / 우리 아빠 11 / 사명:투쟁의 노래 6 / 펭귄키우기 5
# 너의 겨울은 가고, 봄은 온다 3 / 만월<<꿈을 훔치는 요괴>> 2 / 단디해라 1
#
# [홍대어드벤처점 (branch=2)]
# PINOCCHIO(피노키오) 25 / 잔향 23 / 아몬:새벽을 여는 소년 18 / 퀘스트:여정의 시작 17
#
# [홍대라스트시티점 (branch=4)]
# 스텔라 24 / 카부트 22 / alone(얼론) 21 / 라스트코어 19 / 紋身(문신) 15

submission_lock = threading.Lock()

def get_csrf_token(session):
    response = session.get(f'{BASE_URL}/reservation')
    soup = BeautifulSoup(response.text, 'html.parser')
    csrf_token = soup.find('meta', {'name': 'csrf-token'})['content']
    return csrf_token

def submit_time_selection(session, csrf_token, reservation_data):
    headers = {
        'Content-Type': 'application/x-www-form-urlencoded',
        'Referer': f'{BASE_URL}/reservation',
        'Origin': BASE_URL,
    }
    form_data = {
        'branch': reservation_data['branch'],
        'theme': reservation_data['themePK'],
        'date': reservation_data['reservationDate'],
        'time': reservation_data['reservationTime'],
        '_token': csrf_token,
    }
    response = session.post(f'{BASE_URL}/reservation/create', data=form_data, headers=headers)
    return response

def submit_reservation(session, csrf_token, reservation_data, payment_method):
    headers = {
        'Content-Type': 'application/x-www-form-urlencoded',
        'X-CSRF-TOKEN': csrf_token,
        'Referer': f'{BASE_URL}/reservation/create',
        'Origin': BASE_URL,
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
    
    if payment_method != '1':
        endpoint = '/reservation/payment'
    else:
        endpoint = '/reservation'
        
    response = session.post(f'{BASE_URL}{endpoint}', data=form_data, headers=headers)
    return response

def make_reservation(reservation_data, stop_event):
    session = requests.Session()

    while not stop_event.is_set():
        try:
            csrf_token = get_csrf_token(session)

            if stop_event.is_set():
                break
            with submission_lock:
                if stop_event.is_set():
                    break
                
                # Step 1: 시간 선택 선등록
                step1_response = submit_time_selection(session, csrf_token, reservation_data)
                if step1_response.status_code not in (200, 201):
                    handle_error(step1_response, reservation_data, '시간선택')
                    continue
                
                # Step 2: 최종 예약 완료
                try:
                    decoded_html = step1_response.content.decode('utf-8')
                except Exception:
                    decoded_html = step1_response.text
                
                soup = BeautifulSoup(decoded_html, 'html.parser')
                payment_input = soup.find('input', {'name': 'payment_method'})
                payment_method = payment_input.get('value', '1') if payment_input else '1'
                
                step2_response = submit_reservation(session, csrf_token, reservation_data, payment_method)

                if step2_response.status_code in (200, 201):
                    try:
                        done_url = f"{BASE_URL}/reservation/done"
                        done_res = session.get(done_url)
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
                        print(success_msg)
                    except Exception as e:
                        print(f"예약 성공! (상세 정보 파싱 실패: {e})")
                        
                    stop_event.set()
                    break
                else:
                    handle_error(step2_response, reservation_data, '최종예약')

        except Exception as e:
            print(f'Error (time slot {reservation_data["reservationTime"]}): {str(e)}')

def handle_error(response, reservation_data, step_name):
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
            decoded_message = f"{main_msg} ({'; '.join(err_details)})"
        elif 'Message' in error_data:
            decoded_message = error_data['Message']
        elif 'message' in error_data:
            decoded_message = error_data['message']
        else:
            decoded_message = decoded_text[:200]
    except (json.JSONDecodeError, ValueError):
        decoded_message = decoded_text[:200]

    print(f'Failed [{step_name}] for time slot {reservation_data["reservationTime"]}:', response.status_code, decoded_message)


def print_banner():
    print('=' * 60)
    print('  지구별방탈출 집중예약 프로그램')
    print('  사이트: https://www.xn--2e0b040a4xj.com')
    print('=' * 60)
    print()

def print_branches():
    print('[지점 목록]')
    print('  1 = 대구점')
    print('  2 = 홍대어드벤처점')
    print('  4 = 홍대라스트시티점')
    print()

def print_themes(branch):
    print(f'[테마 목록 - 지점 {branch}]')
    if branch == '1':
        print('  20 = 잉카')
        print('  11 = 우리 아빠')
        print('   6 = 사명:투쟁의 노래')
        print('   5 = 펭귄키우기')
        print('   3 = 너의 겨울은 가고, 봄은 온다')
        print('   2 = 만월<<꿈을 훔치는 요괴>>')
        print('   1 = 단디해라')
    elif branch == '2':
        print('  25 = PINOCCHIO(피노키오)')
        print('  23 = 잔향')
        print('  18 = 아몬:새벽을 여는 소년')
        print('  17 = 퀘스트:여정의 시작')
    elif branch == '4':
        print('  24 = 스텔라')
        print('  22 = 카부트')
        print('  21 = alone(얼론)')
        print('  19 = 라스트코어')
        print('  15 = 紋身(문신)')
    print()

def get_user_input():
    print_banner()
    print_branches()

    branch = input('지점 번호를 입력하세요 (1/2/4): ').strip()
    while branch not in ['1', '2', '4']:
        branch = input('올바른 지점 번호를 입력하세요 (1/2/4): ').strip()

    print()
    print_themes(branch)

    theme_pk = input('테마 PK를 입력하세요: ').strip()
    reservation_date = input('예약 날짜를 입력하세요 (예: 2026-06-01): ').strip()
    reservation_time = input('예약 시간을 입력하세요 (예: 14:00): ').strip()

    if len(reservation_time) == 5:
        reservation_time += ':00'

    name = input('예약자 이름을 입력하세요: ').strip()
    phone = input('전화번호를 입력하세요 (예: 010-1234-5678): ').strip()
    people = input('인원 수를 입력하세요: ').strip()

    reservation_data = {
        'branch': branch,
        'reservationDate': reservation_date,
        'name': name,
        'phone': phone,
        'people': people,
        'themePK': theme_pk,
        'reservationTime': reservation_time,
        'policy': 'true',
    }

    print()
    print('-' * 60)
    print('예약 정보 확인')
    print(f'  지점: {branch}')
    print(f'  테마PK: {theme_pk}')
    print(f'  날짜: {reservation_date}')
    print(f'  시간: {reservation_time}')
    print(f'  이름: {name}')
    print(f'  전화번호: {phone}')
    print(f'  인원: {people}명')
    print('-' * 60)

    confirm = input('위 정보로 예약을 시작할까요? (y/n): ').strip().lower()
    if confirm != 'y':
        print('예약이 취소되었습니다.')
        return None

    return reservation_data


if __name__ == '__main__':
    reservation_data = get_user_input()

    if reservation_data is None:
        exit()

    num_threads = 30

    print()
    print(f'{num_threads}개 스레드로 예약 시도를 시작합니다...')
    print()

    reservation_data_list = [reservation_data for _ in range(num_threads)]

    stop_event = threading.Event()

    threads = []
    for data in reservation_data_list:
        thread = threading.Thread(target=make_reservation, args=(data, stop_event))
        threads.append(thread)
        thread.start()

    for thread in threads:
        thread.join()

    print()
    print('Reservation attempt finished.')
