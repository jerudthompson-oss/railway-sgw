"""
main.py - Cloud runner: twice-daily ShopGoodwill scrape -> emailed to you.

Runs on Railway. At each scheduled run time (11:00 AM and 6:00 PM Central
by default) it:
  1. Scrapes ShopGoodwill (pages/window/lanes from sgw_core.py)
  2. Builds the combed, flagged, value-ranked worklist with live eBay
     Browse comps (ebay_comps.py, same engine as the HiBid build)
  3. Emails you a phone-readable summary + attaches the full CSV

No Google Cloud / SMTP needed. Sends via the Resend HTTPS API because
Railway blocks outbound SMTP ports (465/587).

ENV VARS (set in Railway, never in code):
  RESEND_API_KEY     -> your Resend API key (starts with re_)
  EMAIL_TO           -> where to send the worklist
  EMAIL_FROM         -> sender; 'onboarding@resend.dev' works out of the box
                        (optional, that's the default)
  RUN_TIMES          -> comma-separated HH:MM Central run times
                        (optional, default '11:00,18:00')
  MAX_EMAIL_ROWS     -> top items shown in the email body (optional, default 40)
  EBAY_CLIENT_ID     -> eBay App ID for live comps (optional; without it the
  EBAY_CLIENT_SECRET    scraper falls back to the title heuristics)
"""

import os
import io
import csv
import time
import base64
import traceback

import requests
from html import escape as html_escape

import sgw_core
import sgw_seen


def central_now(fmt):
    """Railway's clock is UTC; show times in Central for the email."""
    from datetime import datetime
    from zoneinfo import ZoneInfo
    return datetime.now(ZoneInfo("America/Chicago")).strftime(fmt)


RESEND_ENDPOINT = "https://api.resend.com/emails"


def build_csv_bytes(matrix):
    """Turn the row matrix into CSV bytes for attachment."""
    buf = io.StringIO()
    csv.writer(buf).writerows(matrix)
    return buf.getvalue().encode("utf-8")


STATUS_COLOR = {"REVIEW": "#39d353", "BULK": "#a371f7",
                "VERIFY": "#d2a93a", "SKIP": "#8b949e"}
SPEED_COLOR = {"fast": "#39d353", "medium": "#d2a93a",
               "slow": "#f85149", "unknown": "#8b949e"}


def _status_key(status):
    return "VERIFY" if status.startswith("VERIFY") else (
        "SKIP" if status.startswith("SKIP") else status)


def _row_html(w):
    """One dashboard row (same dark layout as the HiBid build)."""
    color = STATUS_COLOR.get(_status_key(w["status"]), "#8b949e")
    est = f"${w['est']:.2f}" if w["est"] else "&#8212;"
    maxbid = f"${w['max_bid']:.2f}" if w["max_bid"] else "&#8212;"
    profit = (f"${w['net_at_current']:.2f}"
              if w["net_at_current"] is not None else "&#8212;")
    profit_color = "#39d353" if (w["net_at_current"] or 0) > 0 else "#8b949e"
    speed_color = SPEED_COLOR.get(w["sell_speed"], "#8b949e")
    speed_badge = (f'<span style="color:{speed_color};font-weight:600;">'
                   f'{w["sell_speed"].upper()}</span>'
                   f'<div style="color:#8b949e;font-size:11px;">'
                   f'{w["comps"]} comps</div>' if w["comps"] else
                   f'<span style="color:#8b949e;">&#8212;</span>'
                   f'<div style="color:#8b949e;font-size:11px;">no comp</div>')
    status_badge = (f'<span style="background:{color};color:#0d1117;'
                    f'font-weight:700;border-radius:4px;padding:1px 6px;'
                    f'font-size:10px;">{_status_key(w["status"])}</span>')
    new_badge = ('<span style="background:#58a6ff;color:#0d1117;font-weight:700;'
                 'border-radius:4px;padding:1px 6px;font-size:10px;'
                 'margin-right:4px;">NEW</span>'
                 if w["is_new"] else
                 '<span style="background:#30363d;color:#8b949e;font-weight:700;'
                 'border-radius:4px;padding:1px 6px;font-size:10px;'
                 'margin-right:4px;">SEEN</span>')
    img = html_escape(w["image_url"] or "")
    photo = (f'<a href="{w["link"]}"><img src="{img}" width="96" height="96" '
             f'alt="" style="display:block;width:96px;height:96px;'
             f'object-fit:cover;border-radius:6px;border:1px solid #30363d;">'
             f'</a>' if w["image_url"] else
             '<div style="width:96px;height:96px;border-radius:6px;'
             'border:1px solid #30363d;color:#8b949e;font-size:10px;'
             'text-align:center;line-height:96px;">no img</div>')
    return f"""
    <tr style="border-bottom:1px solid #21262d;">
      <td style="padding:8px 6px 8px 10px;width:96px;">{photo}</td>
      <td style="padding:8px 10px;">{new_badge}{status_badge}
        <a href="{w['link']}" style="color:#58a6ff;text-decoration:underline;font-weight:700;margin-left:6px;">{html_escape(w['title'][:70])} &#8599;</a>
        <div style="color:#8b949e;font-size:11px;margin-top:3px;"><a href="{w['link']}" style="color:#39d353;text-decoration:none;font-weight:600;">View on SGW &#8594;</a> <span style="margin-left:8px;color:#a5d6ff;">{html_escape(w['lanes'])}</span> <span style="margin-left:8px;">{html_escape(w['note'])}</span></div></td>
      <td style="padding:8px 10px;text-align:right;color:#e6edf3;font-weight:600;">{est}</td>
      <td style="padding:8px 10px;text-align:right;color:#e6edf3;">${w['price']:.2f}<div style="color:#8b949e;font-size:11px;">{w['bids']} bids</div></td>
      <td style="padding:8px 10px;text-align:right;color:#e6edf3;font-weight:600;">{maxbid}</td>
      <td style="padding:8px 10px;text-align:right;color:{profit_color};font-weight:600;">{profit}</td>
      <td style="padding:8px 10px;">{speed_badge}</td>
      <td style="padding:8px 10px;color:#d2a93a;font-weight:600;white-space:nowrap;">{html_escape(w['time_left'])}<div style="color:#8b949e;font-size:11px;">ship ${w['sgw_ship']:.0f}</div></td>
    </tr>"""


def build_html(items, max_rows):
    """Dark dashboard email (same look as the HiBid build): photos,
    NEW/SEEN badges, status + sell-speed badges, SKIP scan list."""
    review = [w for w in items if w["status"] == "REVIEW"]
    bulk = [w for w in items if w["status"] == "BULK"]
    verify = [w for w in items if w["status"].startswith("VERIFY")]
    skip = [w for w in items if w["status"].startswith("SKIP")]
    shown = (review + bulk + verify)[:max_rows]
    n_new = sum(1 for w in review + bulk + verify if w["is_new"])
    now = central_now("%Y-%m-%d %I:%M %p")

    head = f"""<div style="font-family:'IBM Plex Mono',Consolas,monospace;background:#0d1117;color:#e6edf3;padding:18px;border-radius:8px;">
      <h2 style="margin:0 0 4px;color:#39d353;">FlipIntel &#183; SGW NFL Worklist</h2>
      <div style="color:#8b949e;font-size:12px;margin-bottom:14px;">{now} CT &#183; lanes: {html_escape(os.environ.get('LANES', 'nfl'))} &#183; floor ${os.environ.get('NET_FLOOR', '20')} &#183; eBay fee 13%</div>
      <div style="margin-bottom:12px;font-size:13px;"><span style="color:#58a6ff;font-weight:700;">NEW {n_new}</span> &nbsp; <span style="color:#39d353;">REVIEW {len(review)}</span> &nbsp; <span style="color:#a371f7;">BULK {len(bulk)}</span> &nbsp; <span style="color:#d2a93a;">VERIFY {len(verify)}</span> &nbsp; <span style="color:#8b949e;">SKIP {len(skip)}</span></div>"""

    table_open = """<table style="width:100%;border-collapse:collapse;font-size:12px;"><thead><tr style="color:#8b949e;text-align:left;border-bottom:2px solid #30363d;">
          <th style="padding:6px 10px;">Photo</th><th style="padding:6px 10px;">Item</th><th style="padding:6px 10px;text-align:right;">eBay Est</th><th style="padding:6px 10px;text-align:right;">Current</th><th style="padding:6px 10px;text-align:right;">Max Bid</th><th style="padding:6px 10px;text-align:right;">Net @ Cur</th><th style="padding:6px 10px;">Speed</th><th style="padding:6px 10px;">Time Left</th></tr></thead><tbody>"""
    table = table_open + "".join(_row_html(w) for w in shown) + "</tbody></table>"
    more = ""
    if len(review + bulk + verify) > max_rows:
        more = (f'<div style="color:#8b949e;font-size:12px;margin-top:8px;">'
                f'+{len(review + bulk + verify) - max_rows} more in the '
                f'attached CSV</div>')

    skip_section = ""
    if skip:
        rows = []
        for w in skip[:40]:
            rows.append(
                f'<tr style="border-bottom:1px solid #21262d;">'
                f'<td style="padding:5px 10px;width:50%;"><a href="{w["link"]}" '
                f'style="color:#58a6ff;text-decoration:underline;">'
                f'{html_escape(w["title"][:70])} &#8599;</a></td>'
                f'<td style="padding:5px 10px;color:#d2a93a;font-size:12px;'
                f'white-space:nowrap;">{html_escape(w["time_left"])}</td>'
                f'<td style="padding:5px 10px;color:#8b949e;font-size:12px;">'
                f'{html_escape(w["status"])}</td></tr>')
        skip_section = (
            f'<div style="margin-top:18px;color:#8b949e;font-size:13px;'
            f'font-weight:600;border-top:2px solid #30363d;padding-top:10px;">'
            f'SKIPPED ({len(skip)}) &#8212; quick scan so nothing valuable '
            f'slips through</div>'
            f'<table style="width:100%;border-collapse:collapse;font-size:12px;'
            f'margin-top:6px;"><tbody>' + "".join(rows) + '</tbody></table>')

    return head + table + more + skip_section + "</div>"


def send_email(items):
    """Send the worklist via the Resend HTTPS API (Railway blocks SMTP ports).

    ENV VARS:
      RESEND_API_KEY -> your Resend API key (starts with re_)
      EMAIL_TO       -> recipient address
      EMAIL_FROM     -> sender; use 'onboarding@resend.dev' until you verify
                        a domain, or your own verified domain address.
    Returns True only on a delivered send (gates NEW/SEEN marking)."""
    api_key = os.environ["RESEND_API_KEY"]
    to_addr = os.environ["EMAIL_TO"]
    from_addr = os.environ.get("EMAIL_FROM", "FlipIntel SGW <onboarding@resend.dev>")
    max_rows = int(os.environ.get("MAX_EMAIL_ROWS", "40"))

    actionable = [w for w in items if not w["status"].startswith("SKIP")]
    n_new = sum(1 for w in actionable if w["is_new"])
    n_review = sum(1 for w in items if w["status"] == "REVIEW")
    n_bulk = sum(1 for w in items if w["status"] == "BULK")
    n_verify = sum(1 for w in items if w["status"].startswith("VERIFY"))

    html = build_html(items, max_rows)
    csv_b64 = base64.b64encode(
        build_csv_bytes(sgw_core.worklist_to_matrix(items))).decode("ascii")

    subject = (f"SGW NFL Worklist — {n_new} NEW · {n_review} REVIEW"
               f" / {n_bulk} BULK / {n_verify} VERIFY")
    payload = {
        "from": from_addr,
        "to": [to_addr],
        "subject": subject,
        "html": html,
        "attachments": [
            {"filename": "sgw_worklist.csv", "content": csv_b64}
        ],
    }
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }
    r = requests.post(RESEND_ENDPOINT, json=payload, headers=headers, timeout=30)
    if r.status_code in (200, 201):
        print(f"Email sent to {to_addr} ({len(items)} items, {n_new} new). "
              f"id={r.json().get('id')}")
        return True
    print(f"Resend API error {r.status_code}: {r.text[:300]}")
    return False


def run_once():
    print("\n" + "=" * 50)
    print("RUN START", time.strftime("%Y-%m-%d %H:%M:%S"))
    items = sgw_core.build_worklist()
    if send_email(items):
        # Only a delivered report flips items from NEW to SEEN.
        sgw_seen.mark_reported([w["item_id"] for w in items])
    print("RUN DONE", time.strftime("%Y-%m-%d %H:%M:%S"))


RUN_TIMES = os.environ.get("RUN_TIMES", "11:00,18:00")  # Central, HH:MM


def next_run_time():
    """Return (next scheduled run as aware Central datetime, seconds until)."""
    from datetime import datetime, timedelta
    from zoneinfo import ZoneInfo
    tz = ZoneInfo("America/Chicago")
    now = datetime.now(tz)
    slots = []
    for part in RUN_TIMES.split(","):
        h, m = part.strip().split(":")
        slots.append((int(h), int(m)))
    candidates = []
    for d in (0, 1):
        day = now.date() + timedelta(days=d)
        for h, m in slots:
            dt = datetime(day.year, day.month, day.day, h, m, tzinfo=tz)
            if dt > now:
                candidates.append(dt)
    nxt = min(candidates)
    return nxt, (nxt - now).total_seconds()


def main():
    print(f"Email runner started. Runs daily at {RUN_TIMES} Central.")
    # Test switch: RUN_ON_DEPLOY=1 fires one run immediately on startup
    # (then the normal schedule resumes). Remove the variable after testing
    # or every restart/redeploy will send an extra email.
    if os.environ.get("RUN_ON_DEPLOY") == "1":
        print("RUN_ON_DEPLOY=1 -> running once now...")
        try:
            run_once()
        except Exception as e:
            print("ERROR during run:", e)
            traceback.print_exc()
    while True:
        nxt, wait = next_run_time()
        print(f"Next run {nxt.strftime('%a %I:%M %p %Z')} "
              f"(sleeping {wait / 60:.0f} min)...\n")
        time.sleep(max(wait, 1))
        try:
            run_once()
        except Exception as e:
            print("ERROR during run:", e)
            traceback.print_exc()


if __name__ == "__main__":
    main()
