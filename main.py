"""
main.py - Cloud runner: hourly ShopGoodwill scrape -> emailed to you.

Runs on Railway. Every RUN_INTERVAL_MIN minutes it:
  1. Scrapes ShopGoodwill (pages/window/lanes from sgw_core.py)
  2. Builds the combed, flagged, value-ranked worklist
  3. Emails you a phone-readable summary + attaches the full CSV

No Google Cloud / SMTP needed. Sends via the Resend HTTPS API because
Railway blocks outbound SMTP ports (465/587).

ENV VARS (set in Railway, never in code):
  RESEND_API_KEY   -> your Resend API key (starts with re_)
  EMAIL_TO         -> where to send the worklist
  EMAIL_FROM       -> sender; 'onboarding@resend.dev' works out of the box
                      (optional, that's the default)
  RUN_INTERVAL_MIN -> minutes between runs (optional, default 60)
  MAX_EMAIL_ROWS   -> top items shown in the email body (optional, default 40)
"""

import os
import io
import csv
import time
import base64
import traceback

import requests

import sgw_core


RESEND_ENDPOINT = "https://api.resend.com/emails"


def build_csv_bytes(matrix):
    """Turn the row matrix into CSV bytes for attachment."""
    buf = io.StringIO()
    csv.writer(buf).writerows(matrix)
    return buf.getvalue().encode("utf-8")


def build_html(matrix, max_rows):
    """Phone-readable HTML summary of the top items.
    matrix[0] is the header; columns (0-indexed):
      0 Item 1 Lanes 2 Status 3 Bids 4 CurBid 5 Time 6 SGWship
      7 EstSold 8 Conf 9 Note 10 Comp 11 Outbound 12 MaxBid(formula)
      13 ProjNet(formula) 14 Clears(formula) 15 Link
    We show plain columns (skip the formula cols, which are empty until
    a comp is pasted) and compute a simple estimated max bid for display.
    """
    rows = matrix[1:]
    total = len(rows)
    review = [r for r in rows if r[2] == "REVIEW"]
    verify = [r for r in rows if str(r[2]).startswith("VERIFY")]

    def est_maxbid(r):
        # est sold(7) - 13% - sgw ship(6) - outbound(11) - $20 floor
        try:
            est = float(r[7]); ship = float(r[6] or 0); ob = float(r[11] or 0)
            return round(est - est * 0.13 - ship - ob - 20, 2)
        except Exception:
            return ""

    def row_html(r):
        mb = est_maxbid(r)
        mb_txt = f"${mb}" if mb != "" else "?"
        link = r[15]
        return (
            f"<tr>"
            f"<td style='padding:6px 8px;border-bottom:1px solid #eee'>"
            f"<a href='{link}' style='color:#0a7'>{r[0][:70]}</a><br>"
            f"<span style='color:#888;font-size:12px'>{r[1]} &middot; {r[9]}</span></td>"
            f"<td style='padding:6px 8px;border-bottom:1px solid #eee;text-align:center'>{r[3]}</td>"
            f"<td style='padding:6px 8px;border-bottom:1px solid #eee;text-align:right'>${r[4]}</td>"
            f"<td style='padding:6px 8px;border-bottom:1px solid #eee;text-align:right'>${r[7]}</td>"
            f"<td style='padding:6px 8px;border-bottom:1px solid #eee;text-align:right;font-weight:bold'>{mb_txt}</td>"
            f"<td style='padding:6px 8px;border-bottom:1px solid #eee;text-align:right;color:#c00'>{r[5]}</td>"
            f"</tr>"
        )

    def table(title, subset):
        if not subset:
            return f"<h3>{title}</h3><p style='color:#888'>none this run</p>"
        head = (
            "<tr style='background:#f4f4f4'>"
            "<th style='padding:6px 8px;text-align:left'>Item</th>"
            "<th style='padding:6px 8px'>Bids</th>"
            "<th style='padding:6px 8px'>Cur</th>"
            "<th style='padding:6px 8px'>Est</th>"
            "<th style='padding:6px 8px'>MaxBid</th>"
            "<th style='padding:6px 8px'>Time</th></tr>"
        )
        body = "".join(row_html(r) for r in subset[:max_rows])
        return (f"<h3>{title} ({len(subset)})</h3>"
                f"<table style='border-collapse:collapse;width:100%;font-size:14px'>"
                f"{head}{body}</table>")

    when = time.strftime("%a %b %d, %I:%M %p")
    html = (
        f"<div style='font-family:sans-serif;max-width:680px'>"
        f"<h2>ShopGoodwill worklist &mdash; {when}</h2>"
        f"<p style='color:#555'>{total} contested items &middot; "
        f"{len(review)} REVIEW &middot; {len(verify)} VERIFY. "
        f"Est MaxBid = most to pay and still net $20 (verify on Terapeak before bidding).</p>"
        f"{table('REVIEW &mdash; estimator confident', review)}"
        f"{table('VERIFY &mdash; eyeball these, value uncertain', verify)}"
        f"<p style='color:#888;font-size:12px'>Full list attached as CSV. "
        f"Estimates are conservative guesses, not real comps.</p>"
        f"</div>"
    )
    return html


def send_email(matrix):
    """Send the worklist via the Resend HTTPS API (Railway blocks SMTP ports).

    ENV VARS:
      RESEND_API_KEY -> your Resend API key (starts with re_)
      EMAIL_TO       -> recipient address
      EMAIL_FROM     -> sender; use 'onboarding@resend.dev' until you verify
                        a domain, or your own verified domain address.
    """
    api_key = os.environ["RESEND_API_KEY"]
    to_addr = os.environ["EMAIL_TO"]
    from_addr = os.environ.get("EMAIL_FROM", "onboarding@resend.dev")
    max_rows = int(os.environ.get("MAX_EMAIL_ROWS", "40"))

    n = len(matrix) - 1
    html = build_html(matrix, max_rows)
    csv_b64 = base64.b64encode(build_csv_bytes(matrix)).decode("ascii")

    payload = {
        "from": from_addr,
        "to": [to_addr],
        "subject": f"SGW worklist: {n} items ({time.strftime('%I:%M %p')})",
        "html": html,
        "attachments": [
            {"filename": "bid_calculator.csv", "content": csv_b64}
        ],
    }
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }
    r = requests.post(RESEND_ENDPOINT, json=payload, headers=headers, timeout=30)
    if r.status_code in (200, 201):
        print(f"Email sent to {to_addr} ({n} items). id={r.json().get('id')}")
    else:
        print(f"Resend API error {r.status_code}: {r.text[:300]}")


def run_once():
    print("\n" + "=" * 50)
    print("RUN START", time.strftime("%Y-%m-%d %H:%M:%S"))
    matrix = sgw_core.build_calculator_rows()
    send_email(matrix)
    print("RUN DONE", time.strftime("%Y-%m-%d %H:%M:%S"))


def next_interval_minutes():
    """Pick the wait (minutes) based on current CENTRAL time + day:
      Midnight-8AM   -> 180 min (overnight, quiet)
      8AM-5PM        -> 60 min  (daytime)
      5PM-Midnight   -> 30 min  (evening rush)
        ...but Fri & Sat 5PM-Midnight -> 15 min (weekend peak closings)
    """
    from datetime import datetime
    from zoneinfo import ZoneInfo
    now = datetime.now(ZoneInfo("America/Chicago"))
    hour = now.hour
    weekday = now.weekday()        # Mon=0 ... Fri=4, Sat=5, Sun=6
    if hour < 8:                   # overnight
        return 180
    elif hour < 17:                # daytime
        return 60
    else:                          # 5PM-midnight evening rush
        if weekday in (4, 5):      # Friday or Saturday evening
            return 15
        return 30


def main():
    print("Email runner started. Schedule (Central): "
          "overnight=3h, day=1h, 5pm-midnight=30min.")
    while True:
        try:
            run_once()
        except Exception as e:
            print("ERROR during run:", e)
            traceback.print_exc()
        wait = next_interval_minutes()
        print(f"Sleeping {wait} min until next run...\n")
        time.sleep(wait * 60)


if __name__ == "__main__":
    main()
