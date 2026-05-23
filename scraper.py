import asyncio, json, os, re, smtplib
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from datetime import datetime
from playwright.async_api import async_playwright

# ── credentials ───────────────────────────────────────────────────────────
FB_COOKIES   = os.environ["FB_COOKIES"]         # JSON from Cookie-Editor
GMAIL_EMAIL  = os.environ["GMAIL_EMAIL"]
GMAIL_APP_PW = os.environ["GMAIL_APP_PASSWORD"]
NOTIFY_EMAIL = os.environ.get("NOTIFY_EMAIL", GMAIL_EMAIL)
TARGET_ID    = os.environ.get("PRODUCT_ID", "").strip()

# ── location: center of Markham/RH/Vaughan/Aurora/Newmarket ──────────────
CENTER_LAT = 43.9225
CENTER_LNG = -79.4374
RADIUS_KM  = 40
CITY_NAMES = ["Markham", "Richmond Hill", "Vaughan", "Aurora",
              "Newmarket", "Thornhill", "Unionville", "Stouffville", "ON"]

RESULTS_DIR = "results"

# ── condition keywords ────────────────────────────────────────────────────
NEW_KW      = ["brand new", "new in box", "sealed", "unopened", "never opened"]
LIKE_NEW_KW = ["like new", "likenew", "like-new", "excellent",
               "barely used", "mint condition", "never used"]
GOOD_KW     = ["good condition", "good shape", "great condition",
               "works great", "clean", "no damage"]
FAIR_KW     = ["fair condition", "fair shape", "some wear",
               "some scratches", "shows wear", "needs cleaning"]

# ── relevance filter ──────────────────────────────────────────────────────
def is_relevant(title, query):
    t = title.lower()
    q = query.lower()
    # exact phrase match
    if q in t:
        return True
    # any single meaningful word from the query appears in the title
    words = [w for w in q.split() if len(w) > 3]
    if not words:
        return True
    return any(w in t for w in words)

def classify_condition(text):
    t = text.lower()
    if any(k in t for k in NEW_KW):      return "new",      "New"
    if any(k in t for k in LIKE_NEW_KW): return "like_new", "Like new"
    if any(k in t for k in GOOD_KW):     return "good",     "Good"
    if any(k in t for k in FAIR_KW):     return "fair",     "Fair"
    return "unknown", "Unknown"

# ── load products ─────────────────────────────────────────────────────────
def load_products():
    with open("products.json") as f:
        data = json.load(f)
    products = data.get("products", [])
    if TARGET_ID:
        products = [p for p in products if p["id"] == TARGET_ID]
        if not products:
            raise RuntimeError(f"Product ID '{TARGET_ID}' not found in products.json")
    return products

# ── 1. new price from Walmart.ca ──────────────────────────────────────────
async def get_new_price(page, query, fallback_price):
    try:
        url = "https://www.walmart.ca/search?q=" + query.replace(" ", "+")
        await page.goto(url, wait_until="domcontentloaded", timeout=30000)
        await page.wait_for_timeout(3000)
        for sel in ['[data-automation="product-price"]',
                    ".price-characteristic", '[class*="price"]']:
            el = await page.query_selector(sel)
            if el:
                txt = await el.inner_text()
                m = re.search(r"[\d]+\.?\d*", txt.replace(",", ""))
                if m:
                    p = float(m.group())
                    if 5 < p < 2000:
                        print(f"   Walmart price: ${p:.2f} CAD")
                        return p
    except Exception as e:
        print(f"   Walmart scrape failed ({e})")
    print(f"   Using manual price: ${fallback_price:.2f} CAD")
    return float(fallback_price)

# ── 2. Facebook login via cookies ─────────────────────────────────────────
async def fb_login(ctx, page):
    raw = json.loads(FB_COOKIES)

    cookies = []
    for c in raw:
        ck = {
            "name":   c["name"],
            "value":  c["value"],
            "domain": c.get("domain", ".facebook.com"),
            "path":   c.get("path", "/"),
        }
        if "expirationDate" in c:
            ck["expires"] = float(c["expirationDate"])
        if "secure"   in c: ck["secure"]   = c["secure"]
        if "httpOnly" in c: ck["httpOnly"] = c["httpOnly"]
        if "sameSite" in c:
            ss = str(c["sameSite"]).capitalize()
            if ss in ("Strict", "Lax", "None"):
                ck["sameSite"] = ss
        cookies.append(ck)

    await ctx.add_cookies(cookies)

    await page.goto("https://www.facebook.com/", wait_until="domcontentloaded", timeout=30000)
    await page.wait_for_timeout(2000)

    if "login" in page.url:
        raise RuntimeError(
            "Cookies are invalid or expired — re-export from Cookie-Editor and update FB_COOKIES secret"
        )
    print("   Facebook login OK (cookie auth)")

# ── 3. scrape one product ─────────────────────────────────────────────────
async def scrape_product(page, product, new_price):
    query     = product["query"]
    threshold = float(product.get("threshold", 0.30))
    target_cond = product.get("condition", "like_new")
    cond_rank = {"new": 4, "like_new": 3, "good": 2, "fair": 1, "unknown": 0}
    target_rank = cond_rank.get(target_cond, 3)

    urls = [
        "https://www.facebook.com/marketplace/markham/search/?query=" + query.replace(" ", "+"),
        (f"https://www.facebook.com/marketplace/search/?query={query.replace(' ', '+')}"
         f"&latitude={CENTER_LAT}&longitude={CENTER_LNG}&radius={RADIUS_KM}&exact=false"),
    ]

    anchors = []
    for url in urls:
        await page.goto(url, wait_until="domcontentloaded", timeout=30000)
        await page.wait_for_timeout(4000)
        anchors = await page.query_selector_all('a[href*="/marketplace/item/"]')
        if anchors:
            break
        print(f"   No listings at {url}, trying next...")

    for _ in range(4):
        await page.keyboard.press("End")
        await page.wait_for_timeout(1200)

    anchors = await page.query_selector_all('a[href*="/marketplace/item/"]')
    print(f"   Found {len(anchors)} raw anchor(s)")
    print(f"   Page URL: {page.url}")
    print(f"   Page title: {await page.title()}")
    all_links = await page.query_selector_all('a[href]')
    sample = [await a.get_attribute('href') for a in all_links[:8]]
    print(f"   Sample hrefs: {sample}")

    seen, listings = set(), []

    for a in anchors[:30]:
        try:
            href = await a.get_attribute("href") or ""
            if not href or href in seen:
                continue
            seen.add(href)
            full_url = href if href.startswith("http") else "https://www.facebook.com" + href

            texts = []
            for sel in ['span[dir="auto"]', "span", 'div[dir="auto"]']:
                els = await a.query_selector_all(sel)
                for el in els:
                    t = (await el.inner_text()).strip()
                    if t and t not in texts:
                        texts.append(t)
                if texts:
                    break

            price = None
            for t in texts:
                m = re.search(r"\$\s*([\d,]+)", t)
                if m:
                    price = float(m.group(1).replace(",", ""))
                    break
            if price is None:
                continue

            candidates = [t for t in texts if "$" not in t and len(t) > 6]
            title = max(candidates, key=len) if candidates else query.title()

            location = next(
                (t for t in texts if any(c in t for c in CITY_NAMES)), "Nearby"
            )

            # skip listings that don't match the query keywords
            if not is_relevant(title, query):
                continue

            cond, cond_label = classify_condition(title)
            discount = (new_price - price) / new_price
            is_deal  = (cond_rank.get(cond, 0) >= target_rank and discount >= threshold)

            listings.append({
                "title":      title[:80],
                "price":      price,
                "location":   location,
                "condition":  cond,
                "condLabel":  cond_label,
                "discount":   round(discount * 100, 1),
                "link":       full_url,
                "is_deal":    is_deal,
                "scraped_at": datetime.now().isoformat(),
            })
        except Exception as e:
            print(f"   Card parse error: {e}")

    return sorted(listings, key=lambda x: x["price"])

# ── 4. save results ───────────────────────────────────────────────────────
def save_results(product, listings, new_price):
    os.makedirs(RESULTS_DIR, exist_ok=True)
    path = os.path.join(RESULTS_DIR, product["id"] + ".json")

    prev = {}
    if os.path.exists(path):
        try:
            with open(path) as f:
                content = f.read().strip()
                if content:
                    prev = json.loads(content)
        except Exception:
            prev = {}

    history = prev.get("history", [])
    history.append({
        "checked_at": datetime.now().isoformat(),
        "new_price":  new_price,
        "count":      len(listings),
        "deals":      sum(1 for l in listings if l["is_deal"]),
    })
    history = history[-48:]

    threshold = float(product.get("threshold", 0.30))
    data = {
        "product":      product,
        "new_price":    new_price,
        "threshold":    round(new_price * (1 - threshold), 2),
        "last_checked": datetime.now().isoformat(),
        "listings":     listings,
        "history":      history,
    }
    with open(path, "w") as f:
        json.dump(data, f, indent=2)
    print(f"   Saved {len(listings)} listings to {path}")

# ── 5. send email ─────────────────────────────────────────────────────────
def send_deal_email(product, deals, new_price):
    rows = ""
    for d in deals:
        rows += f"""
        <tr>
          <td style="padding:12px;border-bottom:1px solid #eee">
            <strong>{d['title']}</strong><br>
            <small style="color:#888">{d['location']}</small>
          </td>
          <td style="padding:12px;border-bottom:1px solid #eee;text-align:center">
            <span style="background:#e8f5e9;color:#1b5e20;padding:3px 10px;
                         border-radius:12px;font-size:13px">{d['condLabel']}</span>
          </td>
          <td style="padding:12px;border-bottom:1px solid #eee;text-align:right">
            <strong style="font-size:20px">${d['price']:.0f}</strong><br>
            <small style="color:#aaa;text-decoration:line-through">${new_price:.0f} new</small>
          </td>
          <td style="padding:12px;border-bottom:1px solid #eee;text-align:center">
            <strong style="color:#2e7d32">{d['discount']:.0f}% off</strong>
          </td>
          <td style="padding:12px;border-bottom:1px solid #eee">
            <a href="{d['link']}" style="background:#1877f2;color:#fff;
               padding:7px 16px;border-radius:6px;text-decoration:none;font-size:13px">
              View
            </a>
          </td>
        </tr>"""

    html = f"""<html><body style="font-family:sans-serif;max-width:640px;margin:auto;padding:24px">
      <h2>Deal alert: {product['name']}</h2>
      <p style="color:#555">{len(deals)} listing(s) matched your criteria
         · Markham / Richmond Hill / Vaughan / Aurora / Newmarket</p>
      <table width="100%" style="border-collapse:collapse;font-size:14px">
        <thead><tr style="background:#f5f5f5;text-align:left">
          <th style="padding:10px">Listing</th>
          <th style="padding:10px;text-align:center">Condition</th>
          <th style="padding:10px;text-align:right">Price</th>
          <th style="padding:10px;text-align:center">Discount</th>
          <th style="padding:10px"></th>
        </tr></thead>
        <tbody>{rows}</tbody>
      </table>
      <p style="color:#bbb;font-size:12px;margin-top:24px">
        Reference new price: ${new_price:.2f} CAD ·
        {datetime.now().strftime("%b %d %Y, %I:%M %p ET")}
      </p>
    </body></html>"""

    msg = MIMEMultipart("alternative")
    msg["Subject"] = (f"Deal alert: {product['name']} "
                      f"({len(deals)} match{'es' if len(deals) > 1 else ''})")
    msg["From"]    = GMAIL_EMAIL
    msg["To"]      = NOTIFY_EMAIL
    msg.attach(MIMEText(html, "html"))

    with smtplib.SMTP_SSL("smtp.gmail.com", 465) as s:
        s.login(GMAIL_EMAIL, GMAIL_APP_PW)
        s.sendmail(GMAIL_EMAIL, NOTIFY_EMAIL, msg.as_string())
    print(f"   Email sent — {len(deals)} deal(s)")

# ── main ──────────────────────────────────────────────────────────────────
async def main():
    products = load_products()
    print(f"\n{'─'*50}")
    print(f"Deal Tracker  {datetime.now().strftime('%Y-%m-%d %H:%M ET')}")
    print(f"Running {len(products)} product(s): {[p['name'] for p in products]}")
    print(f"{'─'*50}")

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(
            headless=True,
            args=["--no-sandbox", "--disable-setuid-sandbox",
                  "--disable-dev-shm-usage",
                  "--disable-blink-features=AutomationControlled"]
        )
        ctx = await browser.new_context(
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/124.0.0.0 Safari/537.36"
            ),
            viewport={"width": 1280, "height": 800},
            locale="en-CA",
            timezone_id="America/Toronto",
        )
        page = await ctx.new_page()

        print("\n[1] Logging into Facebook...")
        await fb_login(ctx, page)
        await page.screenshot(path="debug_login.png", full_page=False)
        print(f"   URL after login: {page.url}")

        for i, product in enumerate(products, 1):
            print(f"\n[Product {i}/{len(products)}] {product['name']}")
            print("   Fetching new price from Walmart.ca...")
            new_price = await get_new_price(page, product["query"],
                                            product.get("new_price", 50))

            print("   Scraping Facebook Marketplace...")
            listings = await scrape_product(page, product, new_price)
            await page.screenshot(path=f"debug_marketplace_{product['id']}.png",
                                  full_page=False)
            print(f"   {len(listings)} listings parsed")

            deals = [l for l in listings if l["is_deal"]]
            print(f"   {len(deals)} deal(s) matched")

            save_results(product, listings, new_price)

            if deals:
                send_deal_email(product, deals, new_price)
            else:
                print("   No deals — no email sent")

        await browser.close()

    print("\nAll done.\n")

if __name__ == "__main__":
    asyncio.run(main())
