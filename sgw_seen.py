"""
sgw_seen.py — NEW/SEEN tracking across runs (mirrors hibid_seen).

An item is NEW until it has appeared in a successfully delivered report;
a failed send keeps it NEW next run. Stored as a JSON file of
item_id -> last-reported timestamp, pruned after TTL_DAYS.

Note: Railway's filesystem is ephemeral across redeploys, so after a
deploy every item shows NEW once. Fine for a twice-daily report.
"""
import json
import os
import time

SEEN_PATH = os.environ.get("SEEN_PATH", "seen_items.json")
TTL_DAYS = int(os.environ.get("SEEN_TTL_DAYS", "14"))


def _load():
    try:
        with open(SEEN_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def _save(store):
    try:
        with open(SEEN_PATH, "w", encoding="utf-8") as f:
            json.dump(store, f)
    except Exception as e:
        print(f"[seen] save failed: {e}")


def is_new(item_id):
    return str(item_id) not in _load()


def mark_reported(item_ids):
    """Call ONLY after a delivered report; keeps NEW items NEW on failure."""
    store = _load()
    now = time.time()
    cutoff = now - TTL_DAYS * 86400
    store = {k: v for k, v in store.items() if v > cutoff}
    for iid in item_ids:
        store[str(iid)] = now
    _save(store)
    print(f"[seen] tracking {len(store)} item(s)")
