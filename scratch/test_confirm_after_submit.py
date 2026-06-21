import requests
from bs4 import BeautifulSoup
import json

BASE_URL = 'https://www.xn--2e0b040a4xj.com'

def get_csrf_token(session):
    response = session.get(f'{BASE_URL}/reservation')
    soup = BeautifulSoup(response.text, 'html.parser')
    return soup.find('meta', {'name': 'csrf-token'})['content']

def check_and_book_and_confirm(session, csrf_token):
    # Find a clean slot tomorrow
    date_str = '2026-06-18'
    branch_id = '2' # Hongdae Adventure
    
    url = f'{BASE_URL}/reservation?branch={branch_id}&date={date_str}#list'
    res = session.get(url)
    soup = BeautifulSoup(res.text, 'html.parser')
    
    buttons = soup.find_all('button', class_='eveReservationButton')
    if not buttons:
        # Try today
        date_str = '2026-06-17'
        url = f'{BASE_URL}/reservation?branch={branch_id}&date={date_str}#list'
        res = session.get(url)
        soup = BeautifulSoup(res.text, 'html.parser')
        buttons = soup.find_all('button', class_='eveReservationButton')
        
    if not buttons:
        print("No available slots found.")
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
    
    # Step 3: GET /reservation/confirm (or is it /reservation/confirm#list or redirect?)
    res_confirm = session.get(f'{BASE_URL}/reservation/confirm')
    print(f"\nConfirm Page Status: {res_confirm.status_code}")
    print(f"Confirm Page URL: {res_confirm.url}")
    
    res_confirm.encoding = 'utf-8'
    soup_confirm = BeautifulSoup(res_confirm.text, 'html.parser')
    
    # Search for booking result details
    # Look for temporary reservation text
    info_section = soup_confirm.find(string=lambda s: s and '임시로 예약을 잡아두셨습니다' in s)
    if info_section:
        print("\n[SUCCESS] Confirmation page loaded successfully!")
        print(f"Text matched: {info_section.strip()}")
        
        # Let's extract the table details
        # Let's print out the text of any tables found on the page
        tables = soup_confirm.find_all('table')
        for i, table in enumerate(tables):
            print(f"\n--- Table {i+1} ---")
            for row in table.find_all('tr'):
                th = row.find('th')
                td = row.find('td')
                if th and td:
                    print(f"  {th.text.strip()}: {td.text.strip()}")
    else:
        print("\n[INFO] '임시로 예약을 잡아두셨습니다' not found directly.")
        # Print a snippet of the page text
        text_content = soup_confirm.get_text()
        # Find lines with numbers or important details
        lines = [line.strip() for line in text_content.split('\n') if line.strip()]
        print("\nFirst 40 lines of confirmation text:")
        for line in lines[:40]:
            print(f"  {line}")

session = requests.Session()
csrf = get_csrf_token(session)
check_and_book_and_confirm(session, csrf)
