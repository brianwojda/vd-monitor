from curl_cffi import requests
import json
import os
import time
from bs4 import BeautifulSoup
from urllib.parse import urlparse

# ==========================================
# CONFIGURATION
# ==========================================
DISCORD_WEBHOOK_URL = os.environ.get("DISCORD_WEBHOOK_URL")
DATABASE_FILE = "seen_products.json"

HEADERS = {
    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
    'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8',
    'Accept-Language': 'en-US,en;q=0.5',
}

SITES = [
    # SHOPIFY SITES (Auto-Detect)
    {'name': 'Vuja De Official', 'url': 'https://vujade-studio.com/collections/all', 'type': 'shopify'},
    {'name': 'Adelaide Addition', 'url': 'https://adelaide-addition.com/collections/vujade', 'type': 'shopify'},
    {'name': 'Why are you here?', 'url': 'https://whyareyouhere.jp/collections/vujade', 'type': 'shopify'},
    {'name': 'Refnet', 'url': 'https://www.refnet.tv/collections/vuja-de', 'type': 'shopify'},
    {'name': 'Komune', 'url': 'https://komune.space/shop/vuja-d%C3%A9', 'type': 'shopify'},
    
    # CUSTOM SITES (Manual Selectors)
    {'name': 'BEAMS (Japan)', 'url': 'https://www.beams.co.jp/brand/005416/', 'type': 'custom', 'css_selector': 'li.beams-list-image-item'},
    {'name': 'Barneys Japan', 'url': 'https://onlinestore.barneys.co.jp/items?bc=05918', 'type': 'custom', 'css_selector': '.item_list li, .product-list-item, .js-product-list-item'} 
]

# ==========================================
# FUNCTIONS
# ==========================================

def load_database():
    try:
        with open(DATABASE_FILE, 'r') as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {}

def save_database(data):
    with open(DATABASE_FILE, 'w') as f:
        json.dump(data, f, indent=4)

def send_discord_ping(product_name, product_link, site_name):
    if not DISCORD_WEBHOOK_URL:
        print("CRITICAL: No Webhook URL found! Check your GitHub Secrets.")
        return
    data = {
        "content": "@everyone",
        "embeds": [{
            "title": f"🚨 New Stock at {site_name}!",
            "description": f"**{product_name}**",
            "url": product_link,
            "color": 0, # Black for Vuja De
            "footer": {"text": "Vuja De Monitor"}
        }]
    }
    try:
        requests.post(DISCORD_WEBHOOK_URL, json=data)
        time.sleep(1) # Safety brake
    except Exception as e:
        print(f"Error sending ping: {e}")

def check_shopify(site, seen_db):
    # Handle standard and non-standard Shopify URLs
    json_url = site['url'].rstrip('/') + '/products.json'
    print(f"Checking Shopify: {site['name']}...")
    try:
        r = requests.get(json_url, headers=HEADERS, timeout=30, impersonate="chrome")
        products = r.json().get('products', [])
        for p in products:
            pid = str(p['id'])
            if pid not in seen_db.get(site['name'], []):
                title = p['title']
                handle = p['handle']
                parsed_uri = urlparse(site['url'])
                base_url = f"{parsed_uri.scheme}://{parsed_uri.netloc}"
                link = f"{base_url}/products/{handle}"
                
                print(f"Found new: {title}")
                send_discord_ping(title, link, site['name'])
                if site['name'] not in seen_db: seen_db[site['name']] = []
                seen_db[site['name']].append(pid)
    except Exception as e:
        print(f"Error checking {site['name']}: {e}")

def check_custom(site, seen_db):
    print(f"Checking Custom HTML: {site['name']}...")
    try:
        r = requests.get(site['url'], headers=HEADERS, timeout=60, impersonate="chrome")
        r.encoding = 'utf-8'
        soup = BeautifulSoup(r.text, 'html.parser')
        
        # Try to find items using the selector
        items = soup.select(site['css_selector'])
        
        if not items:
            print(f"  WARNING: No items found for selector: {site['css_selector']}")

        for item in items:
            try:
                # 1. GET THE TITLE
                name_div = item.select_one('.product-name, .product-title, .title, .name, .description, .item_name') 
                if name_div:
                    text = name_div.get_text(strip=True)
                else:
                    text = item.get_text(strip=True)

                # 2. GET THE LINK
                if item.name == 'a':
                    link_tag = item
                else:
                    link_tag = item.find('a')

                if link_tag:
                    href = link_tag.get('href')
                    if not href: continue

                    # 3. FIX RELATIVE URLS
                    if not href.startswith('http'):
                        if site['url'].endswith('/'):
                             base_domain = site['url'].rstrip('/')
                        else:
                             parsed_uri = urlparse(site['url'])
                             base_domain = '{uri.scheme}://{uri.netloc}'.format(uri=parsed_uri)
                        
                        if href.startswith('/'):
                            href = base_domain + href
                        else:
                             href = base_domain + '/' + href

                    unique_id = href 
                    if unique_id not in seen_db.get(site['name'], []):
                        # Filter out empty or junk titles
                        if len(text) > 2:
                            print(f"Found new: {text[:30]}...")
                            send_discord_ping(text, href, site['name'])
                            if site['name'] not in seen_db: seen_db[site['name']] = []
                            seen_db[site['name']].append(unique_id)
            except Exception as e: 
                continue
    except Exception as e:
        print(f"Error checking {site['name']}: {e}")

# ==========================================
# MAIN EXECUTION
# ==========================================
if __name__ == "__main__":
    db = load_database()
    for site in SITES:
        if site['type'] == 'shopify': check_shopify(site, db)
        elif site['type'] == 'custom': check_custom(site, db)
    save_database(db)
