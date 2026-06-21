import requests
from bs4 import BeautifulSoup

url = "https://xdungeon.net/layout/res/home.php?go=rev.make&crypt_data=SGYyT3pjeUJxRFFlWnQydTM3dSt5RkdJNkU2bXdMcEdCcjF6Zmt1V3J5Y25mY0xOU2hQQVBUQWJNZnNJeExLTQ=="
headers = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
}
try:
    response = requests.get(url, headers=headers, timeout=10)
    print("Status Code:", response.status_code)
    soup = BeautifulSoup(response.text, "html.parser")
    
    # Save the page for inspection
    with open("scratch/xdungeon_rev_make.html", "w", encoding="utf-8") as f:
        f.write(response.text)
    print("Saved to scratch/xdungeon_rev_make.html")
    
    # Print forms
    forms = soup.find_all("form")
    print("\n--- Forms ---")
    for f in forms:
        print("Form Name:", f.get("name"), "Action:", f.get("action"), "Method:", f.get("method"))
        for i in f.find_all(["input", "select", "textarea"]):
            print(f"  Field: tag={i.name} name={i.get('name')} type={i.get('type')} value={i.get('value')}")
            
except Exception as e:
    print("Error:", e)
