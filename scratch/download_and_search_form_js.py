import requests
import re

url = 'https://www.xn--2e0b040a4xj.com/js/reservation.form.js'
res = requests.get(url)
text = res.text

print(f"File size: {len(text)} bytes")

# Find redirects or location.href or location.replace
matches = re.findall(r'(?:window\.)?location\.(?:href|replace)\s*=\s*[^;\}]+', text)
print("\nLocation redirects found:")
for m in matches[:15]:
    print(f"  {m}")

# Search for /reservation or /payment or confirm or alert
matches_kw = re.findall(r'[^"\']*(?:/reservation|/payment|confirm|alert)[^"\']*', text)
print(f"\nKeyword matches found: {len(matches_kw)}")
for m in matches_kw[:30]:
    print(f"  - {m[:120]}")
