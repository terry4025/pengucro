import requests
from bs4 import BeautifulSoup
import json

BASE_URL = 'https://www.xn--2e0b040a4xj.com'

def get_csrf_token(session):
    response = session.get(f'{BASE_URL}/reservation')
    soup = BeautifulSoup(response.text, 'html.parser')
    return soup.find('meta', {'name': 'csrf-token'})['content']

def check_and_book_virtual_account(session, csrf_token):
    # Today's date or tomorrow
    date_str = '2026-06-18' # Let's try tomorrow
    branch_id = '2' # Hongdae Adventure
    
    url = f'{BASE_URL}/reservation?branch={branch_id}&date={date_str}#list'
    res = session.get(url)
    soup = BeautifulSoup(res.text, 'html.parser')
    
    buttons = soup.find_all('button', class_='eveReservationButton')
    if not buttons:
        print(f"No available slots for branch {branch_id} on {date_str}. Trying today...")
        date_str = '2026-06-17'
        url = f'{BASE_URL}/reservation?branch={branch_id}&date={date_str}#list'
        res = session.get(url)
        soup = BeautifulSoup(res.text, 'html.parser')
        buttons = soup.find_all('button', class_='eveReservationButton')
        
    if not buttons:
        print("No available slots found on either day.")
        return
        
    # Pick the first available slot
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
    res_step1 = session.post(f'{BASE_URL}/reservation/create', data=form_data_step1, headers=headers)
    print(f"Step 1 Status: {res_step1.status_code}")
    
    # Step 2: 최종 예약 완료 (payment_method = '21' -> /reservation/payment)
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
        'payment_method': '21', # Virtual account
        'policy': 'on',
        '_token': csrf_token,
    }
    
    res_step2 = session.post(f'{BASE_URL}/reservation/payment', data=form_data_step2, headers=headers_step2)
    print(f"Step 2 Status: {res_step2.status_code}")
    print(f"Step 2 Headers: {dict(res_step2.headers)}")
    print(f"Step 2 History: {res_step2.history}")
    print(f"Step 2 Final URL: {res_step2.url}")
    
    # Check if redirect or direct page
    res_step2.encoding = 'utf-8'
    text = res_step2.text
    print(f"\nStep 2 Text Length: {len(text)}")
    print("Step 2 Text Snippet (first 1000 chars):")
    print(text[:1000])
    
    # Parse for success indicators
    soup_step2 = BeautifulSoup(text, 'html.parser')
    success_indicator = soup_step2.find(text=lambda t: t and '임시로 예약을 잡아두셨습니다' in t)
    print(f"\nSuccess indicator found: {success_indicator is not None}")
    
    # Try to find virtual account info
    vbank_num = soup_step2.find(text=lambda t: t and '가상계좌번호' in t)
    print(f"Vbank number element: {vbank_num}")
    if vbank_num:
        # Get parent or sibling td
        td = vbank_num.parent
        print(f"Vbank parent html: {td}")
        # Let's print the table content containing vbank info
        table = soup_step2.find('table')
        if table:
            print(f"Table html:\n{table.prettify()[:1000]}")

session = requests.Session()
csrf = get_csrf_token(session)
check_and_book_virtual_account(session, csrf)
