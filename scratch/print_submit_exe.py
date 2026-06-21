import re

with open("scratch/xdungeon_rev_make.html", "r", encoding="utf-8") as f:
    html = f.read()

m = re.search(r'function fun_submit_exe\(\)', html)
if m:
    start = m.start()
    print(html[start:start+1000])
else:
    print("fun_submit_exe not found")
