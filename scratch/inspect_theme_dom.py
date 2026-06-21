from bs4 import BeautifulSoup
import re

with open("scratch/xdungeon_rev_main.html", "r", encoding="utf-8") as f:
    soup = BeautifulSoup(f.read(), "html.parser")

# Find all blocks of themes and their times
# Let's inspect elements around javascript:_fun_theme_view
theme_links = soup.find_all("a", href=re.compile(r"javascript:_fun_theme_view"))
print(f"Found {len(theme_links)} themes in the HTML:")
for idx, a in enumerate(theme_links):
    theme_num = re.search(r"_fun_theme_view\('(\d+)'\)", a['href']).group(1)
    
    # Try to find the theme name
    # Let's look at the parent structure
    parent = a.parent
    text_content = a.get_text(separator=' ').strip()
    print(f"\n[{idx}] Theme Number: {theme_num}")
    print("Link text:", text_content)
    
    # Let's print some sibling elements or nearby elements
    # Let's search upwards to find a container
    curr = a
    for depth in range(5):
        if not curr:
            break
        print(f"Depth {depth} tag: {curr.name}, class: {curr.get('class')}")
        # Print text snippet
        sibling_text = curr.get_text(" | ", strip=True)[:300]
        print("  Text:", sibling_text)
        curr = curr.parent
