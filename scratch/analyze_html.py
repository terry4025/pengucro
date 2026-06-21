from bs4 import BeautifulSoup
import re

with open("scratch/xdungeon_rev_main.html", "r", encoding="utf-8") as f:
    soup = BeautifulSoup(f.read(), "html.parser")

print("--- SELECTS & OPTIONS ---")
for sel in soup.find_all("select"):
    print(f"Select Name: {sel.get('name')}, ID: {sel.get('id')}")
    for opt in sel.find_all("option"):
        print(f"  Option: value={opt.get('value')} text={opt.text.strip()}")

print("\n--- FORMS ---")
for f in soup.find_all("form"):
    print(f"Form Name: {f.get('name')}, Action: {f.get('action')}, Method: {f.get('method')}")
    for i in f.find_all("input"):
        print(f"  Input: name={i.get('name')} type={i.get('type')} value={i.get('value')}")

# Print any script tags that contain "act" or "act.php"
print("\n--- SCRIPTS with 'act.php' ---")
for s in soup.find_all("script"):
    if s.string and "act.php" in s.string:
        print(s.string.strip())

# Print all link tags that look like reservation actions
print("\n--- LINKS containing 'rev' or 'theme' ---")
for a in soup.find_all("a", href=True):
    href = a["href"]
    if "go=" in href or "rev" in href or "theme" in href:
        print(f"Anchor Text: {a.text.strip()}, Href: {href}")
