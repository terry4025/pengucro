import requests
from bs4 import BeautifulSoup
import re
import urllib.parse
from datetime import datetime, timedelta

def parse_booking_site(url, site_name=""):
    """
    Fetches and automatically parses the branch and theme configuration of a booking site.
    Detects if the site is Jigubyeol-style or Zeroworld-style, and queries the theme list accordingly.
    
    :param url: The reservation page URL (e.g., https://zerogangnam.com/reservation)
    :param site_name: Optional name for the site
    :return: A dictionary containing site configuration, or raises an Exception.
    """
    session = requests.Session()
    session.headers.update({
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    })
    
    try:
        response = session.get(url, timeout=10)
        if response.status_code != 200:
            raise Exception(f"페이지를 불러오는데 실패했습니다 (HTTP Status: {response.status_code})")
    except Exception as e:
        raise Exception(f"사이트 연결 오류: {e}")
        
    soup = BeautifulSoup(response.text, "html.parser")
    html_text = response.text
    
    # Smart URL Redirection for main pages or hash links
    parsed_url = urllib.parse.urlparse(url)
    if parsed_url.path in ["", "/", "/index.php", "/index.html"] or parsed_url.fragment == "nav" or parsed_url.fragment == "list":
        # Search for reservation links
        res_link = soup.find("a", href=re.compile(r"/reservation$|/reservation/create|/reservation\.php|/booking", re.I))
        if res_link:
            target_url = urllib.parse.urljoin(url, res_link.get("href"))
            try:
                # Redirect and scrape the actual booking page
                response = session.get(target_url, timeout=10)
                if response.status_code == 200:
                    url = target_url
                    soup = BeautifulSoup(response.text, "html.parser")
                    html_text = response.text
            except Exception:
                pass
    
    # 1. Determine site engine template style
    style = "zeroworld"  # default
    if "reservation/create" in html_text or "branch=" in html_text or "theme=" in html_text:
        style = "jigubyeol"
    elif "run_proc.php" in html_text:
        style = "zeroworld" # Zeroworld style uses run_proc.php or direct reservation POST
        
    # Get base URL of the site
    parsed_url = urllib.parse.urlparse(url)
    base_url = f"{parsed_url.scheme}://{parsed_url.netloc}"
    
    # Get CSRF token
    csrf_token = ""
    csrf_meta = soup.find('meta', {'name': 'csrf-token'})
    if not csrf_meta:
        csrf_meta = soup.find('meta', {'id': 'csrf'})
    if csrf_meta:
        csrf_token = csrf_meta.get('content', '')
        
    branches = {}
    themes = {}
    
    # 2. Extract branches
    # Search for branch selects (name=branch, name=zizum, id=zizum, etc.)
    branch_select = soup.find("select", attrs={"name": re.compile("branch|zizum|zizumNum", re.I)})
    if not branch_select:
        branch_select = soup.find("select", attrs={"id": re.compile("branch|zizum|zizumNum", re.I)})
    if not branch_select:
        # Fallback: check any select containing options ending in "점" or "본점"
        for s in soup.find_all("select"):
            options = s.find_all("option")
            if any("점" in o.text for o in options if o.text):
                branch_select = s
                break
                
    if branch_select:
        for option in branch_select.find_all("option"):
            val = option.get("value", "").strip()
            name = option.text.strip()
            # Skip placeholders
            if val and not any(ph in name for ph in ["선택", "바로가기", "지점", "테스트"]):
                branches[name] = val
    else:
        # Single branch default
        branches["기본 지점"] = "1"
        
    # 3. Extract themes based on style
    tomorrow = (datetime.now() + timedelta(days=1)).strftime("%Y-%m-%d")
    
    if style == "jigubyeol":
        # Jigubyeol style: try parsing from option dropdown or /theme page
        # A. Try parsing from theme select options first
        theme_select = soup.find("select", attrs={"name": re.compile("theme|themePK", re.I)})
        if theme_select:
            # We map themes to the first branch by default
            first_branch_id = list(branches.values())[0] if branches else "1"
            themes[first_branch_id] = {}
            for option in theme_select.find_all("option"):
                val = option.get("value", "").strip()
                name = option.text.strip()
                if val and not any(ph in name for ph in ["선택", "테마"]):
                    themes[first_branch_id][name] = val
                    
        # B. If themes are empty or we want to get themes for all branches, fetch /theme page
        theme_url = f"{base_url}/theme"
        try:
            r_theme = session.get(theme_url, timeout=5)
            if r_theme.status_code == 200:
                theme_soup = BeautifulSoup(r_theme.text, "html.parser")
                # Expand match pattern to themes-item / themes_item (exact class matching)
                theme_items = theme_soup.find_all(class_=re.compile(r"^(theme-item|theme_item|themes-item|themes_item)$", re.I))
                if not theme_items:
                    theme_items = theme_soup.find_all("section", class_=re.compile("theme", re.I))
                    
                themes_found = False
                for item in theme_items:
                    a_tags = item.find_all("a")
                    for a in a_tags:
                        href = a.get("href", "")
                        if "branch=" in href and "theme=" in href:
                            parsed_href = urllib.parse.urlparse(href)
                            query = urllib.parse.parse_qs(parsed_href.query)
                            b_id = query.get("branch", [""])[0]
                            t_pk = query.get("theme", [""])[0]
                            
                            # Get theme name
                            name_el = item.find(class_=re.compile("title|name|subject", re.I))
                            if not name_el:
                                name_el = item.find(["h2", "h3", "h4", "h5", "strong"])
                            if name_el and b_id and t_pk:
                                name = name_el.text.strip()
                                name = re.sub(r'\s+', ' ', name)
                                name = re.sub(r'^#[^\s]+\s*', '', name).strip()
                                if b_id not in themes:
                                    themes[b_id] = {}
                                themes[b_id][name] = t_pk
                                themes_found = True

                # Fallback: scan ALL a tags in the page if basic parsing didn't find themes
                # (essential for play33.kr variant DOM layouts)
                if not themes_found:
                    for a in theme_soup.find_all("a"):
                        href = a.get("href", "")
                        if "branch=" in href and "theme=" in href:
                            parsed_href = urllib.parse.urlparse(href)
                            query = urllib.parse.parse_qs(parsed_href.query)
                            b_id = query.get("branch", [""])[0]
                            t_pk = query.get("theme", [""])[0]
                            
                            # Find theme name from parent/ancestor tags
                            name = ""
                            curr = a.parent
                            for _ in range(5):
                                if not curr:
                                    break
                                name_el = curr.find(class_=re.compile("title|name|subject", re.I))
                                if not name_el:
                                    name_el = curr.find(["h2", "h3", "h4", "h5", "strong"])
                                if name_el:
                                    name = name_el.text.strip()
                                    break
                                curr = curr.parent
                                
                            if name and b_id and t_pk:
                                name = re.sub(r'\s+', ' ', name)
                                name = re.sub(r'^#[^\s]+\s*', '', name).strip()
                                if b_id not in themes:
                                    themes[b_id] = {}
                                themes[b_id][name] = t_pk
        except Exception:
            pass
            
    else:  # zeroworld style
        # Zeroworld/Keyescape style: try fetching themes via POST AJAX API
        # A. Try Zeroworld style AJAX: POST /reservation/theme
        for b_name, b_id in branches.items():
            themes[b_id] = {}
            try:
                # We need CSRF for Laravel POST
                headers = {
                    "X-CSRF-TOKEN": csrf_token,
                    "Accept": "application/json, text/javascript, */*; q=0.01",
                    "X-Requested-With": "XMLHttpRequest",
                    "Referer": url
                }
                theme_api_url = f"{base_url}/reservation/theme"
                r_theme = session.post(theme_api_url, data={"date": tomorrow}, headers=headers, timeout=2)
                if r_theme.status_code == 200:
                    res_json = r_theme.json()
                    # Res matches: {"data": [{"PK": 23, "title": "링", ...}]} or array directly
                    theme_list = res_json.get("data", []) if isinstance(res_json, dict) else res_json
                    if isinstance(theme_list, list):
                        for t in theme_list:
                            pk = str(t.get("PK", t.get("id", "")))
                            title = t.get("title", t.get("name", ""))
                            # Remove branch prefix e.g. "[강남] 링" -> "링"
                            title = re.sub(r'^\[[^\]]+\]\s*', '', title).strip()
                            if pk and title:
                                themes[b_id][title] = pk
            except Exception:
                pass
                
        # B. Try Keyescape/Zeroworld variant AJAX: POST /controller/run_proc.php with t=get_theme_info_list
        # Check if we didn't find any themes from method A
        has_themes = any(themes[b_id] for b_id in themes)
        if not has_themes:
            for b_name, b_id in branches.items():
                themes[b_id] = {}
                try:
                    proc_url = f"{base_url}/controller/run_proc.php"
                    r_proc = session.post(proc_url, data={
                        "t": "get_theme_info_list",
                        "zizum_num": b_id
                    }, timeout=2)
                    if r_proc.status_code == 200:
                        res_json = r_proc.json()
                        if res_json.get("status") and "data" in res_json:
                            for item in res_json["data"]:
                                # info_num is used as the themeInfoNum/themePK, level as difficulty, level_num as theme_num
                                name = item.get("info_name", "")
                                pk = str(item.get("info_num", ""))
                                if name and pk:
                                    themes[b_id][name] = pk
                except Exception:
                    pass
                    
    # Clean up empty branches/themes mapping
    themes = {b_id: t_dict for b_id, t_dict in themes.items() if t_dict and b_id in branches.values()}
    
    # 4. Final check: if no themes were found, throw error
    if not themes:
        raise Exception("지점이나 테마 정보를 파싱하지 못했습니다. 지원되지 않는 사이트 템플릿일 수 있습니다.")
        
    return {
        "name": site_name or parsed_url.netloc,
        "url": url,
        "base_url": base_url,
        "style": style,
        "branches": branches,
        "themes": themes
    }
