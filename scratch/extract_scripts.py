from bs4 import BeautifulSoup

with open("scratch/xdungeon_rev_main.html", "r", encoding="utf-8") as f:
    soup = BeautifulSoup(f.read(), "html.parser")

for i, script in enumerate(soup.find_all("script")):
    if script.string:
        print(f"\n--- Script {i} ---")
        print(script.string.strip()[:1000])
        print("...")
