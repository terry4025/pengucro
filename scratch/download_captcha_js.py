import requests

url = "https://xdungeon.net/core/captcha/jscript.captcha.js"
headers = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
}
try:
    response = requests.get(url, headers=headers, timeout=10)
    print("Status:", response.status_code)
    print("JS Code:")
    print(response.text[:2000])
    with open("scratch/jscript.captcha.js", "w", encoding="utf-8") as f:
        f.write(response.text)
except Exception as e:
    print("Error:", e)
