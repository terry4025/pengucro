with open("scratch/xdungeon_rev_make.html", "r", encoding="utf-8") as f:
    html = f.read()

# Search for any img tag or script that has "captcha" or "key" or "code"
import re
from bs4 import BeautifulSoup
soup = BeautifulSoup(html, "html.parser")

print("--- Captcha related tags ---")
# Search for input_captcha surrounding tags
captcha_input = soup.find(attrs={"name": "input_captcha"})
if captcha_input:
    curr = captcha_input
    for depth in range(4):
        if not curr:
            break
        print(f"Parent Depth {depth}: tag={curr.name}, class={curr.get('class')}")
        # Print child tags inside it
        child_imgs = curr.find_all("img")
        for img in child_imgs:
            print(f"  Found image: src={img.get('src')}, class={img.get('class')}")
        curr = curr.parent
else:
    print("input_captcha input not found in DOM")
