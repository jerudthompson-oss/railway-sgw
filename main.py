"""
main.py - Cloud runner: hourly ShopGoodwill scrape -> emailed to you.

Runs on Railway. Every RUN_INTERVAL_MIN minutes it:
  1. Scrapes ShopGoodwill (pages/window/lanes from sgw_core.py)
  2. Builds the combed, flagged, value-ranked worklist
  3. Emails you a phone-readable summary + attaches the full CSV

No Google Cloud / service-account keys needed (those are blocked on
personal accounts). Uses plain SMTP with a Gmail App Password.

ENV VARS (set in Railway, never in code):
  GMAIL_ADDRESS      -> your gmail, e.g. you@gmail.com  (the sender)
  GMAIL_APP_PASSWORD -> 16-char App Password (NOT your normal password)
  EMAIL_TO           -> where to send (can be same gmail or another address)
  RUN_INTERVAL_MIN   -> minutes between runs (optional, default 60)
  MAX_EMAIL_ROWS     -> how many top items in the email body (optional, default 40)
"""

import os
import io
import csv
import time
import smtplib
import traceback
from email.message import EmailMessage

import sgw_core


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
    sender = os.environ["GMAIL_ADDRESS"]
    app_pw = os.environ["GMAIL_APP_PASSWORD"]
    to_addr = os.environ.get("EMAIL_TO", sender)
    max_rows = int(os.environ.get("MAX_EMAIL_ROWS", "40"))

    n = len(matrix) - 1
    msg = EmailMessage()
    msg["Subject"] = f"SGW worklist: {n} items ({time.strftime('%I:%M %p')})"
    msg["From"] = sender
    msg["To"] = to_addr
    msg.set_content("HTML email - open in an HTML-capable client.")
    msg.add_alternative(build_html(matrix, max_rows), subtype="html")
    msg.add_attachment(build_csv_bytes(matrix), maintype="text",
                       subtype="csv", filename="bid_calculator.csv")

    with smtplib.SMTP_SSL("smtp.gmail.com", 465) as s:
        s.login(sender, app_pw)
        s.send_message(msg)
    print(f"Email sent to {to_addr} ({n} items).")


def run_once():
    print("\n" + "=" * 50)
    print("RUN START", time.strftime("%Y-%m-%d %H:%M:%S"))
    matrix = sgw_core.build_calculator_rows()
    send_email(matrix)
    print("RUN DONE", time.strftime("%Y-%m-%d %H:%M:%S"))


def main():
    interval_min = int(os.environ.get("RUN_INTERVAL_MIN", "60"))
    print(f"Email runner started. Interval: {interval_min} min.")
    while True:
        try:
            run_once()
        except Exception as e:
            print("ERROR during run:", e)
            traceback.print_exc()
        print(f"Sleeping {interval_min} min...\n")
        time.sleep(interval_min * 60)


if __name__ == "__main__":
    main()
