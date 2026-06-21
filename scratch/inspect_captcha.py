with open("scratch/xdungeon_rev_make.html", "r", encoding="utf-8") as f:
    html = f.read()

# Find lines containing input_captcha and print surrounding lines
lines = html.splitlines()
for idx, line in enumerate(lines):
    if "input_captcha" in line:
        start = max(0, idx - 15)
        end = min(len(lines), idx + 15)
        print(f"\n--- CAPTCHA Section (Lines {start}-{end}) ---")
        for i in range(start, end):
            print(f"{i}: {lines[i]}")
        break
