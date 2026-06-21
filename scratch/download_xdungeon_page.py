import requests

url = "https://xdungeon.net/layout/res/home.php?go=rev.main"
headers = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
}
try:
    response = requests.get(url, headers=headers, timeout=10)
    with open("scratch/xdungeon_rev_main.html", "w", encoding="utf-8") as f:
        f.write(response.text)
    print("Successfully saved to scratch/xdungeon_rev_main.html")
except Exception as e:
    print("Error:", e)
