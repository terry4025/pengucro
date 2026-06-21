with open("scratch/xdungeon_rev_make.html", "r", encoding="utf-8") as f:
    html = f.read()

import re
matches = list(re.finditer(r'captcha', html, re.I))
print(f"Found {len(matches)} matches for 'captcha':")
for m in matches[:10]:
    start = max(0, m.start() - 100)
    end = min(len(html), m.end() + 100)
    print("\n--- Match ---")
    print(html[start:end])
