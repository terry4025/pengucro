import requests
from bs4 import BeautifulSoup
import json

BASE_URL = 'https://www.xn--2e0b040a4xj.com'

def get_csrf_token(session):
    response = session.get(f'{BASE_URL}/reservation')
    soup = BeautifulSoup(response.text, 'html.parser')
    return soup.find('meta', {'name': 'csrf-token'})['content']

def check_and_book_and_done(session, csrf_token):
    # Find a clean slot tomorrow
    date_str = '2026-06-18'
    branch_id = '2' # Hongdae Adventure
    
    url = f'{BASE_URL}/reservation?branch={branch_id}&date={date_str}#list'
    res = session.get(url)
    soup = BeautifulSoup(res.text, 'html.parser')
    
    buttons = soup.find_all('button', class_='eveReservationButton')
    if not buttons:
        date_str = '2026-06-17'
        url = f'{BASE_URL}/reservation?branch={branch_id}&date={date_str}#list'
        res = session.get(url)
        soup = BeautifulSoup(res.text, 'html.parser')
        buttons = soup.find_all('button', class_='eveReservationButton')
        
    if not buttons:
        print("No slots found.")
        return
        
    btn = buttons[0]
    hidden_data = json.loads(btn.find('div', class_='eveHiddenData').text)
    theme_id = hidden_data['theme']
    time_val = hidden_data['time']
    date_val = hidden_data['date']
    
    print(f"Selected slot: Theme {theme_id}, Date {date_val}, Time {time_val}")
    
    # Step 1: 시간 선택 선등록
    headers = {
        'Content-Type': 'application/x-www-form-urlencoded',
        'Referer': url,
        'Origin': BASE_URL,
    }
    form_data_step1 = {
        'branch': branch_id,
        'theme': theme_id,
        'date': date_val,
        'time': time_val,
        '_token': csrf_token,
    }
    session.post(f'{BASE_URL}/reservation/create', data=form_data_step1, headers=headers)
    
    # Step 2: 최종 예약 완료
    headers_step2 = {
        'Content-Type': 'application/x-www-form-urlencoded',
        'X-CSRF-TOKEN': csrf_token,
        'Accept': 'application/json, text/javascript, */*; q=0.01',
        'Referer': f'{BASE_URL}/reservation/create',
        'Origin': BASE_URL,
    }
    form_data_step2 = {
        'branch': branch_id,
        'theme': theme_id,
        'date': date_val,
        'time': time_val,
        'name': '정세영',
        'phone': '010-8975-2709',
        'people': '2',
        'payment_method': '21',
        'policy': 'on',
        '_token': csrf_token,
    }
    
    res_step2 = session.post(f'{BASE_URL}/reservation/payment', data=form_data_step2, headers=headers_step2)
    print(f"Step 2 Status: {res_step2.status_code}")
    print(f"Step 2 Response: {res_step2.text}")
    
    # Step 3: GET /reservation/done
    res_done = session.get(f'{BASE_URL}/reservation/done')
    res_done.encoding = 'utf-8'
    print(f"Done Page Status: {res_done.status_code}")
    print(f"Done Page URL: {res_done.url}")
    
    # Save the HTML to scratch/done.html
    with open('scratch/done.html', 'w', encoding='utf-8') as f:
        f.write(res_done.text)
    print("Saved HTML to scratch/done.html")
    
    # Search for keywords in raw text
    raw_text = res_done.text
    print("\nSearch results:")
    print(f"  - '정세영' in HTML: {'정세영' in raw_text}")
    print(f"  - '010-8975-2709' in HTML: {'010-8975-2709' in raw_text}")
    print(f"  - '임시로' in HTML: {'임시로' in raw_text}")
    print(f"  - '가상계좌' in HTML: {'가상계좌' in raw_text}")
    
    soup_done = BeautifulSoup(raw_text, 'html.parser')
    info_section = soup_done.find(string=lambda s: s and '임시로 예약을 잡아두셨습니다' in s)
    if info_section:
        print("\n[SUCCESS] Done page contains the booking details!")
        tables = soup_done.find_all('table')
        for i, table in enumerate(tables):
            print(f"\n--- Table {i+1} ---")
            for row in table.find_all('tr'):
                th = row.find('th')
                td = row.find('td')
                if th and td:
                    print(f"  {th.text.strip()}: {td.text.strip()}")
    else:
        print("\n[FAIL] Info section not found in done page.")
        # print some text
        print(raw_text[:1000])

session = requests.Session()
csrf = get_csrf_token(session)
check_and_book_and_done(session, csrf)
