"""
sgw_ending_soon.py  (v3)

ShopGoodwill ending-soonest scraper via internal JSON API.

v3 CHANGES (all based on the real API fields):
  - numBids fixed (was wrong field name -> blank in v1/v2).
  - shippingPrice pulled straight from the search response. No separate
    per-item shipping call needed. Full shipping for free, fast.
  - Category tagging now uses the API's real catFullName, not just title
    keyword guesses -> more accurate lane matches.
  - TIME WINDOW targeting: set MIN/MAX minutes-left to grab exactly your
    sourcing sweet spot (e.g. ending in 1-4 hours). Page numbers no longer
    matter -- the window does the work.
  - Added sneakers lane (Nike/Adidas/Jordan) since you flip those too.
  - Extra useful columns: views, minimum_bid, category, image_url.

REQUIREMENTS: pip install requests
RUN: python sgw_ending_soon.py
"""

import csv
import json
import time
from datetime import datetime
from zoneinfo import ZoneInfo
import requests

# SGW API timestamps are Pacific time. Compare against Pacific "now" so the
# minutes-left math is correct regardless of your local timezone.
SGW_TZ = ZoneInfo("America/Los_Angeles")

API_ROOT = "https://buyerapi.shopgoodwill.com/api"
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 6.1; WOW64; rv:12.0) "
                  "Gecko/20100101 Firefox/12.0",
    "Content-Type": "application/json",
}

# ---- TIME WINDOW (your real sourcing control) ----
# Keep only items ending within this window from now. This is what you
# actually want -- enough runway to research, not so far you forget.
# Example: 60 to 240 = "ending in 1 to 4 hours."
MIN_MINUTES_LEFT = 6
MAX_MINUTES_LEFT = 999999

# Page range to sweep. Go wide; the time window filters what matters.
# The script auto-stops early once items are ending past MAX_MINUTES_LEFT.
START_PAGE = 40
END_PAGE = 120
PAGE_SIZE = 40

REQUEST_DELAY = 0.8
OUTPUT_CSV = "sgw_ending_soon.csv"

# Lane tagging checks BOTH the title and the API category path (catFullName).
# Each lane is a list of lowercase keywords; a match in title OR category tags it.
LANE_KEYWORDS = {
    "electronics": ["apple", "ipad", "iphone", "macbook", "imac", "dji",
                    "samsung", "lg", "dell", "hp", "lenovo", "laptop",
                    "tablet", "kindle", "drone", "gopro", "camera", "router",
                    "monitor", "smartwatch", "apple watch", "fitbit", "garmin",
                    "electronics", "computer", "smartphone"],
    "audio": ["bose", "jbl", "sony", "sonos", "beats", "airpods", "sennheiser",
              "klipsch", "harman", "marshall", "speaker", "soundbar",
              "headphones", "earbuds", "subwoofer", "receiver", "amplifier",
              "turntable", "audio"],
    "video_cameras": ["mevo", "gopro", "camcorder", "dslr", "mirrorless",
                      "canon eos", "nikon", "sony alpha", "blackmagic",
                      "webcam", "video camera", "action camera"],
    "games_gaming": ["nintendo", "switch", "playstation", "ps4", "ps5",
                     "xbox", "wii", "gamecube", "sega", "atari", "gameboy",
                     "game boy", "ds", "3ds", "psp", "vita", "console",
                     "controller", "video game", "games", "steam deck"],
    "cards": ["psa", "bgs", "cgc", "sgc", "pokemon", "prizm", "topps",
              "panini", "bowman", "donruss", "fleer", "upper deck", "graded",
              "rookie", "card lot", "trading card", "trading cards",
              "autograph", "numbered", "refractor"],
    "nfl": ["nfl", "football card", "panini prizm", "donruss football",
            "rookie card", "patch auto", "tom brady", "mahomes", "jersey",
            "super bowl", "quarterback", "touchdown",
            "chiefs", "kansas city", "patrick mahomes", "travis kelce",
            "kelce", "isiah pacheco", "rashee rice", "xavier worthy",
            "andy reid", "chiefs kingdom"],
    "toys": ["lego", "star wars", "transformers", "gi joe", "teenage mutant",
             "tmnt", "my little pony", "fisher price", "vintage toy", "kenner",
             "playmates", "minifig", "funko", "hot wheels", "action figure",
             "model kit", "toys"],
    "sporting_goods": ["golf", "callaway", "titleist", "taylormade", "ping",
                       "fishing", "reel", "rod", "shimano", "penn", "fly rod",
                       "bicycle", "bike", "trek", "specialized", "kayak",
                       "tennis", "baseball", "bat", "glove", "hockey", "skis",
                       "snowboard", "treadmill", "dumbbell", "weights",
                       "camping", "tent", "coleman", "yeti", "firearm scope",
                       "binoculars", "archery", "bow", "sporting"],
    "tools": ["dewalt", "milwaukee", "makita", "ryobi", "bosch", "ridgid",
              "craftsman", "kobalt", "snap-on", "snap on", "drill", "impact",
              "saw", "grinder", "wrench set", "socket set", "tool set",
              "power tool", "air compressor", "welder", "multimeter",
              "festool", "klein"],
    "sneakers": ["nike", "adidas", "jordan", "yeezy", "new balance",
                 "sneaker", "shoes men's", "shoes women's"],
}


def build_query(page):
    return {
        "isSize": False, "isWeddingCatagory": "false",
        "isMultipleCategoryIds": False, "isFromHeaderMenuTab": False,
        "layout": "", "isFromHeaderTab": False, "searchText": "",
        "selectedGroup": "", "selectedCategoryIds": "", "selectedSellerIds": "",
        "lowPrice": "0", "highPrice": "999999",
        "searchBuyNowOnly": "", "searchPickupOnly": "false",
        "searchNoPickupOnly": "false", "searchOneCentShippingOnly": "false",
        "searchDescriptions": "false", "searchClosedAuctions": "false",
        "closedAuctionEndingDate": "1/1/2026", "closedAuctionDaysBack": "7",
        "searchCanadaShipping": "false", "searchInternationalShippingOnly": "false",
        "sortColumn": "1", "page": str(page), "pageSize": str(PAGE_SIZE),
        "sortDescending": "false", "savedSearchId": 0, "useBuyerPrefs": "true",
        "searchUSOnlyShipping": "true", "categoryLevelNo": "1",
        "categoryLevel": 1, "categoryId": 0, "partNumber": "", "catIds": "",
        "isFromWidget": False,
    }


def fetch_page(session, page):
    try:
        r = session.post(f"{API_ROOT}/Search/ItemListing",
                         data=json.dumps(build_query(page)), timeout=30)
        r.raise_for_status()
        return r.json().get("searchResults", {}).get("items", []) or []
    except requests.HTTPError as e:
        print(f"  [page {page}] HTTP error: {e} (403 = check User-Agent)")
        return []
    except Exception as e:
        print(f"  [page {page}] error: {e}")
        return []


def tag_lanes(title, cat_full):
    blob = f"{title} {cat_full}".lower()
    return ",".join(lane for lane, kws in LANE_KEYWORDS.items()
                    if any(k in blob for k in kws))


def minutes_until(end_time):
    try:
        # API time is Pacific-local with no tz attached; attach it, then
        # compare against current Pacific time.
        end = datetime.fromisoformat(end_time).replace(tzinfo=SGW_TZ)
        now = datetime.now(SGW_TZ)
        return (end - now).total_seconds() / 60
    except Exception:
        return None


def parse_item(raw):
    item_id = raw.get("itemId")
    title = (raw.get("title") or "").strip()
    cat_full = raw.get("catFullName") or raw.get("categoryName") or ""
    ship = raw.get("shippingPrice")
    return {
        "item_id": item_id,
        "title": title,
        "current_price": raw.get("currentPrice"),
        "num_bids": raw.get("numBids"),
        "minimum_bid": raw.get("minimumBid"),
        "buy_now_price": raw.get("buyNowPrice"),
        "shipping": ship if ship else "",
        "views": raw.get("views"),
        "remaining_time": (raw.get("remainingTime") or "").strip(),
        "end_time": raw.get("endTime"),
        "category": cat_full,
        "lanes": tag_lanes(title, cat_full),
        "image_url": raw.get("imageURL"),
        "link": f"https://shopgoodwill.com/item/{item_id}" if item_id else "",
    }


def collect_rows():
    """Run the scrape and RETURN the rows list (no file writing).
    Reusable by both the CLI main() and the cloud Sheets writer."""
    session = requests.Session()
    session.headers.update(HEADERS)

    rows, seen = [], set()
    skipped_soon = skipped_late = 0

    for page in range(START_PAGE, END_PAGE + 1):
        items = fetch_page(session, page)
        if not items:
            print(f"Page {page}: no items. Stopping.")
            break

        page_mins = [minutes_until(i.get("endTime")) for i in items]
        valid_mins = [m for m in page_mins if m is not None]
        page_min_left = min(valid_mins) if valid_mins else 0

        new = 0
        for raw in items:
            iid = raw.get("itemId")
            if iid in seen:
                continue
            seen.add(iid)
            mins = minutes_until(raw.get("endTime"))
            if mins is not None:
                if mins < MIN_MINUTES_LEFT:
                    skipped_soon += 1
                    continue
                if mins > MAX_MINUTES_LEFT:
                    skipped_late += 1
                    continue
            rows.append(parse_item(raw))
            new += 1

        status = ""
        if new == 0 and page_min_left < MIN_MINUTES_LEFT:
            status = " (still ending too soon -- climbing toward window...)"
        print(f"Page {page}: +{new} (total {len(rows)}) "
              f"[page ends ~{page_min_left:.0f}min out]{status}")
        time.sleep(REQUEST_DELAY)

        if valid_mins and min(valid_mins) > MAX_MINUTES_LEFT:
            print("Past the time window. Stopping early.")
            break

    rows.sort(key=lambda r: (r["lanes"] == "", r["end_time"] or ""))
    print(f"\nCollected {len(rows)} items "
          f"(skipped {skipped_soon} too soon, {skipped_late} too far).")
    return rows


def main():
    print("\n=== ShopGoodwill Ending-Soon Scraper v3 ===")
    print(f"Window: ending in {MIN_MINUTES_LEFT}-{MAX_MINUTES_LEFT} min "
          f"| pages {START_PAGE}-{END_PAGE}\n")

    rows = collect_rows()

    # ---- FILE 1: full raw data ----
    fields = ["item_id", "title", "current_price", "num_bids", "minimum_bid",
              "buy_now_price", "shipping", "views", "remaining_time",
              "end_time", "category", "lanes", "image_url", "link"]
    with open(OUTPUT_CSV, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=fields)
        w.writeheader()
        w.writerows(rows)

    print(f"\nSaved {len(rows)} items -> {OUTPUT_CSV}")
    tagged = sum(1 for r in rows if r["lanes"])
    print(f"{tagged} matched your lanes ({', '.join(LANE_KEYWORDS.keys())}).")

    # ---- FILE 2: bid calculator (contested, lane-tagged, live formulas) ----
    write_bid_calculator(rows)


def _to_int(v):
    try:
        return int(float(v))
    except Exception:
        return 0


def _outbound_ship(title):
    """Rough outbound shipping by item type -- your tiered model."""
    t = title.lower()
    if "lego" in t and ("lb" in t or "bulk" in t):
        return 12
    if any(k in t for k in ["lego", "kenner", "toy", "transformers"]):
        return 10
    if any(k in t for k in ["drone", "dji"]):
        return 15
    if any(k in t for k in ["bose", "speaker", "wave", "media center"]):
        return 12
    return 8


def _untested(t):
    return any(w in t for w in (
        'untested', 'for parts', 'parts/repair', 'parts only', 'as-is',
        'as is', 'not tested', 'not working', 'no charger', 'icloud lock',
        'locked', 'cracked', 'damaged'))


def estimate_sold(title):
    """
    CONSERVATIVE sold-price estimate read from title specifics.
    Returns (low_estimate, confidence, note). Biased LOW on purpose: a
    too-low guess makes you skip (safe); too-high makes you overbid (costly).
    Confidence H/M/L; L means 'triage only, Terapeak required'.
    Untested/for-parts items get a heavy penalty since that guts resale.
    """
    import re
    t = title.lower()
    bad = _untested(t)
    pen = 0.45 if bad else 1.0
    flag = ' [UNTESTED]' if bad else ''

    def has(*w): return any(x in t for x in w)

    # APPLE
    if 'macbook' in t:
        if has('m1', 'm2', 'm3'): v, c = 350, 'M'
        elif has('retina', '2017', '2018', '2019', '2020'): v, c = 200, 'M'
        else: v, c = 90, 'L'
        return round(v * pen), c, 'MacBook-confirm year/chip' + flag
    if 'imac' in t:
        return round(110 * pen), 'L', 'iMac-heavy ship' + flag
    if 'ipad' in t:
        if has('pro'): v, c = 180, 'M'
        elif has('air', '9th', '10th', '8th'): v, c = 110, 'M'
        elif has('mini'): v, c = 60, 'M'
        else: v, c = 45, 'L'
        if 'lot' in t or re.search(r'\b[2-9]\s*pc', t): v = int(v * 1.7); c = 'L'
        return round(v * pen), c, 'iPad-check iCloud lock' + flag
    if re.search(r'\biphone\b', t):
        m = re.search(r'iphone\s*(\d+)', t)
        gen = int(m.group(1)) if m else 0
        base = {15: 300, 14: 250, 13: 200, 12: 140, 11: 110, 10: 80}.get(gen, 50)
        if 'lot' in t or re.search(r'\b[2-9]\s*(pc|pcs)', t): base = int(base * 1.5)
        return round(base * pen), ('L' if bad else 'M'), ('iPhone ' + (str(gen) if gen else '?')) + flag
    if 'airpods' in t:
        return round((70 if 'pro' in t else 40) * pen), 'M', 'AirPods' + flag
    if 'apple watch' in t:
        return round(90 * pen), 'M', 'Apple Watch-confirm series' + flag
    if 'apple tv' in t:
        return round(18 * pen), 'M', 'Apple TV-low' + flag

    # AUDIO
    if 'bose' in t:
        if 'quietcomfort 45' in t or 'qc45' in t: return round(110 * pen), 'M', 'QC45' + flag
        if 'soundlink' in t and 'mini' in t: return round(50 * pen), 'M', 'SoundLink Mini' + flag
        if 'wave' in t: return round(60 * pen), 'L', 'Bose Wave-heavy ship' + flag
        return round(40 * pen), 'L', 'Bose-identify model' + flag
    if 'sonos' in t: return round(90 * pen), 'M', 'Sonos' + flag
    if 'beats' in t and has('studio', 'solo'): return round(55 * pen), 'M', 'Beats' + flag
    if 'jbl' in t: return round(30 * pen), 'L', 'JBL-model varies' + flag
    if has('turntable', 'receiver', 'amplifier', 'speaker'):
        return round(30 * pen), 'L', 'vintage audio-varies' + flag

    # CAMERAS
    if 'mevo' in t: return round(120 * pen), 'M', 'Mevo livestream' + flag
    if 'gopro' in t and has('9', '10', '11', '12'): return round(120 * pen), 'M', 'recent GoPro' + flag
    if 'gopro' in t: return round(35 * pen), 'L', 'older GoPro' + flag
    if has('canon ae-1', 'nikkormat', 'pentax', 'minolta') and 'film' in t:
        return round(45 * pen), 'L', 'vintage film cam-niche' + flag
    if has('digital camera', 'easyshare', 'coolpix'): return round(15 * pen), 'L', 'old point-shoot' + flag
    if 'lens' in t: return round(20 * pen), 'L', 'lens-depends on mount' + flag

    # GAMING
    if 'ps5' in t and 'console' in t: return round(280 * pen), 'M', 'PS5 console' + flag
    if 'xbox series x' in t: return round(280 * pen), 'M', 'Xbox Series X' + flag
    if 'nintendo switch' in t and has('console', 'oled', 'lite'): return round(150 * pen), 'M', 'Switch console' + flag
    if 'cooking mama' in t: return round(55 * pen), 'M', 'Cooking Mama-scarce' + flag
    if 'game lot' in t or re.search(r'lot.*game', t): return 35, 'L', 'game lot-title dependent'
    if has('controller') and has('third-party', '3rd party', 'for ps', 'for xbox'):
        return 12, 'L', '3rd-party controller-low'
    if has('ps4', 'ps5', 'xbox', 'switch') and 'game' in t: return 18, 'L', 'game-title dependent'

    # MUSICAL INSTRUMENTS (guitars resell well)
    if has('epiphone', 'fender', 'gibson', 'ibanez', 'yamaha') and has('guitar'):
        if has('electric'): return round(80 * pen), 'L', 'electric guitar-brand/model varies' + flag
        return round(55 * pen), 'L', 'acoustic guitar-brand/model varies' + flag
    if 'guitar' in t: return round(40 * pen), 'L', 'guitar-brand dependent' + flag

    # TOYS / LEGO
    if 'lego' in t:
        m = re.search(r'(\d+\.?\d*)\s*lb', t)
        if m: return round(float(m.group(1)) * 4.5), 'L', 'bulk LEGO ~$4.5/lb-verify clean'
        # named/licensed sealed sets sell well above generic; lots stack value
        is_lot = 'lot' in t or re.search(r'\b[3-9]\b', t)
        if has('sealed', 'nib', 'new'):
            if has('botanical', 'orchid', 'icons', 'star wars', 'harry potter',
                   'hagrid', 'technic', 'holiday', 'marvel', 'fast'):
                return (55 if is_lot else 38), 'M', 'sealed licensed/premium LEGO'
            return (40 if is_lot else 25), 'M', 'sealed LEGO set/lot'
        if 'minifig' in t: return 20, 'L', 'minifig lot-depends on figs'
        return 18, 'L', 'loose LEGO'
    if 'funko' in t: return 12, 'L', 'Funko-mostly common'
    if has('transformers', 'gi joe', 'tmnt', 'kenner', 'star wars') and not bad:
        return 25, 'L', 'vintage toy-completeness matters'

    # CARDS
    import re as _re
    if _re.search(r'(\d+\.?\d*)\s*lb', t) and 'card' in t:
        m = _re.search(r'(\d+\.?\d*)\s*lb', t)
        return round(float(m.group(1)) * 2), 'L', 'bulk cards ~$2/lb commons'
    if has('psa', 'bgs', 'cgc', 'sgc') and has('10', '9.5', 'gem'):
        return 50, 'L', 'graded-PLAYER is everything'
    if has('mahomes', 'chiefs') and 'card' in t: return 30, 'L', 'Chiefs card-verify player/year'
    if 'pokemon' in t and has('vintage', '1999', '2000', 'base set', 'first edition'):
        return 25, 'L', 'vintage Pokemon-condition critical'
    if has('card lot', 'mixed', 'team', 'mlb', 'nba', 'nfl') and 'card' in t:
        return 12, 'L', 'bulk/team cards-low unless stars'

    # SNEAKERS
    if 'jordan' in t and has('a ma maniere', 'travis', 'off-white', 'dior'):
        return 120, 'L', 'HYPED collab-AUTHENTICATE'
    if 'jordan' in t and not has('kids', 'boys', 'girls', 'child', 'gs', 'td', '7y'):
        return 45, 'L', 'adult Jordan-model/size dependent'
    if 'yeezy' in t: return 50, 'L', 'Yeezy-authenticate'
    if 'ugg' in t and 'boot' in t: return 30, 'L', 'UGG boots-real market'
    if has('kids', 'boys', 'girls', 'child', 'toddler', 'youth') and has('nike', 'shoe', 'jordan'):
        return 12, 'L', 'kids shoes-low resale'
    if has('nike', 'adidas', 'vans', 'new balance'): return 22, 'L', 'mainstream shoe-condition matters'
    if 'boot' in t: return 20, 'L', 'boots-brand dependent'

    # SPORTING GOODS
    if has('callaway', 'titleist', 'taylormade', 'ping') and has('driver', 'iron', 'wood', 'putter', 'golf'):
        return round(60 * pen), 'L', 'golf club-brand/model varies' + flag
    if 'golf' in t and has('set', 'clubs'): return round(70 * pen), 'L', 'golf club set-varies' + flag
    if has('shimano', 'penn', 'abu garcia') and has('reel', 'rod'): return round(45 * pen), 'L', 'fishing reel/rod-model varies' + flag
    if has('yeti') and has('cooler', 'tumbler', 'rambler'): return round(40 * pen), 'L', 'YETI-real resale market' + flag
    if has('trek', 'specialized', 'cannondale') and 'bike' in t: return round(120 * pen), 'L', 'brand bike-heavy ship' + flag
    if has('treadmill', 'peloton', 'bowflex'): return round(100 * pen), 'L', 'fitness equip-heavy/local' + flag
    if has('binoculars', 'rangefinder', 'scope') and not has('toy'): return round(35 * pen), 'L', 'optics-brand varies' + flag

    # TOOLS (brand power tools hold value)
    if has('dewalt', 'milwaukee', 'makita', 'festool', 'snap-on', 'snap on'):
        if has('set', 'kit', 'combo', 'lot'): return round(80 * pen), 'L', 'brand tool set-strong resale' + flag
        if has('drill', 'impact', 'saw', 'grinder', 'driver'): return round(50 * pen), 'L', 'brand power tool' + flag
        return round(40 * pen), 'L', 'brand tool-verify' + flag
    if has('ryobi', 'ridgid', 'craftsman', 'kobalt', 'bosch'):
        if has('set', 'kit', 'combo'): return round(45 * pen), 'L', 'tool set-decent resale' + flag
        return round(25 * pen), 'L', 'tool-verify model' + flag
    if has('socket set', 'wrench set', 'tool set'): return round(30 * pen), 'L', 'tool set-brand matters' + flag

    return 20, 'L', 'generic-Terapeak needed'


def write_bid_calculator(rows):
    """
    Write a Google-Sheets-ready bid calculator. Keeps lane-tagged items with
    at least 1 bid (live market interest), sorts most-contested first, and
    embeds LIVE formulas plus a CONSERVATIVE estimated-sold column so the list
    is pre-ranked before you paste real comps.
      MAX BID  = most you can pay and still net at least MIN_NET per item
      Proj Net = your profit if you won at the current bid
      Clears $20? = YES/no against the flat per-item floor
    """
def build_calculator_rows():
    """Return the calculator as a list-of-lists (header + rows) with live
    Sheets formulas. Reused by both the CSV writer and the cloud Sheets push.
    Note: formulas use A1-style refs, so row order here defines the refs."""
    MIN_NET = 20.00
    EBAY_FEE = 0.13

    rows = collect_rows()
    cand = [r for r in rows if r["lanes"] and _to_int(r["num_bids"]) >= 1]

    scored = []
    for r in cand:
        est, conf, note = estimate_sold(r["title"])
        ship = r["shipping"] if r["shipping"] not in ("", None) else 0
        try:
            ship_f = float(ship)
        except Exception:
            ship_f = 0.0
        ob = _outbound_ship(r["title"])
        est_max = est - est * EBAY_FEE - ship_f - ob - MIN_NET
        generic = note.startswith("generic")
        if est_max <= 0 and conf in ("H", "M"):
            status = "SKIP (est below $20)"
        elif generic or conf == "L":
            status = "VERIFY - value uncertain"
        else:
            status = "REVIEW"
        scored.append((r, est, conf, note, ob, ship, est_max, status))

    status_rank = {"REVIEW": 0, "VERIFY - value uncertain": 1, "SKIP (est below $20)": 2}
    conf_rank = {"H": 0, "M": 1, "L": 2}
    scored.sort(key=lambda x: (status_rank.get(x[7], 1),
                               conf_rank.get(x[2], 3), -x[6]))

    header = ["Item", "Lanes", "Status", "Bids", "Current Bid", "Time Left",
              "SGW Ship", "Est Sold (rough)", "Conf", "Note",
              "PASTE Comp Here", "Outbound Ship", "MAX BID ($20 floor)",
              "Proj Net", "Clears $20?", "Link"]
    out = [header]
    for idx, (r, est, conf, note, ob, ship, est_max, status) in enumerate(scored, start=2):
        maxbid = (f'=IF(K{idx}="","",'
                  f'K{idx}-K{idx}*{EBAY_FEE}-G{idx}-L{idx}-{MIN_NET})')
        projnet = (f'=IF(K{idx}="","",'
                   f'K{idx}-K{idx}*{EBAY_FEE}-E{idx}-G{idx}-L{idx})')
        clears = (f'=IF(K{idx}="","",IF(N{idx}>={MIN_NET},"YES","no"))')
        out.append([r["title"], r["lanes"], status, r["num_bids"],
                    r["current_price"], (r["remaining_time"] or "").strip(),
                    ship, est, conf, note, "", ob, maxbid, projnet, clears,
                    r["link"]])

    n_review = sum(1 for x in scored if x[7] == "REVIEW")
    n_verify = sum(1 for x in scored if x[7].startswith("VERIFY"))
    n_skip = sum(1 for x in scored if x[7].startswith("SKIP"))
    print(f"Calculator: {len(cand)} contested | {n_review} REVIEW "
          f"| {n_verify} VERIFY | {n_skip} SKIP")
    return out


def write_bid_calculator(rows):
    """CLI version: build the calculator and write it to a local CSV."""
    CALC_CSV = "bid_calculator.csv"
    out = build_calculator_rows()
    with open(CALC_CSV, "w", newline="", encoding="utf-8") as f:
        csv.writer(f).writerows(out)
    print(f"\nBID CALCULATOR -> {CALC_CSV}")
    print("  Nothing silently cut. VERIFY = eyeball it; estimator wasn't sure.")
    print("  Paste a real comp in the comp column; MAX BID / Proj Net fill in.")


def _legacy_write_bid_calculator(rows):
    """(unused) original inline writer kept for reference."""
    CALC_CSV = "bid_calculator.csv"
    MIN_NET = 20.00          # flat net-profit floor per item (your time/effort)
    EBAY_FEE = 0.13

    cand = [r for r in rows if r["lanes"] and _to_int(r["num_bids"]) >= 1]

    # Pre-score each by estimated headroom so the best float to the top.
    scored = []
    for r in cand:
        est, conf, note = estimate_sold(r["title"])
        ship = r["shipping"] if r["shipping"] not in ("", None) else 0
        try:
            ship_f = float(ship)
        except Exception:
            ship_f = 0.0
        ob = _outbound_ship(r["title"])
        est_max = est - est * EBAY_FEE - ship_f - ob - MIN_NET

        # STATUS: never silently cut. Only mark SKIP when we're CONFIDENT
        # (H/M) the estimate is below floor. Uncertain (L) generic items get
        # VERIFY so you eyeball them instead of trusting a blind cut.
        generic = note.startswith("generic")
        if est_max <= 0 and conf in ("H", "M"):
            status = "SKIP (est below $20)"
        elif generic or conf == "L":
            status = "VERIFY - value uncertain"
        else:
            status = "REVIEW"
        scored.append((r, est, conf, note, ob, ship, est_max, status))

    # Sort: actionable first. REVIEW > VERIFY > SKIP, then by est headroom.
    status_rank = {"REVIEW": 0, "VERIFY - value uncertain": 1, "SKIP (est below $20)": 2}
    conf_rank = {"H": 0, "M": 1, "L": 2}
    scored.sort(key=lambda x: (status_rank.get(x[7], 1),
                               conf_rank.get(x[2], 3), -x[6]))

    header = ["Item", "Lanes", "Status", "Bids", "Current Bid", "Time Left",
              "SGW Ship", "Est Sold (rough)", "Conf", "Note",
              "PASTE Comp Here", "Outbound Ship", "MAX BID ($20 floor)",
              "Proj Net", "Clears $20?", "Link"]
    # Col letters: A Item B Lanes C Status D Bids E CurBid F Time G SGWship
    #   H EstSold I Conf J Note K COMP L Outbound M MaxBid N ProjNet
    #   O Clears P Link
    out = [header]
    for idx, (r, est, conf, note, ob, ship, est_max, status) in enumerate(scored, start=2):
        # Formulas key off the pasted comp in column K.
        maxbid = (f'=IF(K{idx}="","",'
                  f'K{idx}-K{idx}*{EBAY_FEE}-G{idx}-L{idx}-{MIN_NET})')
        projnet = (f'=IF(K{idx}="","",'
                   f'K{idx}-K{idx}*{EBAY_FEE}-E{idx}-G{idx}-L{idx})')
        clears = (f'=IF(K{idx}="","",IF(N{idx}>={MIN_NET},"YES","no"))')
        out.append([r["title"], r["lanes"], status, r["num_bids"],
                    r["current_price"], (r["remaining_time"] or "").strip(),
                    ship, est, conf, note, "", ob, maxbid, projnet, clears,
                    r["link"]])

    with open(CALC_CSV, "w", newline="", encoding="utf-8") as f:
        csv.writer(f).writerows(out)

    n_review = sum(1 for x in scored if x[7] == "REVIEW")
    n_verify = sum(1 for x in scored if x[7].startswith("VERIFY"))
    n_skip = sum(1 for x in scored if x[7].startswith("SKIP"))
    print(f"\nBID CALCULATOR -> {CALC_CSV} ({len(cand)} contested items)")
    print(f"  {n_review} REVIEW (est clears $20) | {n_verify} VERIFY (uncertain) "
          f"| {n_skip} SKIP (confident below floor)")
    print("  Nothing silently cut. VERIFY = eyeball it; estimator wasn't sure.")
    print("  Paste a real comp in the comp column; MAX BID / Proj Net fill in.")


if __name__ == "__main__":
    main()
