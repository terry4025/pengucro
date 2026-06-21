from bs4 import BeautifulSoup

with open("scratch/xdungeon_rev_make.html", "r", encoding="utf-8") as f:
    soup = BeautifulSoup(f.read(), "html.parser")

spam_row = soup.find(class_="spam")
if spam_row:
    print(spam_row.prettify())
else:
    print("No spam row found")
