import re

with open("scratch/xdungeon_rev_make.html", "r", encoding="utf-8") as f:
    html = f.read()

# Let's find function fun_submit() and read until the next script block closing or similar
match = re.search(r'function fun_submit\(\)\s*\{(.*?)\n\}', html, re.DOTALL)
if match:
    print(match.group(0))
else:
    # Let's print the first 1000 characters from where it matches
    m = re.search(r'function fun_submit\(\)', html)
    if m:
        start = m.start()
        print(html[start:start+1500])
