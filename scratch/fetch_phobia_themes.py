import requests
from bs4 import BeautifulSoup
import re
import json

branches = {
    "홍대던전": "3",
    "던전101": "1",
    "홍대던전Ⅲ": "5",
    "강남던전": "2",
    "강남던전Ⅱ": "4",
    "던전루나(강남)": "6",
    "던전스텔라(강남)": "9",
    "서면던전(부산)": "7",
    "서면던전 레드(부산)": "10"
}

headers = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
}

results = {}

for name, zid in branches.items():
    print(f"Fetching themes for {name} ({zid})...")
    url = f"https://xdungeon.net/layout/res/home.php?go=rev.main&s_zizum={zid}"
    try:
        response = requests.get(url, headers=headers, timeout=10)
        if response.status_code == 200:
            soup = BeautifulSoup(response.text, "html.parser")
            theme_links = soup.find_all("a", href=re.compile(r"javascript:_fun_theme_view"))
            results[zid] = {}
            for a in theme_links:
                theme_num = re.search(r"_fun_theme_view\('(\d+)'\)", a['href']).group(1)
                
                # Find theme name in parent elements
                # Usually there's a p.tit or text in nearby div
                parent_box = a.find_parent("div", class_="box")
                if parent_box:
                    tit_el = parent_box.find("p", class_="tit")
                    if tit_el:
                        theme_name = tit_el.text.strip()
                        results[zid][theme_name] = theme_name # Wait, for Phobia, the UI will pass the theme name as themePK, or do we want to map it to theme_num?
                        # Actually, let's keep both name and ID mapping.
                        # Wait, what value is submitted in the form?
                        # In xdungeon_rev_make.html:
                        # <input type=hidden name=theme_num value='15'>
                        # And:
                        # <a href="home.php?go=rev.make&crypt_data=...">
                        # Wait! The link is "home.php?go=rev.make&crypt_data=..."!
                        # It uses the crypt_data OR theme_num/rev_days/time_index in the query parameters!
                        # Wait, the theme_num is sent as part of the query parameter or POST data?
                        # On the first page (rev.main), the link is "home.php?go=rev.make&crypt_data=..."
                        # When we click it, we go to step 2 (rev.make), where the form has:
                        # <input type=hidden name=theme_num value='15'>
                        # So the initial navigation from step 1 to step 2 uses the crypt_data in the URL!
                        # Since crypt_data is dynamic and encrypted, we MUST extract the link matching the theme name and time from the timetable!
                        # In the UI, the user will select a branch, a theme name, and a time (like "사라진 보물 : 대저택의 비밀" and "16:40").
                        # So for the UI dropdown, we just need a list of theme names for each branch.
                        # We don't need a numeric themePK! We just need to matching the theme name.
                        # But wait, let's collect the theme names for each branch so we can populate the UI dropdown!
                        results[zid][theme_name] = theme_num
            print(f"Found themes: {list(results[zid].keys())}")
        else:
            print(f"Failed to fetch {name}: status code {response.status_code}")
    except Exception as e:
        print(f"Error fetching {name}: {e}")

# Save results
with open("scratch/phobia_themes_parsed.json", "w", encoding="utf-8") as f:
    json.dump(results, f, ensure_ascii=False, indent=2)
print("Finished!")
