from bs4 import BeautifulSoup
import re

with open("scratch/xdungeon_rev_make.html", "r", encoding="utf-8") as f:
    html = f.read()

# Match the fun_submit function block
match = re.search(r'function fun_submit\(\).*?}', html, re.DOTALL)
if match:
    print(match.group(0))
else:
    print("fun_submit not found")
