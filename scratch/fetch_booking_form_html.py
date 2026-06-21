import requests
from bs4 import BeautifulSoup
import json

BASE_URL = 'https://www.xn--2e0b040a4xj.com'

def get_csrf_token(session):
    response = session.get(f'{BASE_URL}/reservation')
    soup = BeautifulSoup(response.text, 'html.parser')
    return soup.find('meta', {'name': 'csrf-token'})['content']

def save_booking_form(session, csrf_token):
    # Find a slot tomorrow
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
    
    # Save the HTML to scratch/booking_form.html
    with open('scratch/booking_form.html', 'w', encoding='utf-8') as f:
        f.write(res_step1.text)
    print("Saved HTML to scratch/booking_form.html")

session = requests.Session()
csrf = get_csrf_token(session)
save_booking_form(session, csrf)
