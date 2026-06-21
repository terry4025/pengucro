import requests
from bs4 import BeautifulSoup
import json

BASE_URL = 'https://www.xn--2e0b040a4xj.com'

def get_csrf_token(session):
    response = session.get(f'{BASE_URL}/reservation')
    soup = BeautifulSoup(response.text, 'html.parser')
    return soup.find('meta', {'name': 'csrf-token'})['content']

def submit_reservation(session, csrf_token, data):
    headers = {
        'Content-Type': 'application/x-www-form-urlencoded',
        'X-CSRF-TOKEN': csrf_token,
        'Accept': 'application/json, text/javascript, */*; q=0.01',
        'Referer': f'{BASE_URL}/reservation/create',
        'Origin': BASE_URL,
    }
    form_data = {
        'date': data.get('date', '2026-06-18'),
        'name': data.get('name', '홍길동'),
        'phone': data.get('phone', '010-1234-5678'),
        'people': data.get('people', '2'),
        'theme': data.get('theme', '20'),
        'time': data.get('time', '20:40'),
        'branch': data.get('branch', '1'),
        'payment_method': data.get('payment_method', '1'),
        'policy': data.get('policy', 'on'),
        '_token': csrf_token,
    }
    res = session.post(f'{BASE_URL}/reservation', data=form_data, headers=headers)
    res.encoding = 'utf-8'
    return res

session = requests.Session()
csrf = get_csrf_token(session)

tests = [
    {"name": "Step 1: Missing date", "data": {"date": None}},
    {"name": "Step 1: Missing time", "data": {"time": None}},
    {"name": "Step 1: Missing branch", "data": {"branch": None}},
    {"name": "Step 1: Missing theme", "data": {"theme": None}},
    {"name": "Step 1: Invalid date format", "data": {"date": "abc"}},
    {"name": "Step 1: Invalid time format", "data": {"time": "99:99"}},
]

for t in tests:
    print(f"\n--- Running test: {t['name']} ---")
    form_data = {
        'date': '2026-06-19',
        'theme': '20',
        'time': '20:40',
        'branch': '1',
        '_token': csrf,
    }
    for k, v in t['data'].items():
        if v is None:
            if k in form_data:
                del form_data[k]
        else:
            form_data[k] = v
                
    headers = {
        'Content-Type': 'application/x-www-form-urlencoded',
        'X-Requested-With': 'XMLHttpRequest',
        'Referer': f'{BASE_URL}/reservation',
        'Origin': BASE_URL,
    }
    res = session.post(f'{BASE_URL}/reservation/create', data=form_data, headers=headers)
    res.encoding = 'utf-8'
    print(f"Step 1 Status: {res.status_code}")
    print(f"Step 1 Raw content: {res.content}")


