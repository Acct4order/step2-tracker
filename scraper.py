import asyncio, json, os, re, smtplib
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from datetime import datetime
from playwright.async_api import async_playwright

# ── credentials (set these as GitHub Secrets, never hardcode) ─────────────
FB_EMAIL         = os.environ["FB_EMAIL"]
FB_PASSWORD      = os.environ["FB_PASSWORD"]
GMAIL_EMAIL      = os.environ["GMAIL_EMAIL"]
GMAIL_APP_PW     = os.environ["GMAIL_APP_PASSWORD"]
NOTIFY_EMAIL     = os.environ.get("NOTIFY_EMAIL", GMAIL_EMAIL)

# ── search config ─────────────────────────────────────────────────────────
QUERY        = "step 2 water table"
LAT, LNG     = 43.8561, -79.3370
RADIUS_KM    = 25
DEAL_CUTOFF  = 0.30
LIKE_NEW_KW  = ["like new", "likenew", "like-new", "excellent",
                "barely used", "brand new", "mint condition", "never used"]
GOOD_KW      = ["good condition", "good shape", "great condition",
                "works great", "clean", "no damage"]
RESULTS_FILE = "results.json"

# ── 1. new price from Walmart.ca ──────────────────────────────────────────
async def get_new_price(page):
    fallback = 74.0
    try:
        await page.goto(
            "https://www.walmart.ca/search?q=step+2+water+table",
            wait_until="domcontentloaded", timeout=30000
        )
        await page.wait_for_timeout(3000)
        for sel in ['[data-automation="product-price"]', '.price-characteristic', '[class*="price"]']:
            el = await page.query_selector(sel)
            if el:
                txt = await el.inner_text()
                m = re.search(r"[\d]+\.?\d*", txt.replace(",", ""))
                if m:
                    p = float(m.group())
                    if 20 < p < 300:
                        print(f"   Walmart price: ${p:.2f} CAD")
                        return p
    except Exception as e:
        print(f"   Walmart scrape failed ({e}), using fallback ${fallback}")
    return fallback

# ── 2. Facebook login ─────────────────────────────────────────────────────
async def fb_login(page):
    await page.goto("https://www.facebook.com/login/", wait_until="domcontentloaded", timeout=30000)
    await page.wait_for_timeout(3000)

    for btn_txt in ["Accept all", "Allow all cookies", "Allow essential and optional cookies",
                    "Only allow essential cookies", "Decline optional cookies", "Close"]:
        try:
            await page.click(f'button:has-text("{btn_txt}")', timeout=2000)
            await page.wait_for_timeout(1000)
        except Exception:
            pass

    email_sel = None
    for sel in ['input[name="email"]', "#email", 'input[type="email"]', 'input[autocomplete="username"]']:
        try:
            await page.wait_for_selector(sel, timeout=5000)
            email_sel = sel
            break
        except Exception:
            continue

    if not email_sel:
        raise RuntimeError("Could not find Facebook login form")

    await page.fill(email_sel, FB_EMAIL)

    pass_sel = None
    for sel in ['input[name="pass"]', "#pass", 'input[type="password"]']:
        try:
            await page.wait_for_selector(sel, timeout=3000)
            pass_sel = sel
            break
        except Exception:
            continue

    if not pass_sel:
        raise RuntimeError("Could not find Facebook password field")

    await page.fill(pass_sel, FB_PASSWORD)
    await page.wait_for_timeout(500)

    for sel in ['[name="login"]', 'button[type="submit"]', 'input[type="submit"]']:
        try:
            await page.click(sel, timeout=3000)
            break
        except Exception:
            continue

    await page.wait_for_timeout(6000)

    if "checkpoint" in page.url or "two_step" in page.url:
        raise RuntimeError("Facebook is asking for 2FA — disable it on this account")

    print("   Facebook login OK")

# ── 3. scrape marketplace ─────────────────────────────────────────────────
async def scrape_marketplace(page, new_price):
    urls = [
        "https://www.facebook.com/marketplace/markham/search/?query=" + QUERY.replace(" ", "+"),
        "https://www.facebook.com/marketplace/search/?query=" + QUERY.replace(" ", "+")
        + f"&latitude={LAT}&longitude={LNG}&radius={RADIUS_KM}&exact=false",
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
            title = max(candidates, key=len) if candidates else "Step 2 Water Table"

            location = next(
                (t for t in texts if any(
                    c in t for c in ["Markham", "Richmond Hill", "Unionville",
                                     "Scarborough", "Toronto", "Thornhill", "Stouffville", "ON"]
                )), "Nearby"
            )

            cond, cond_label = classify_condition(title)
            discount = (new_price - price) / new_price

            listings.append({
                "title":      title[:80],
                "price":      price,
                "location":   location,
                "condition":  cond,
                "condLabel":  cond_label,
                "discount":   round(discount * 100, 1),
                "link":       full_url,
                "is_deal":    (cond == "like_new" and discount >= DEAL_CUTOFF),
                "scraped_at": datetime.now().isoformat(),
            })
        except Exception as e:
            print(f"   Card parse error: {e}")

    return sorted(listings, key=lambda x: x["price"])

# ── helpers ───────────────────────────────────────────────────────────────
def classify_condition(text):
    t = text.lower()
    if any(k in t for k in LIKE_NEW_KW):
        return "like_new", "Like new"
    if any(k in t for k in GOOD_KW):
        return "good", "Good"
    return "unknown", "Unknown"

# ── 4. send email ─────────────────────────────────────────────────────────
def send_deal_email(deals, new_price):
    rows = ""
    for d in deals:
        rows += f"""
        <tr>
          <td style="padding:12px;border-bottom:1px solid #eee">
            <strong>{d['title']}</strong><br>
            <small style="color:#888">{d['location']}</small>
          </td>
          <td style="padding:12px;border-bottom:1px solid #eee;text-align:center">
            <span style="background:#e8f5e9;color:#1b5e20;padding:3px 10px;border-radius:12px;font-size:13px">
              {d['condLabel']}
            </span>
          </td>
          <td style="padding:12px;border-bottom:1px solid #eee;text-align:right">
            <strong style="font-size:20px">${d['price']:.0f}</strong>
            <br><small style="color:#aaa;text-decoration:line-through">${new_price:.0f} new</small>
          </td>
          <td style="padding:12px;border-bottom:1px solid #eee;text-align:center">
            <strong style="color:#2e7d32;font-size:16px">{d['discount']:.0f}% off</strong>
          </td>
          <td style="padding:12px;border-bottom:1px solid #eee;text-align:center">
            <a href="{d['link']}" style="background:#1877f2;color:#fff;padding:7px 16px;
               border-radius:6px;text-decoration:none;font-size:13px">View</a>
          </td>
        </tr>"""

    html = f"""<html><body style="font-family:sans-serif;max-width:640px;margin:auto;padding:24px">
      <h2>Step 2 Water Table — deal alert</h2>
      <p style="color:#555">{len(deals)} listing(s) matched: Like new + 30%+ off + Markham / Richmond Hill</p>
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
        Reference new price: ${new_price:.2f} CAD (Walmart.ca) · {datetime.now().strftime("%b %d %Y, %I:%M %p ET")}
      </p>
    </body></html>"""

    msg = MIMEMultipart("alternative")
    msg["Subject"] = f"Deal alert: Step 2 Water Table ({len(deals)} match{'es' if len(deals) > 1 else ''})"
    msg["From"]    = GMAIL_EMAIL
    msg["To"]      = NOTIFY_EMAIL
    msg.attach(MIMEText(html, "html"))

    with smtplib.SMTP_SSL("smtp.gmail.com", 465) as s:
        s.login(GMAIL_EMAIL, GMAIL_APP_PW)
        s.sendmail(GMAIL_EMAIL, NOTIFY_EMAIL, msg.as_string())
    print(f"   Email sent — {len(deals)} deal(s)")

# ── 5. save results ───────────────────────────────────────────────────────
def save_results(listings, new_price):
    prev = {}
    if os.path.exists(RESULTS_FILE):
        try:
            with open(RESULTS_FILE) as f:
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

    data = {
        "new_price":    new_price,
        "threshold":    round(new_price * (1 - DEAL_CUTOFF), 2),
        "last_checked": datetime.now().isoformat(),
        "listings":     listings,
        "history":      history,
    }
    with open(RESULTS_FILE, "w") as f:
        json.dump(data, f, indent=2)
    print(f"   Saved {len(listings)} listings to {RESULTS_FILE}")

# ── main ──────────────────────────────────────────────────────────────────
async def main():
    print("\n" + "-" * 50)
    print(f"Step 2 Water Table Tracker  {datetime.now().strftime('%Y-%m-%d %H:%M ET')}")
    print("-" * 50)

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(
            headless=True,
            args=["--no-sandbox", "--disable-setuid-sandbox",
                  "--disable-dev-shm-usage", "--disable-blink-features=AutomationControlled"]
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

        print("\n[1/4] Fetching new price from Walmart.ca...")
        new_price = await get_new_price(page)

        print("\n[2/4] Logging into Facebook...")
        await fb_login(page)

        print("\n[3/4] Scraping Facebook Marketplace...")
        listings = await scrape_marketplace(page, new_price)
        print(f"   {len(listings)} listings parsed")

        await browser.close()

    deals = [l for l in listings if l["is_deal"]]
    print(f"   {len(deals)} deal(s) matched criteria")

    print("\n[4/4] Saving results...")
    save_results(listings, new_price)

    if deals:
        send_deal_email(deals, new_price)
    else:
        print("   No deals this run — no email sent")

    print("\nDone\n")

if __name__ == "__main__":
    asyncio.run(main())
