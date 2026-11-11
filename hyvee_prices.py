"""
hyvee_prices.py - Competitor price snapshot from Hy-Vee Aisles Online.

Uses a real browser (Playwright) the way a shopper would:
  1. Opens a public Hy-Vee category page
  2. Picks the store with the site's own "change store" picker
  3. Scrolls each category page and reads the product cards
     (name, size, price, deal badge, product link / id)

Only public category pages are read. Hy-Vee's robots.txt disallows
/aisles-online/search, so this never uses search. Requests are paced.

Usage:
  python hyvee_prices.py                 # default store + cleaning aisle
  python hyvee_prices.py --out snap.csv

ENV VARS (all optional):
  HYVEE_STORE_QUERY  -> text typed into the store finder (default 'Ames, IA')
  HYVEE_STORE_NAME   -> exact store button name to pick (default 'Ames #2')
  HYVEE_CATEGORIES   -> comma-separated category paths under
                        /aisles-online/browse/ (default: cleaning aisle below)
  HYVEE_MAX_SCROLLS  -> max scroll rounds per category (default 40)

Requires: pip install playwright && playwright install chromium
"""

import os
import re
import csv
import sys
import time
import argparse
from datetime import datetime
from zoneinfo import ZoneInfo

from playwright.sync_api import sync_playwright

BASE = "https://www.hy-vee.com"
BROWSE = BASE + "/aisles-online/browse/"

# "Cleaners in general": surface/bath/kitchen/glass cleaners, bleach,
# Swiffer, wipes (cleaning-supplies), detergent / softener / stain
# (laundry-supplies), dish soap (dishwashing-supplies), drain cleaner.
DEFAULT_CATEGORIES = [
    "household-and-laundry/household-supplies/cleaning-supplies",
    "household-and-laundry/household-supplies/laundry-supplies",
    "household-and-laundry/household-supplies/dishwashing-supplies",
    "household-and-laundry/household-supplies/drain-and-septic-system-care",
]

CARD_JS = """
() => [...document.querySelectorAll('a[data-testid="details-clickable-wrapper"]')].map(a => {
  const card = a.closest('[data-testid="wrapper"]') || a.parentElement.parentElement;
  const txt = s => { const e = card.querySelector(s); return e ? e.innerText.trim() : ''; };
  return {
    href: a.getAttribute('href') || '',
    price: txt('[data-testid="product-card-price-amount"]'),
    price_block: (a.querySelector('[class*="product-card-price__Container"]') || {}).innerText || '',
    text: a.innerText,
    card_text: card.innerText,
    deal: [...card.querySelectorAll('[data-testid^="badge-"]')].map(b => (b.innerText || '').trim()).filter(Boolean).join(' / '),
  };
})
"""


def central_now(fmt="%Y-%m-%d %H:%M"):
    return datetime.now(ZoneInfo("America/Chicago")).strftime(fmt)


def money(s):
    m = re.search(r"\$\s*([\d,]+\.\d{2})", s or "")
    return float(m.group(1).replace(",", "")) if m else None


def parse_card(c, category):
    href = c["href"]
    m = re.search(r"/p/(\d+)", href)
    pid = m.group(1) if m else ""
    # Card text looks like "$4.97\nLysol Power Toilet Bowl ...\n24 fl oz"
    lines = [l.strip() for l in c["text"].split("\n") if l.strip()]
    sponsored = bool(lines) and lines[0].lower() == "sponsored"
    lines = [l for l in lines if not l.startswith("$") and l.lower() != "sponsored"
             and not re.fullmatch(r"(each|/\s*\w+|\d+ for)", l, re.I)]
    name = lines[0] if lines else ""
    size = lines[1] if len(lines) > 1 else ""
    prices = re.findall(r"\$[\d,]+\.\d{2}", c["price_block"])
    price = money(c["price"]) if c["price"] else (money(prices[0]) if prices else None)
    regular = money(prices[1]) if len(prices) > 1 else None
    return {
        "product_id": pid,
        "name": name,
        "size": size,
        "price": price,
        "regular_price": regular,
        "deal": c["deal"],
        "sponsored": sponsored or "Sponsored" in c["card_text"],
        "category": category.rsplit("/", 1)[-1],
        "url": BASE + href if href.startswith("/") else href,
    }


def select_store(page, query, store_name):
    page.get_by_role("button", name=re.compile("change store", re.I)).first.click()
    box = page.get_by_placeholder(re.compile("city, state or zip", re.I))
    box.wait_for(timeout=15000)
    box.fill(query)
    box.press("Enter")
    btn = page.get_by_role(
        "button", name=re.compile(r"^" + re.escape(store_name) + r" \d"))
    btn.first.wait_for(timeout=20000)
    btn.first.click()
    page.wait_for_timeout(4000)
    want = re.compile(re.escape(store_name) + r".*change store", re.I)
    try:
        page.get_by_role("button", name=want).first.wait_for(timeout=20000)
    except Exception:
        raise RuntimeError(f"store picker did not switch to {store_name}")
    return f"Shopping {store_name}"


def scrape_category(page, path, max_scrolls):
    page.goto(BROWSE + path, wait_until="domcontentloaded", timeout=60000)
    page.wait_for_selector('[data-testid="product-card-price-amount"]', timeout=30000)
    last, stable = -1, 0
    for _ in range(max_scrolls):
        n = page.locator('a[data-testid="details-clickable-wrapper"]').count()
        if n == last:
            stable += 1
            if stable >= 3:
                break
        else:
            stable = 0
        last = n
        page.mouse.wheel(0, 20000)
        page.wait_for_timeout(2500)  # polite pacing between loads
    rows, seen = [], set()
    for c in page.evaluate(CARD_JS):
        r = parse_card(c, path)
        if r["product_id"] and r["product_id"] not in seen:
            seen.add(r["product_id"])
            rows.append(r)
    return rows


def run(categories, store_query, store_name, max_scrolls, headless=True):
    out = []
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=headless)
        ctx = browser.new_context(viewport={"width": 1400, "height": 1000})
        page = ctx.new_page()
        page.goto(BROWSE + categories[0], wait_until="domcontentloaded", timeout=60000)
        page.wait_for_timeout(4000)
        shown = select_store(page, store_query, store_name)
        print(f"[hyvee] {shown}", file=sys.stderr)
        stamp = central_now()
        for path in categories:
            rows = scrape_category(page, path, max_scrolls)
            print(f"[hyvee] {path}: {len(rows)} products", file=sys.stderr)
            for r in rows:
                r["store"] = store_name
                r["scraped_at"] = stamp
            out.extend(rows)
            time.sleep(3)
        browser.close()
    return out


FIELDS = ["scraped_at", "store", "category", "product_id", "name", "size",
          "price", "regular_price", "deal", "sponsored", "url"]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default=f"hyvee_{central_now('%Y%m%d_%H%M')}.csv")
    ap.add_argument("--headed", action="store_true")
    a = ap.parse_args()
    cats = [c.strip().strip("/") for c in os.environ.get(
        "HYVEE_CATEGORIES", ",".join(DEFAULT_CATEGORIES)).split(",") if c.strip()]
    rows = run(cats,
               os.environ.get("HYVEE_STORE_QUERY", "Ames, IA"),
               os.environ.get("HYVEE_STORE_NAME", "Ames #2"),
               int(os.environ.get("HYVEE_MAX_SCROLLS", "40")),
               headless=not a.headed)
    with open(a.out, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=FIELDS)
        w.writeheader()
        w.writerows(rows)
    print(f"[hyvee] wrote {len(rows)} rows -> {a.out}", file=sys.stderr)


if __name__ == "__main__":
    main()
