import requests
import re

url = "https://xdungeon.net/layout/res/home.php?go=rev.main"
headers = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
}
response = requests.get(url, headers=headers, timeout=10)

# Let's search for the pattern containing javascript:_fun_theme_view
# and look at the bytes around it.
match = re.search(b'javascript:_fun_theme_view\\(\'\\d+\'\\)', response.content)
if match:
    start = max(0, match.start() - 200)
    end = min(len(response.content), match.end() + 200)
    chunk = response.content[start:end]
    print("Raw Chunk Bytes:")
    print(chunk)
    print("\nDecoded as utf-8 (replace):", chunk.decode('utf-8', errors='replace'))
    print("Decoded as cp949 (replace):", chunk.decode('cp949', errors='replace'))
    print("Decoded as euc-kr (replace):", chunk.decode('euc-kr', errors='replace'))
else:
    print("Pattern not found in raw content")
