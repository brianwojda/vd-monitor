from curl_cffi import requests
import json
import os
import time
from bs4 import BeautifulSoup
from urllib.parse import urlparse, urljoin, unquote

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

CUSTOM_KEYWORDS = (
    'vuja',
    'vuja-de',
    'vuja de',
    'vuja_d',
    'vuja-d',
    'vuja%20de',
    'vuja%2dde',
    'vuja%2Dde',
    'vuja-d%C3%A9'.lower(),
)

SITES = [
    # --- SHOPIFY SITES (Auto-Detect via products.json) ---
    {'name': 'Vuja De Official', 'url': 'https://vujade-studio.com/collections/all', 'type': 'shopify'},
    {'name': 'Adelaide Addition', 'url': 'https://adelaide-addition.com/collections/vujade', 'type': 'shopify'},
    {'name': 'Why are you here?', 'url': 'https://whyareyouhere.jp/collections/vujade', 'type': 'shopify'},
    {'name': 'Refnet', 'url': 'https://www.refnet.tv/collections/vuja-de', 'type': 'shopify'},
    
    # --- CUSTOM SITES (Manual CSS Selectors) ---
    # Komune (Headless/WooCommerce) -> targeting the link elements directly
    {'name': 'Komune', 'url': 'https://komune.space/shop/vuja-d%C3%A9', 'type': 'custom', 'css_selector': 'a[href*="/shop/vuja-de"]'},
    
    # BEAMS -> targeting the list item container
    {'name': 'BEAMS (Japan)', 'url': 'https://www.beams.co.jp/brand/005416/', 'type': 'custom', 'css_selector': 'li.beams-list-image-item'},
    
    # Barneys -> targeting the list item container
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
        r = None
        for attempt in range(2):
            try:
                r = requests.get(
                    site['url'],
                    headers=HEADERS,
                    timeout=20,
                    impersonate="chrome",
                )
                break
            except Exception as fetch_err:
                if attempt == 1:
                    raise fetch_err
                print(f"  Retry fetch after error: {fetch_err}")
                time.sleep(1)

        r.encoding = 'utf-8'
        soup = BeautifulSoup(r.text, 'html.parser')

        # Primary extraction: site-specific selector
        items = soup.select(site['css_selector'])
        used_fallback = False

        # Secondary extraction: generic product-like links filtered by Vuja keywords
        if not items:
            used_fallback = True
            print(f"  WARNING: No items found for selector: {site['css_selector']}")
            all_links = soup.select('a[href]')
            filtered = []
            for a in all_links:
                href = a.get('href', '')
                text = a.get_text(' ', strip=True)
                haystack = f"{unquote(href).lower()} {text.lower()}"
                if any(k in haystack for k in CUSTOM_KEYWORDS):
                    filtered.append(a)
            items = filtered
            print(f"  Fallback matches: {len(items)}")

        print(f"  Parsed items: {len(items)}{' (fallback)' if used_fallback else ''}")

        for item in items:
            try:
                # 1. IDENTIFY LINK AND TITLE
                if item.name == 'a':
                    link_tag = item
                    name_text = item.get_text(strip=True)
                else:
                    link_tag = item.find('a')
                    name_div = item.select_one(
                        '.product-name, .product-title, .title, .name, '
                        '.woocommerce-loop-product__title, .item_name'
                    )
                    name_text = name_div.get_text(strip=True) if name_div else item.get_text(strip=True)

                if not link_tag:
                    continue

                # 2. GET HREF
                href = link_tag.get('href')
                if not href:
                    continue
                if href.startswith(('#', 'javascript:', 'mailto:')):
                    continue

                # 3. NORMALIZE URL
                href = urljoin(site['url'], href.strip())

                # 4. FALLBACK TITLE
                if len(name_text) <= 2:
                    name_text = link_tag.get('title', '').strip() or link_tag.get_text(strip=True) or href

                # 5. CHECK DATABASE
                unique_id = href
                if unique_id not in seen_db.get(site['name'], []):
                    if len(name_text) > 2:
                        print(f"Found new: {name_text[:30]}...")
                        send_discord_ping(name_text, href, site['name'])
                        if site['name'] not in seen_db:
                            seen_db[site['name']] = []
                        seen_db[site['name']].append(unique_id)

            except Exception:
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
