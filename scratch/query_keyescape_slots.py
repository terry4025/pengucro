import requests
import json

# Let's query an open date on Keyescape
# zizumNum = 2 (Hongdae)
# date = 2026-06-25 (Should be open)
# themeNum = 18 (Money Money Package / 머니머니패키지)

r = requests.post("https://www.keyescape.com/controller/run_proc.php", data={
    't': 'get_theme_time',
    'date': '2026-06-25',
    'zizumNum': '2',
    'themeNum': '18',
    'endDay': '0'
})

print("Status Code:", r.status_code)
try:
    data = r.json()
    print("Response JSON:")
    print(json.dumps(data, indent=2, ensure_ascii=False))
except Exception as e:
    print("Failed to parse JSON:", e)
    print("Response text:", r.text[:500])
