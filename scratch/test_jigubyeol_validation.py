import requests
from bs4 import BeautifulSoup
import json

BASE_URL = 'https://www.xn--2e0b040a4xj.com'

# Test data similar to what the user would use
reservation_data = {
    'branch': '1', # Daegu
    'themePK': '20', # Inca
    'reservationDate': '2026-06-18', # Future date to avoid "past time" error
    'reservationTime': '20:40:00',
    'name': '정세영',
    'phone': '010-8975-2709',
    'people': '4',
    'policy': 'true'
}

session = requests.Session()

def get_csrf_token(session):
    response = session.get(f'{BASE_URL}/reservation')
    soup = BeautifulSoup(response.text, 'html.parser')
    return soup.find('meta', {'name': 'csrf-token'})['content']

def submit_time_selection(session, csrf_token, data):
    headers = {
        'Content-Type': 'application/x-www-form-urlencoded',
        'Referer': f'{BASE_URL}/reservation',
        'Origin': BASE_URL,
    }
    form_data = {
        'branch': data['branch'],
        'theme': data['themePK'],
        'date': data['reservationDate'],
        'time': data['reservationTime'], # Send 8-character time
        '_token': csrf_token,
    }
    return session.post(f'{BASE_URL}/reservation/create', data=form_data, headers=headers)

def submit_reservation(session, csrf_token, data):
    headers = {
        'Content-Type': 'application/x-www-form-urlencoded',
        'X-CSRF-TOKEN': csrf_token,
        'Accept': 'application/json, text/javascript, */*; q=0.01',
        'Referer': f'{BASE_URL}/reservation/create',
        'Origin': BASE_URL,
    }
    form_data = {
        'date': data['reservationDate'],
        'name': data['name'],
        'phone': data['phone'],
        'people': data['people'],
        'theme': data['themePK'],
        'time': data['reservationTime'][:5],
        'branch': data['branch'],
        'payment_method': '1',
        'policy': 'on',
        '_token': csrf_token,
    }
    return session.post(f'{BASE_URL}/reservation', data=form_data, headers=headers)

try:
    print("Fetching CSRF...")
    csrf = get_csrf_token(session)
    print(f"CSRF: {csrf}")

    print("Step 1 (submit_time_selection)...")
    res1 = submit_time_selection(session, csrf, reservation_data)
    print(f"Step 1 status: {res1.status_code}")
    print(f"Step 1 content: {res1.content}")
    try:
        print(f"Step 1 decoded: {res1.content.decode('utf-8')}")
    except Exception as e:
        print(f"Step 1 decode failed: {e}")

    print("Step 2 (submit_reservation)...")
    res2 = submit_reservation(session, csrf, reservation_data)
    print(f"Step 2 status: {res2.status_code}")
    print(f"Raw content: {res2.content}")
    try:
        print(f"Decoded UTF-8: {res2.content.decode('utf-8')}")
    except Exception as e:
        print(f"UTF-8 decode failed: {e}")
    try:
        print(f"Decoded CP949: {res2.content.decode('cp949')}")
    except Exception as e:
        print(f"CP949 decode failed: {e}")
    try:
        res2.encoding = 'utf-8'
        print(f"Step 2 response JSON:\n{json.dumps(res2.json(), indent=2, ensure_ascii=False)}")
    except Exception:
        pass
except Exception as e:
    print(f"Error: {e}")
