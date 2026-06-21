import requests
import json

r = requests.post("https://www.keyescape.com/controller/run_proc.php", data={
    't': 'get_theme_time',
    'date': '2026-06-26',
    'zizumNum': '19',
    'themeNum': '60',
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
