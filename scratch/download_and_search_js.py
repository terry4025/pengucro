import requests
import re

url = 'https://www.xn--2e0b040a4xj.com/js/reservation.js'
res = requests.get(url)
text = res.text

print(f"File size: {len(text)} bytes")

# Search for '/confirm'
matches = re.findall(r'[\w\./\-]*confirm[\w\./\-]*', text)
print(f"\nConfirm keywords found: {len(matches)}")
for m in set(matches):
    print(f"  - {m}")
