from curl_cffi import requests
import json
import os
import time
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
    {'name': 'Vuja De', 'url': 'https://vujade-studio.com/collections/all', 'type': 'shopify'},
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
            "color": 0,
            "footer": {"text": "Vuja De Monitor"}
        }]
    }
    
    try:
        response = requests.post(DISCORD_WEBHOOK_URL, json=data)
        if response.status_code == 204:
            print("  -> Discord Ping Sent Successfully")
        else:
            print(f"  -> FAILED to send ping. Discord Error Code: {response.status_code}")
            print(f"  -> Response: {response.text}")
    except Exception as e:
        print(f"  -> CRITICAL ERROR sending ping: {e}")

    # SLEEP to prevent rate-limiting (The "Brake")
    time.sleep(1)

def check_shopify(site, seen_db):
    json_url = site['url'].rstrip('/') + '/products.json'
    print(f"Checking Shopify: {site['name']}...")
    try:
        r = requests.get(json_url, headers=HEADERS, timeout=30, impersonate="chrome")
        products = r.json().get('products', [])
        
        print(f"  Found {len(products)} products on site.")
        
        for p in products:
            pid = str(p['id'])
            if pid not in seen_db.get(site['name'], []):
                title = p['title']
                handle = p['handle']
                parsed_uri = urlparse(site['url'])
                base_url = f"{parsed_uri.scheme}://{parsed_uri.netloc}"
                link = f"{base_url}/products/{handle}"
                
                print(f"Found new item: {title}")
                send_discord_ping(title, link, site['name'])
                
                if site['name'] not in seen_db: seen_db[site['name']] = []
                seen_db[site['name']].append(pid)
                
    except Exception as e:
        print(f"Error checking {site['name']}: {e}")

if __name__ == "__main__":
    db = load_database()
    for site in SITES:
        check_shopify(site, db)
    save_database(db)
