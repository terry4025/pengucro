import requests
from bs4 import BeautifulSoup
import json
import urllib.parse

BASE_URL = 'https://www.xn--2e0b040a4xj.com'

def get_csrf_token(session):
    response = session.get(f'{BASE_URL}/reservation')
    soup = BeautifulSoup(response.text, 'html.parser')
    return soup.find('meta', {'name': 'csrf-token'})['content']

def find_available_slots_and_check_payment(session, csrf_token, branch_id):
    # Today's date
    date_str = '2026-06-17'
    print(f"\nChecking available slots for Branch {branch_id} on {date_str}...")
    
    # Get reservation page for this branch
    url = f'{BASE_URL}/reservation?branch={branch_id}&date={date_str}#list'
    res = session.get(url)
    soup = BeautifulSoup(res.text, 'html.parser')
    
    # Find all reservation buttons that are active
    buttons = soup.find_all('button', class_='eveReservationButton')
    print(f"Found {len(buttons)} available slots.")
    
    if not buttons:
        # Try tomorrow's date
        date_str = '2026-06-18'
        print(f"No slots found. Trying {date_str}...")
        url = f'{BASE_URL}/reservation?branch={branch_id}&date={date_str}#list'
        res = session.get(url)
        soup = BeautifulSoup(res.text, 'html.parser')
        buttons = soup.find_all('button', class_='eveReservationButton')
        print(f"Found {len(buttons)} available slots on {date_str}.")
        
    for btn in buttons:
        # Get the hidden data
        hidden_data_div = btn.find('div', class_='eveHiddenData')
        if hidden_data_div:
            try:
                data = json.loads(hidden_data_div.text)
                theme_id = data.get('theme')
                time_val = data.get('time')
                date_val = data.get('date')
                
                # Let's check payment methods for this theme and time slot
                headers = {
                    'Content-Type': 'application/x-www-form-urlencoded',
                    'Referer': url,
                    'Origin': BASE_URL,
                }
                form_data = {
                    'branch': branch_id,
                    'theme': theme_id,
                    'date': date_val,
                    'time': time_val,
                    '_token': csrf_token,
                }
                
                create_res = session.post(f'{BASE_URL}/reservation/create', data=form_data, headers=headers)
                create_soup = BeautifulSoup(create_res.text, 'html.parser')
                
                # Check payment methods in the form
                payment_inputs = create_soup.find_all('input', {'name': 'payment_method'})
                methods = []
                for inp in payment_inputs:
                    lbl = create_soup.find('label', {'for': inp.get('id')})
                    methods.append({
                        'value': inp.get('value'),
                        'label': lbl.text.strip() if lbl else '',
                        'checked': inp.has_attr('checked')
                    })
                    
                print(f"  * Theme '{theme_id}' at {time_val} ({date_val}):")
                if methods:
                    for m in methods:
                        print(f"    - Value: '{m['value']}', Label: '{m['label']}', Checked: {m['checked']}")
                else:
                    # Let's search for the word '결제' or 'pay'
                    pay_text = create_soup.find(text=lambda t: t and '결제' in t)
                    print(f"    - No inputs. Text match: {pay_text}")
                    # Let's print form inputs to see if there is payment_method
                    form = create_soup.find('form')
                    if form:
                        inputs = form.find_all('input')
                        for inp in inputs:
                            if inp.get('name') == 'payment_method':
                                print(f"    - Found input in form: value={inp.get('value')}")
            except Exception as e:
                print(f"  Error checking slot: {e}")
                
session = requests.Session()
csrf = get_csrf_token(session)

# Check for Branch 1 (Daegu), Branch 2 (Hongdae Adventure), Branch 4 (Last City)
find_available_slots_and_check_payment(session, csrf, '1')
find_available_slots_and_check_payment(session, csrf, '2')
find_available_slots_and_check_payment(session, csrf, '4')
