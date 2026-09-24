from curl_cffi import requests
import json
import os
import re
import sys
import time
from datetime import datetime, timedelta, timezone
from bs4 import BeautifulSoup
from urllib.parse import urlparse, urljoin, unquote

# ==========================================
# CONFIGURATION
# ==========================================
DISCORD_WEBHOOK_URL = os.environ.get("DISCORD_WEBHOOK_URL")
DATABASE_FILE = "seen_products.json"
STATUS_FILE = "site_status.json"
# What was in stock at the last check, per site and product: Shopify variant ids, or '*'
# for a web-page product that isn't marked sold out. Compared each run to ping restocks.
STOCK_FILE = "stock_state.json"

HEADERS = {
    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
    'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8',
    'Accept-Language': 'en-US,en;q=0.5',
}

# Shopify rate-limits datacenter IPs (GitHub Actions runners) with 429s.
# Retrying does not help: it sends Retry-After >= 60 and still 429s after
# waiting, so these fail fast and are logged distinctly from a dead store.
RATE_LIMIT_STATUSES = (429, 503)

# A site that keeps failing this long gets a plain (no @everyone) Discord
# warning, and a follow-up once it works again.
FAILURE_ALERT_AFTER = timedelta(hours=2)

# A product missing from its listing this long is dropped from the stock record
FORGET_MISSING_AFTER = timedelta(days=60)

# Discord allows 10 embeds and 6000 embed characters per message.
MAX_EMBEDS_PER_MESSAGE = 10
MAX_EMBED_CHARS_PER_MESSAGE = 5500

# Shopify option names as shown in pings (stores also name them in Japanese/Korean)
OPTION_LABELS = {
    'color': 'Color', 'colour': 'Color', 'カラー': 'Color', '色': 'Color', '컬러': 'Color', '색상': 'Color',
    'size': 'Size', 'サイズ': 'Size', '사이즈': 'Size',
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

SOLD_OUT_MARKERS = (
    'sold out',
    'soldout',
    'out of stock',
    'out-of-stock',
    'no stock',
    'not available',
    '在庫なし',
    '完売',
    '欠品',
)

SITES = [
    # --- SHOPIFY SITES (Auto-Detect via products.json) ---
    {'name': 'Adelaide Addition', 'url': 'https://adelaide-addition.com/collections/vujade', 'type': 'shopify'},
    {'name': 'Why are you here?', 'url': 'https://whyareyouhere.jp/collections/vujade', 'type': 'shopify'},
    {'name': 'Refnet', 'url': 'https://www.refnet.tv/collections/vuja-de', 'type': 'shopify'},
    {'name': 'Addicted Seoul', 'url': 'https://addictedseoul.com/collections/vuja-de', 'type': 'shopify'},
    {'name': 'Mars', 'url': 'https://manhole-onlinestore.com/collections/vuja-de', 'type': 'shopify'},
    {'name': 'Plus81', 'url': 'https://www.plus81.id/en/collections/vuja-de', 'type': 'shopify'},
    {'name': 'Attic Sendai', 'url': 'https://attic-sendai.com/en/collections/vuja-de', 'type': 'shopify'},
    {'name': 'Chinatown Country Club', 'url': 'https://chinatowncountryclub.com/collections/vuja-de', 'type': 'shopify'},

    # --- CUSTOM SITES (Manual CSS Selectors) ---
    # Komune (Headless/WooCommerce) -> product hrefs keep the URL-encoded é (/shop/vuja-d%C3%A9/...)
    {'name': 'Komune', 'url': 'https://komune.space/shop/vuja-d%C3%A9', 'type': 'custom', 'css_selector': 'a[href*="/shop/vuja-d"]'},

    # BEAMS -> targeting the list item container
    {'name': 'BEAMS (Japan)', 'url': 'https://www.beams.co.jp/brand/005416/', 'type': 'custom', 'css_selector': 'li.beams-list-image-item'},

    # Barneys -> product cards in the item list grid
    {'name': 'Barneys Japan', 'url': 'https://onlinestore.barneys.co.jp/items?bc=05918', 'type': 'custom', 'css_selector': '.p-item-list__item'},

    # Loftman -> product cards in the category grid
    {'name': 'Loftman', 'url': 'https://loftman.co.jp/shop/c/cvujade/', 'type': 'custom', 'css_selector': 'dl.block-thumbnail-t--goods'}
]

# ==========================================
# FUNCTIONS
# ==========================================

def load_json(path):
    try:
        with open(path, 'r') as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {}

def save_json(path, data, sort_keys=False):
    with open(path, 'w') as f:
        json.dump(data, f, indent=4, sort_keys=sort_keys)

def post_to_discord(payload):
    """Send one webhook message, waiting out Discord's rate limits.

    Returns the final HTTP status, or 0 if Discord could not be reached.
    """
    status = 0
    for _ in range(5):
        try:
            r = requests.post(DISCORD_WEBHOOK_URL, json=payload, timeout=30)
        except Exception as e:
            print(f"  Discord unreachable: {e}")
            status = 0
            time.sleep(2)
            continue
        status = r.status_code
        if status == 429:
            try:
                wait = float(r.json()['retry_after'])
            except Exception:
                wait = 5.0
            print(f"  Discord rate limit, retrying in {wait:.1f}s")
            time.sleep(min(wait, 120))
            continue
        if status >= 500:
            print(f"  Discord error (HTTP {status}), retrying")
            time.sleep(2)
            continue
        if status >= 400:
            print(f"  Discord rejected message: HTTP {status} {r.text[:300]}")
        elif r.headers.get('X-RateLimit-Remaining') == '0':
            time.sleep(float(r.headers.get('X-RateLimit-Reset-After') or 1))
        return status
    return status

def delivered(status):
    return 200 <= status < 300

def format_price(prices, currency):
    amounts = [f"{p:,.0f}" if p.is_integer() else f"{p:,.2f}" for p in (prices[0], prices[-1])]
    text = amounts[0] if prices[0] == prices[-1] else f"{amounts[0]} – {amounts[1]}"
    return f"{text} {currency}" if currency else text

def product_embed(item):
    embed = {
        "title": item['name'][:256],
        "url": item['link'],
        "color": 0, # Black for Vuja De
        "footer": {"text": "Vuja De Monitor"},
    }
    fields = []
    if item.get('back'):
        # A restock: which sizes came back, ahead of the usual details
        fields.append({"name": "Back in stock", "value": ", ".join(item['back'])[:1024], "inline": False})
    elif 'back' in item:
        embed['description'] = "Back in stock"
    product = item.get('product')
    if product:
        # Shopify listing: add price, options (sold-out values struck through) and photo
        variants = product.get('variants') or []
        prices = sorted({float(v['price']) for v in variants if v.get('price')})
        if prices:
            fields.append({"name": "Price", "value": format_price(prices, item.get('currency')), "inline": True})
        for i, option in enumerate(product.get('options') or []):
            values = [str(v) for v in option.get('values') or [] if str(v).strip()]
            if not values or values == ['Default Title']:
                continue
            key = f"option{option.get('position') or i + 1}"
            in_stock = {v.get(key) for v in variants if v.get('available')}
            shown = ", ".join(v if v in in_stock else f"~~{v}~~" for v in values)
            name = option.get('name') or 'Option'
            fields.append({"name": OPTION_LABELS.get(name.strip().lower(), name)[:256], "value": shown[:1024], "inline": True})
        if not any(v.get('available') for v in variants):
            embed['description'] = "Sold out right now"
        images = product.get('images') or []
        if images and images[0].get('src'):
            embed['thumbnail'] = {"url": images[0]['src']}
    if fields:
        embed['fields'] = fields[:25]
    return embed

def embed_chars(embed):
    return (len(embed['title']) + len(embed.get('description', '')) + len(embed['footer']['text'])
            + sum(len(f['name']) + len(f['value']) for f in embed.get('fields', [])))

def discord_batches(items):
    """Group items into messages that fit Discord's embed limits."""
    batches = []
    for item in items:
        embed = product_embed(item)
        size = embed_chars(embed)
        if (not batches or len(batches[-1]['items']) == MAX_EMBEDS_PER_MESSAGE
                or batches[-1]['chars'] + size > MAX_EMBED_CHARS_PER_MESSAGE):
            batches.append({'items': [], 'embeds': [], 'chars': 0})
        batches[-1]['items'].append(item)
        batches[-1]['embeds'].append(embed)
        batches[-1]['chars'] += size
    return batches

def mark_seen(seen_db, site, items):
    seen_db.setdefault(site['name'], []).extend(item['id'] for item in items)

def announce(items, heading, on_sent):
    """Ping Discord about items under one heading, calling on_sent(items) for each batch
    Discord accepts, so nothing is recorded as announced before it really is.

    Returns False if anything could not be sent; those items are retried next run.
    """
    if not DISCORD_WEBHOOK_URL:
        print("  Dry run: not sending")
        return True
    all_sent = True
    for batch in discord_batches(items):
        status = post_to_discord({"content": heading, "embeds": batch['embeds']})
        if delivered(status):
            print(f"  Sent {len(batch['items'])} to Discord")
            on_sent(batch['items'])
            continue
        if status != 400:
            all_sent = False  # outage, rate limit or broken webhook: retry next run
            continue
        # Discord refused the embeds themselves, so fall back to one plain message per item
        for item in batch['items']:
            back = f"\nBack in stock: {', '.join(item['back'])}" if item.get('back') else ''
            fallback = post_to_discord({"content": f"{heading}\n**{item['name']}**{back}\n{item['link']}"[:2000]})
            if delivered(fallback) or fallback == 400:
                on_sent([item])  # refused twice: give up rather than retry forever
            all_sent = all_sent and delivered(fallback)
    return all_sent

def pingable(site, item):
    """Web pages only count a product once it is in stock; Shopify lists sold-out ones too."""
    return site['type'] == 'shopify' or bool(item['stock'])

def record_stock(stock_db, site, items):
    for item in items:
        stock_db[site['name']][item['id']] = {'in_stock': sorted(item['stock'])}

def find_restocks(site, items, stock_db, new_ids, now):
    """Compare what is in stock now with the last check. Returns the items with a size (or,
    on a web page, the whole product) back in stock, each with 'back' naming the sizes.

    Everything else is recorded straight away; a restocked item is only recorded once its
    ping is delivered (record_stock), so a failed ping is retried next run. New products are
    pinged as new stock instead, and a site's first check just records the starting point.
    """
    if site['name'] not in stock_db:
        stock_db[site['name']] = {}
        record_stock(stock_db, site, items)
        return []
    state = stock_db[site['name']]
    restocked = []
    for item in items:
        entry = state.get(item['id'])
        if entry is None or item['id'] in new_ids:
            record_stock(stock_db, site, [item])
            continue
        back = set(item['stock']) - set(entry['in_stock'])
        if back:
            # Sizes in the store's own order; a lone 'Default Title' variant is the whole product
            labels = [label for key, label in item['stock'].items() if key in back and label and label != 'Default Title']
            restocked.append(dict(item, back=labels))
        else:
            record_stock(stock_db, site, [item])

    listed = {item['id'] for item in items}
    for key, entry in list(state.items()):
        if key in listed:
            continue
        if 'missing_since' not in entry:
            # A Shopify feed drops a product that is hidden or unpublished (some stores hide
            # sold-out ones), so count it as sold out and ping if it comes back. An empty feed
            # is more likely a hidden collection, and web pages can miss cards, so those keep
            # the last known stock.
            if site['type'] == 'shopify' and items:
                entry['in_stock'] = []
            entry['missing_since'] = now.isoformat(timespec='seconds')
        elif now - datetime.fromisoformat(entry['missing_since']) > FORGET_MISSING_AFTER:
            del state[key]
    return restocked

def post_notice(title, lines, color):
    """Post a status message without pinging anyone. Returns True once Discord accepts it."""
    embed = {"title": title, "description": "\n".join(lines)[:4096], "color": color, "footer": {"text": "Vuja De Monitor"}}
    return delivered(post_to_discord({"embeds": [embed], "allowed_mentions": {"parse": []}}))

def update_site_health(status_db, failures, now):
    """Warn once a site has been failing for FAILURE_ALERT_AFTER, and again when it recovers.

    failures maps each site that failed this run to the reason. Returns False
    if a notice could not be sent (it is retried next run).
    """
    site_names = [site['name'] for site in SITES]
    for name in list(status_db):
        if name not in site_names:
            del status_db[name]  # site was removed from SITES

    to_warn, recovered = [], []
    for name in site_names:
        entry = status_db.get(name)
        if name in failures:
            if entry is None:
                entry = status_db[name] = {'failing_since': now.isoformat(timespec='seconds'), 'alerted': False}
            if not entry['alerted'] and now - datetime.fromisoformat(entry['failing_since']) >= FAILURE_ALERT_AFTER:
                to_warn.append(name)
        elif entry is not None:
            if entry['alerted']:
                recovered.append(name)
            else:
                del status_db[name]

    since = lambda name: f"{datetime.fromisoformat(status_db[name]['failing_since']):%b %d %H:%M} UTC"
    warn_lines = [f"**{name}**: {failures[name][:200]} (failing since {since(name)})" for name in to_warn]
    recovered_lines = [f"**{name}** (was failing since {since(name)})" for name in recovered]
    if not DISCORD_WEBHOOK_URL:
        for line in warn_lines + recovered_lines:
            print(f"  Dry run, would post: {line}")
        return True

    all_sent = True
    if to_warn:
        if post_notice("⚠️ Monitor can't check these sites", warn_lines, 0xE67E22):
            for name in to_warn:
                status_db[name]['alerted'] = True
        else:
            all_sent = False
    if recovered:
        if post_notice("✅ Working again", recovered_lines, 0x2ECC71):
            for name in recovered:
                del status_db[name]
        else:
            all_sent = False
    return all_sent

def normalize_text(value):
    return re.sub(r'\s+', ' ', (value or '').replace('\xa0', ' ')).strip()

def title_from_href(href):
    path = urlparse(href).path.strip('/')
    slug = unquote(path.split('/')[-1] if path else '')
    slug = normalize_text(re.sub(r'[-_]+', ' ', slug))
    if not slug or slug.isdigit():
        return href
    words = slug.split(' ')
    pretty_words = []
    for word in words:
        if len(word) <= 3 and word.isalpha():
            pretty_words.append(word.upper())
        else:
            pretty_words.append(word.capitalize())
    return ' '.join(pretty_words)

def clean_product_name(name_text, href):
    cleaned = normalize_text(unquote(name_text or ''))
    cleaned = re.sub(r'(?i)^sold\s*out[:\-\s]*', '', cleaned)
    cleaned = re.sub(
        r'(?i)\b(sold\s*out|soldout|out\s*of\s*stock)\b[:\-\s]*',
        '',
        cleaned,
    )
    cleaned = re.sub(r'[\$¥€£]\s?\d[\d,]*(?:\.\d{1,2})?$', '', cleaned).strip()
    cleaned = normalize_text(cleaned)
    cleaned = cleaned.replace('Vuja Dé', 'Vuja Dé ').replace('Vuja De', 'Vuja De ')
    cleaned = re.sub(r'(?i)(vuja\s*d[eé])([A-Z])', r'\1 \2', cleaned)
    cleaned = normalize_text(cleaned)
    if len(cleaned) <= 2:
        cleaned = title_from_href(href)
    return cleaned

def is_sold_out_item(item, link_tag, raw_name_text):
    status_fields = [raw_name_text]
    for tag in (item, link_tag):
        if tag and hasattr(tag, 'get'):
            status_fields.append(' '.join(tag.get('class', [])))
            status_fields.append(tag.get('aria-label', '') or '')
            status_fields.append(tag.get('data-stock-status', '') or '')
            status_fields.append(tag.get('title', '') or '')
    if item is not None and hasattr(item, 'select'):
        for badge in item.select('.badge, [class*="sold"], [class*="stock"], [class*="label"]'):
            status_fields.append(' '.join(badge.get('class', [])))
            status_fields.append(badge.get_text(' ', strip=True))
    status_blob = normalize_text(' '.join(status_fields)).lower()
    return any(marker in status_blob for marker in SOLD_OUT_MARKERS)

def fetch_shopify(site):
    """Return every product in the collection. Raises if the store can't be read."""
    # limit=250 is the most Shopify returns per page (the default is only 30)
    json_url = site['url'].rstrip('/') + '/products.json?limit=250'
    print(f"Checking Shopify: {site['name']}...")
    r = requests.get(json_url, headers=HEADERS, timeout=30, impersonate="chrome")
    if r.status_code in RATE_LIMIT_STATUSES:
        raise RuntimeError(f"rate limited by store (HTTP {r.status_code})")
    if r.status_code != 200 or '/password' in str(r.url):
        raise RuntimeError(f"store locked or unavailable (HTTP {r.status_code})")
    # Prices come back in the visitor's local currency, which Shopify names in this cookie
    try:
        currency = r.cookies.get('cart_currency')
    except Exception:
        currency = None
    parsed_uri = urlparse(site['url'])
    base_url = f"{parsed_uri.scheme}://{parsed_uri.netloc}"
    products = []
    for p in r.json().get('products', []):
        products.append({
            'id': str(p['id']),
            'name': p['title'],
            'link': f"{base_url}/products/{p['handle']}",
            'product': p,
            'currency': currency,
            # In-stock variants (id -> title, e.g. "46 / Black"), in the store's order
            'stock': {str(v['id']): v.get('title') or '' for v in p.get('variants') or [] if v.get('available')},
        })
    return products

def fetch_custom(site):
    """Return the products on the page, sold-out ones with empty 'stock'. Raises if the page
    can't be read."""
    print(f"Checking Custom HTML: {site['name']}...")
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
    if r.status_code != 200:
        raise RuntimeError(f"page unavailable (HTTP {r.status_code})")

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

    products = []
    processed_hrefs = set()
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
                    '.woocommerce-loop-product__title, .item_name, '
                    '.c-item-card__name, .block-thumbnail-t--goods-name'
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
            if href.rstrip('/') == site['url'].rstrip('/'):
                continue  # link back to the collection page itself, not a product
            if href in processed_hrefs:
                continue
            processed_hrefs.add(href)

            # 4. FALLBACK TITLE
            if len(name_text) <= 2:
                name_text = link_tag.get('title', '').strip() or link_tag.get_text(strip=True) or href

            # 5. CLEAN + FILTER
            raw_name_text = name_text
            name_text = clean_product_name(name_text, href)
            sold_out = is_sold_out_item(item, link_tag, raw_name_text)

            # 6. COLLECT (the href is the product's ID in the database). Sold-out cards are kept
            # so a later restock can be noticed; '*' stands for "the product is in stock".
            if len(name_text) > 2:
                products.append({'id': href, 'name': name_text, 'link': href, 'stock': {} if sold_out else {'*': ''}})

        except Exception:
            continue
    return products

# ==========================================
# MAIN EXECUTION
# ==========================================
if __name__ == "__main__":
    if not DISCORD_WEBHOOK_URL:
        print("No DISCORD_WEBHOOK_URL set: dry run, nothing is sent and new products are not marked as seen.")
    seen_db = load_json(DATABASE_FILE)
    status_db = load_json(STATUS_FILE)
    stock_db = load_json(STOCK_FILE)
    now = datetime.now(timezone.utc)
    failures = {}
    all_sent = True
    for site in SITES:
        try:
            if site['type'] == 'shopify':
                items = fetch_shopify(site)
            else:
                items = fetch_custom(site)
        except Exception as e:
            reason = str(e).split(' See https://curl.se')[0]  # drop curl's generic help link
            print(f"  Skipping {site['name']}: {reason}")
            failures[site['name']] = reason
            continue

        if site['name'] not in seen_db:
            # First check of a newly added retailer: record what it already lists instead of pinging it all
            seen_db[site['name']] = [item['id'] for item in items if pingable(site, item)]
            stock_db.pop(site['name'], None)
            find_restocks(site, items, stock_db, set(), now)  # starting point for restocks
            print(f"  First check: recorded {len(seen_db[site['name']])} current products without pinging")
            continue

        seen = set(seen_db[site['name']])
        new_items = [item for item in items if item['id'] not in seen and pingable(site, item)]
        for item in new_items:
            print(f"Found new: {item['name']}")
        if new_items and not announce(new_items, f"@everyone 🚨 New Stock at {site['name']}!",
                                      lambda sent: mark_seen(seen_db, site, sent)):
            all_sent = False

        restocked = find_restocks(site, items, stock_db, {item['id'] for item in new_items}, now)
        for item in restocked:
            print(f"Restocked: {item['name']} ({', '.join(item['back']) or 'back in stock'})")
        if restocked and not announce(restocked, f"@everyone 🔄 Restock at {site['name']}!",
                                      lambda sent: record_stock(stock_db, site, sent)):
            all_sent = False

    for name in list(stock_db):
        if name not in {site['name'] for site in SITES}:
            del stock_db[name]  # site was removed from SITES
    if not update_site_health(status_db, failures, now):
        all_sent = False
    save_json(DATABASE_FILE, seen_db)
    save_json(STATUS_FILE, status_db)
    save_json(STOCK_FILE, stock_db, sort_keys=True)
    if not all_sent:
        print("Some Discord messages could not be sent; they will be retried next run.")
        sys.exit(1)
