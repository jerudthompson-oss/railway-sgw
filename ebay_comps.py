"""
ebay_comps.py — resale estimate + sell-speed proxy via the eBay Browse API.

Browse API gives ACTIVE listings (asking prices), not sold comps — the
Marketplace Insights API (90-day sold) was denied for this app. So:

  * Resale estimate  = median of active asks (configurable percentile +
    optional haircut), outliers trimmed. Asks skew high, so this is
    optimistic; tune ASK_HAIRCUT to pull it toward real resale.
  * Sell-speed proxy = a fast/medium/slow tag derived from active SUPPLY
    (how many are currently listed). Low supply -> scarcer -> likely faster.
    This is a PROXY, not a true sell-through rate (that needs sold data).

Auth: OAuth2 client-credentials (App ID + Cert ID) -> application token,
cached until expiry. Endpoint: api.ebay.com/buy/browse/v1/item_summary/search
"""
from __future__ import annotations
import os
import time
import base64
import statistics
import requests

EBAY_CLIENT_ID     = os.environ.get("EBAY_CLIENT_ID", "")      # App ID
EBAY_CLIENT_SECRET = os.environ.get("EBAY_CLIENT_SECRET", "")  # Cert ID
EBAY_MARKETPLACE   = os.environ.get("EBAY_MARKETPLACE", "EBAY_US")

# Estimate tuning
ASK_PERCENTILE = float(os.environ.get("ASK_PERCENTILE", "50"))  # 50 = median
ASK_HAIRCUT    = float(os.environ.get("ASK_HAIRCUT", "1.0"))    # 0.85 = shave 15%
MIN_COMPS      = int(os.environ.get("MIN_COMPS", "3"))          # below -> no estimate
MAX_COMPS      = int(os.environ.get("MAX_COMPS", "50"))         # pull cap

# Sell-speed proxy thresholds (active-listing supply counts)
FAST_MAX_SUPPLY   = int(os.environ.get("FAST_MAX_SUPPLY", "25"))    # <= -> fast
SLOW_MIN_SUPPLY   = int(os.environ.get("SLOW_MIN_SUPPLY", "150"))   # >= -> slow

# Comp cache: the worker loops every 15-180 min and the same auctions recur
# run after run, so successful lookups are cached by query for this long.
COMP_CACHE_TTL_MIN = int(os.environ.get("COMP_CACHE_TTL_MIN", "360"))
_COMP_CACHE_MAX = 2000
_comp_cache: dict[str, tuple[float, dict]] = {}

OAUTH_URL  = "https://api.ebay.com/identity/v1/oauth2/token"
BROWSE_URL = "https://api.ebay.com/buy/browse/v1/item_summary/search"
SCOPE      = "https://api.ebay.com/oauth/api_scope"

_token = {"value": None, "expires": 0}


def _have_creds() -> bool:
    return bool(EBAY_CLIENT_ID and EBAY_CLIENT_SECRET)


def _get_token() -> str | None:
    if not _have_creds():
        return None
    if _token["value"] and time.time() < _token["expires"] - 60:
        return _token["value"]
    basic = base64.b64encode(
        f"{EBAY_CLIENT_ID}:{EBAY_CLIENT_SECRET}".encode()).decode()
    r = requests.post(
        OAUTH_URL,
        headers={"Authorization": f"Basic {basic}",
                 "Content-Type": "application/x-www-form-urlencoded"},
        data={"grant_type": "client_credentials", "scope": SCOPE},
        timeout=30,
    )
    if r.status_code != 200:
        print(f"[ebay] OAuth failed {r.status_code}: {r.text[:160]}")
        return None
    j = r.json()
    _token["value"] = j["access_token"]
    _token["expires"] = time.time() + int(j.get("expires_in", 7200))
    return _token["value"]


def _trim_outliers(prices: list[float]) -> list[float]:
    """Drop the cheapest/most-expensive 10% to kill junk + aspirational fliers."""
    if len(prices) < 5:
        return prices
    prices = sorted(prices)
    k = max(1, len(prices) // 10)
    return prices[k:-k]


def _percentile(sorted_vals: list[float], pct: float) -> float:
    if not sorted_vals:
        return 0.0
    if len(sorted_vals) == 1:
        return sorted_vals[0]
    rank = (pct / 100.0) * (len(sorted_vals) - 1)
    lo = int(rank)
    frac = rank - lo
    if lo + 1 < len(sorted_vals):
        return sorted_vals[lo] * (1 - frac) + sorted_vals[lo + 1] * frac
    return sorted_vals[lo]


def estimate(query: str) -> dict:
    """Return resale estimate + sell-speed proxy for a search phrase.

    Returns dict:
      { resale_usd: float|None, comps: int, total_supply: int|None,
        sell_speed: 'fast'|'medium'|'slow'|'unknown', source: str, note: str }
    """
    token = _get_token()
    if not token:
        return {"resale_usd": None, "comps": 0, "total_supply": None,
                "sell_speed": "unknown", "source": "none",
                "note": "eBay creds unset or OAuth failed"}

    params = {
        "q": query[:100],
        "limit": str(min(MAX_COMPS, 50)),
        "filter": "buyingOptions:{FIXED_PRICE},conditionIds:{3000|4000|5000|6000}",
        "sort": "price",
    }
    try:
        r = requests.get(
            BROWSE_URL, params=params,
            headers={"Authorization": f"Bearer {token}",
                     "X-EBAY-C-MARKETPLACE-ID": EBAY_MARKETPLACE},
            timeout=30,
        )
    except Exception as e:
        return {"resale_usd": None, "comps": 0, "total_supply": None,
                "sell_speed": "unknown", "source": "error", "note": str(e)[:120]}

    if r.status_code != 200:
        return {"resale_usd": None, "comps": 0, "total_supply": None,
                "sell_speed": "unknown", "source": "error",
                "note": f"browse {r.status_code}: {r.text[:120]}"}

    j = r.json()
    total_supply = j.get("total")  # total active listings matching the query
    items = j.get("itemSummaries") or []
    prices = []
    for it in items:
        p = (it.get("price") or {}).get("value")
        if p:
            try:
                prices.append(float(p))
            except ValueError:
                pass

    if len(prices) < MIN_COMPS:
        speed = _speed_from_supply(total_supply)
        return {"resale_usd": None, "comps": len(prices), "total_supply": total_supply,
                "sell_speed": speed, "source": "browse",
                "note": f"only {len(prices)} comps (<{MIN_COMPS}) — VERIFY"}

    trimmed = _trim_outliers(prices)
    est = _percentile(sorted(trimmed), ASK_PERCENTILE) * ASK_HAIRCUT
    speed = _speed_from_supply(total_supply)
    return {"resale_usd": round(est, 2), "comps": len(prices),
            "total_supply": total_supply, "sell_speed": speed,
            "source": "browse",
            "note": f"p{ASK_PERCENTILE:.0f} of {len(trimmed)} asks"
                    + (f" x{ASK_HAIRCUT}" if ASK_HAIRCUT != 1.0 else "")}


def _speed_from_supply(total_supply):
    """Active-supply proxy for sell speed. Lower supply -> faster mover.
    NOTE: proxy only; true sell-through needs sold data (Marketplace Insights,
    which was denied). Directional, not a real percentage."""
    if total_supply is None:
        return "unknown"
    if total_supply <= FAST_MAX_SUPPLY:
        return "fast"
    if total_supply >= SLOW_MIN_SUPPLY:
        return "slow"
    return "medium"


def estimate_cached(query: str) -> dict:
    """estimate() with a TTL cache keyed by the normalized query.

    Only usable results (a real estimate, or a definitive low-comp answer
    from the API) are cached; error/no-cred results are not, so a flaky
    run retries next time instead of pinning a failure for hours.
    """
    key = " ".join((query or "").lower().split())
    now = time.time()
    hit = _comp_cache.get(key)
    if hit and now - hit[0] < COMP_CACHE_TTL_MIN * 60:
        out = dict(hit[1])
        out["source"] = out.get("source", "") + "+cache"
        return out

    result = estimate(query)
    if result.get("source") == "browse":
        if len(_comp_cache) >= _COMP_CACHE_MAX:
            oldest = sorted(_comp_cache.items(), key=lambda kv: kv[1][0])
            for k, _ in oldest[:_COMP_CACHE_MAX // 4]:
                _comp_cache.pop(k, None)
        _comp_cache[key] = (now, dict(result))
    return result
