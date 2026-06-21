import requests
from bs4 import BeautifulSoup

BASE_URL = 'https://www.xn--2e0b040a4xj.com'

def get_csrf_token(session):
    response = session.get(f'{BASE_URL}/reservation')
    soup = BeautifulSoup(response.text, 'html.parser')
    return soup.find('meta', {'name': 'csrf-token'})['content']

def check_payment_methods(session, csrf_token, branch, theme, date, time):
    headers = {
        'Content-Type': 'application/x-www-form-urlencoded',
        'Referer': f'{BASE_URL}/reservation',
        'Origin': BASE_URL,
    }
    form_data = {
        'branch': branch,
        'theme': theme,
        'date': date,
        'time': time,
        '_token': csrf_token,
    }
    res = session.post(f'{BASE_URL}/reservation/create', data=form_data, headers=headers)
    soup = BeautifulSoup(res.text, 'html.parser')
    
    # Find payment method radios or inputs
    payment_inputs = soup.find_all('input', {'name': 'payment_method'})
    methods = []
    for inp in payment_inputs:
        methods.append({
            'value': inp.get('value'),
            'id': inp.get('id'),
            'checked': inp.has_attr('checked'),
            'label': soup.find('label', {'for': inp.get('id')}).text.strip() if inp.get('id') else ''
        })
        
    # Also search for any select dropdown for payment
    payment_select = soup.find('select', {'name': 'payment_method'})
    if payment_select:
        for opt in payment_select.find_all('option'):
            methods.append({
                'value': opt.get('value'),
                'label': opt.text.strip()
            })
            
    print(f"\n[Branch {branch} - Theme {theme}]")
    print(f"Status Code: {res.status_code}")
    print(f"Payment Methods Found ({len(methods)}):")
    for m in methods:
        print(f"  - Value: '{m['value']}', Label: '{m['label']}', Checked: {m.get('checked')}")
        
    if not methods:
        # Print some context if not found
        # Check if there is an error message on the page
        err_box = soup.find(class_='error') or soup.find(class_='alert')
        if err_box:
            print(f"  Possible error on page: {err_box.text.strip()}")
        else:
            # Look for payment methods in text
            print("  No payment inputs found. HTML snippet:")
            # find anything with payment
            pay_div = soup.find(id=lambda x: x and 'payment' in x.lower())
            if pay_div:
                print(pay_div.prettify()[:500])
            else:
                print(res.text[:1000])

session = requests.Session()
csrf = get_csrf_token(session)

# 1. Daegu Inca (Branch 1, Theme 20)
check_payment_methods(session, csrf, '1', '20', '2026-06-25', '20:40')

# 2. Hongdae Adventure Janhyang (Branch 2, Theme 23)
check_payment_methods(session, csrf, '2', '23', '2026-06-25', '23:05')

# 3. Last City Stella (Branch 4, Theme 24)
check_payment_methods(session, csrf, '4', '24', '2026-06-25', '22:40')
