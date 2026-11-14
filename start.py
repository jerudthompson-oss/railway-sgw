"""Railway entry point for every service built from this repo's root.

A service that has the SGW email key (RESEND_API_KEY) runs the SGW email
worker exactly as before. A service without it runs the live college football
tracker in cfb_live/, so the tracker needs no Root Directory setting.
Set APP=sgw or APP=cfb_live on a service to choose explicitly.
"""
import os
import sys
from pathlib import Path

ROOT = Path(__file__).parent
app = os.environ.get("APP", "").strip().lower()
if app not in ("sgw", "cfb_live"):
    app = "sgw" if os.environ.get("RESEND_API_KEY") else "cfb_live"

script = ROOT / ("main.py" if app == "sgw" else "cfb_live/app.py")
print(f"start.py: launching {app} ({script.relative_to(ROOT)})", flush=True)
os.execv(sys.executable, [sys.executable, str(script)])
