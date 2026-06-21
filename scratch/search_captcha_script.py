with open("scratch/xdungeon_rev_make.html", "r", encoding="utf-8") as f:
    html = f.read()

import re
matches = re.finditer(r'captcha_img', html)
for m in matches:
    start = max(0, m.start() - 150)
    end = min(len(html), m.end() + 250)
    print("\n--- Match ---")
    print(html[start:end])
