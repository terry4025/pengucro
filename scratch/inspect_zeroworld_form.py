import requests
from bs4 import BeautifulSoup

url = "https://zeroworldkorea.com/layout/res/home.php?go=rev.make&s_subj=A&zizum_num=4&rev_days=2026-06-25"
headers = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
}
try:
    response = requests.get(url, headers=headers, timeout=10)
    print("Status Code:", response.status_code)
    
    # Save HTML for debugging
    with open("scratch/zeroworld_rev_make_shin.html", "w", encoding="utf-8") as f:
        f.write(response.text)
        
    soup = BeautifulSoup(response.text, "html.parser")
    forms = soup.find_all("form")
    print(f"Found {len(forms)} forms.")
    for idx, f in enumerate(forms):
        print(f"\nForm #{idx}: name={f.get('name')}, action={f.get('action')}, method={f.get('method')}")
        for i in f.find_all(["input", "select", "textarea"]):
            print(f"  Field: tag={i.name} name={i.get('name')} type={i.get('type')} value={i.get('value')}")
except Exception as e:
    print("Error:", e)
