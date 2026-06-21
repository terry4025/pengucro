import requests
from bs4 import BeautifulSoup

url = "https://xdungeon.net/layout/res/home.php?go=rev.main"
headers = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
}
response = requests.get(url, headers=headers, timeout=10)
print("Response Encoding:", response.encoding)
print("Apparent Encoding:", response.apparent_encoding)

# Decode with apparent encoding (EUC-KR) and print
text = response.content.decode("euc-kr", errors="ignore")
soup = BeautifulSoup(text, "html.parser")
theme_links = soup.find_all("a", href=lambda h: h and "javascript:_fun_theme_view" in h)
print(f"\nFound {len(theme_links)} themes with EUC-KR:")
for a in theme_links:
    # Let's find the text in the same box
    box = a.find_parent("div", class_="box")
    if box:
        # Find theme name from img_box
        img_box = box.find("div", class_="img_box")
        theme_name = img_box.text.strip() if img_box else "Unknown"
        theme_num = a['href'].split("'")[1]
        print(f"Theme Name: {theme_name}, ID/PK: {theme_num}")
