import requests
from bs4 import BeautifulSoup
import urllib.parse

url = "https://xdungeon.net/layout/res/home.php?go=rev.main"
headers = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
}

try:
    response = requests.get(url, headers=headers, timeout=10)
    print("Status Code:", response.status_code)
    soup = BeautifulSoup(response.text, "html.parser")
    
    # Print forms
    forms = soup.find_all("form")
    print("\n--- Forms ---")
    for f in forms:
        print("Form action:", f.get("action"), "method:", f.get("method"))
        
    # Print select elements
    selects = soup.find_all("select")
    print("\n--- Selects ---")
    for s in selects:
        print("Select name:", s.get("name"), "id:", s.get("id"))
        for opt in s.find_all("option")[:5]:
            print("  Option value:", opt.get("value"), "text:", opt.text.strip())
            
    # Print input elements
    inputs = soup.find_all("input")
    print("\n--- Inputs ---")
    for i in inputs[:10]:
        print("Input name:", i.get("name"), "type:", i.get("type"), "value:", i.get("value"))
        
    # Search for script tags containing ajax or act
    print("\n--- Scripts snippet ---")
    scripts = soup.find_all("script")
    for scr in scripts:
        if scr.string and ("ajax" in scr.string or "act.php" in scr.string or "rev" in scr.string):
            print(scr.string[:500])
            print("...")
            
except Exception as e:
    print("Error:", e)
