"""Live college football player tracker (QB / RB / WR / TE, every live game).

Polls ESPN's public scoreboard and game summaries, keeps the latest stat line
for every skill-position player, and serves one web page that refreshes itself.

Runs on Railway as a web service (root directory: cfb_live). No API keys.

Environment variables (all optional):
  PORT           -> set by Railway
  POLL_SECONDS   -> how often to re-read live games (default 20)
  GROUPS         -> ESPN group ids, comma separated. 80 = FBS (default), 81 = FCS
  SEASON         -> roster season used to look up positions (default: this year)
"""
from __future__ import annotations

import json
import os
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import requests

HERE = Path(__file__).parent
ESPN = "https://site.api.espn.com/apis/site/v2/sports/football/college-football"
ROSTERS = ("https://github.com/sportsdataverse/sportsdataverse-data/releases/"
           "download/espn_cfb_rosters/cfb_rosters_{season}.parquet")
POLL_SECONDS = int(os.environ.get("POLL_SECONDS", "20"))
GROUPS = [g.strip() for g in os.environ.get("GROUPS", "80").split(",") if g.strip()]
SEASON = int(os.environ.get("SEASON", datetime.now(timezone.utc).year))
SKILL = {"QB": "QB", "RB": "RB", "FB": "RB", "WR": "WR", "TE": "TE"}
# Yardage marks worth a line in the feed when a player crosses them.
MILESTONES = {"pass_yds": (200, 300, 400, 500), "rush_yds": (50, 100, 150, 200),
              "rec_yds": (50, 100, 150, 200)}
STAT_KEYS = ("pass_cmp", "pass_att", "pass_yds", "pass_td", "pass_int",
             "rush_att", "rush_yds", "rush_td", "rush_long",
             "rec", "rec_yds", "rec_td", "rec_long")

session = requests.Session()
session.headers["User-Agent"] = "cfb-live-tracker"

lock = threading.Lock()
state = {"updated": 0, "games": [], "players": [], "feed": [], "error": None}
positions: dict[str, str] = {}     # athlete id -> roster position
prev: dict[tuple, dict] = {}       # (game id, athlete id) -> last stat line
changed: dict[tuple, dict] = {}    # (game id, athlete id) -> {"t": epoch, "delta": {...}}
seen_games: set[str] = set()       # games already read once (so first read is silent)
final_cache: dict[str, list] = {}  # finished games: player lines never change again
feed: list[dict] = []


def load_positions() -> None:
    """Roster positions (QB/RB/WR/TE) by ESPN athlete id. Best effort."""
    try:
        import duckdb
        path = "/tmp/cfb_rosters.parquet"
        r = session.get(ROSTERS.format(season=SEASON), timeout=60)
        r.raise_for_status()
        Path(path).write_bytes(r.content)
        rows = duckdb.sql(
            f"select athlete_id::varchar, any_value(position_abbreviation) "
            f"from '{path}' group by 1").fetchall()
        positions.update({a: p for a, p in rows if a and p})
        print(f"loaded {len(positions)} roster positions for {SEASON}", flush=True)
    except Exception as exc:  # the page still works: positions are inferred
        print(f"roster positions unavailable ({exc}); inferring from stats", flush=True)


def num(text) -> float:
    try:
        return float(text)
    except (TypeError, ValueError):
        return 0.0


def parse_players(game_id: str, summary: dict) -> list[dict]:
    """One merged passing/rushing/receiving line per player in a game."""
    out: dict[str, dict] = {}
    for team in summary.get("boxscore", {}).get("players", []):
        abbr = team.get("team", {}).get("abbreviation", "")
        for cat in team.get("statistics", []):
            name = cat.get("name")
            if name not in ("passing", "rushing", "receiving"):
                continue
            keys = cat.get("keys", [])
            for row in cat.get("athletes", []):
                ath = row.get("athlete", {})
                pid = str(ath.get("id", ""))
                # ESPN adds a team-total row named "Team"; it is not a player.
                if not pid or ath.get("displayName", "").strip().lower() in ("team", ""):
                    continue
                p = out.setdefault(pid, {"id": pid, "gid": game_id, "team": abbr,
                                         "name": ath.get("displayName", "?"),
                                         **{k: 0 for k in STAT_KEYS}})
                s = dict(zip(keys, row.get("stats", [])))
                if name == "passing":
                    cmp_, _, att = str(s.get("completions/passingAttempts", "0/0")).partition("/")
                    p.update(pass_cmp=num(cmp_), pass_att=num(att),
                             pass_yds=num(s.get("passingYards")),
                             pass_td=num(s.get("passingTouchdowns")),
                             pass_int=num(s.get("interceptions")))
                elif name == "rushing":
                    p.update(rush_att=num(s.get("rushingAttempts")),
                             rush_yds=num(s.get("rushingYards")),
                             rush_td=num(s.get("rushingTouchdowns")),
                             rush_long=num(s.get("longRushing")))
                else:
                    p.update(rec=num(s.get("receptions")),
                             rec_yds=num(s.get("receivingYards")),
                             rec_td=num(s.get("receivingTouchdowns")),
                             rec_long=num(s.get("longReception")))
    players = []
    for p in out.values():
        pos = positions.get(p["id"])
        if pos is None:  # not on the roster file: infer from what they did
            if p["pass_att"] >= 3 and p["pass_att"] >= p["rush_att"]:
                pos = "QB"
            elif p["rush_att"] > p["rec"]:
                pos = "RB"
            else:
                pos = "WR"
            p["pos_guess"] = True
        if pos in SKILL:
            p["pos"] = SKILL[pos]
            players.append(p)
    return players


def fetch_summary(game_id: str) -> list[dict]:
    r = session.get(f"{ESPN}/summary", params={"event": game_id}, timeout=20)
    r.raise_for_status()
    return parse_players(game_id, r.json())


def fetch_scoreboard() -> list[dict]:
    games = {}
    for group in GROUPS:
        r = session.get(f"{ESPN}/scoreboard", params={"groups": group, "limit": 400}, timeout=20)
        r.raise_for_status()
        for ev in r.json().get("events", []):
            comp = ev["competitions"][0]
            st = ev["status"]
            sides = {}
            for c in comp["competitors"]:
                t = c["team"]
                sides[c["homeAway"]] = {"abbr": t.get("abbreviation", ""),
                                        "name": t.get("shortDisplayName", ""),
                                        "score": c.get("score", ""),
                                        "rank": (c.get("curatedRank") or {}).get("current", 99)}
            sit = comp.get("situation") or {}
            games[ev["id"]] = {
                "id": ev["id"], "state": st["type"]["state"], "date": ev["date"],
                "period": st.get("period", 0), "clock": st.get("displayClock", ""),
                "detail": st["type"].get("shortDetail", ""),
                "home": sides.get("home", {}), "away": sides.get("away", {}),
                "down": sit.get("downDistanceText", ""), "last": (sit.get("lastPlay") or {}).get("text", ""),
            }
    return list(games.values())


def note_changes(game: dict, players: list[dict], quiet: bool) -> None:
    """Compare with the last read; remember what moved and add feed lines."""
    now = time.time()
    label = f"{game['away'].get('abbr')} @ {game['home'].get('abbr')}"
    for p in players:
        key = (p["gid"], p["id"])
        old = prev.get(key)
        line = {k: p[k] for k in STAT_KEYS}
        if old is not None and old != line:
            delta = {k: line[k] - old[k] for k in STAT_KEYS if line[k] != old[k]}
            changed[key] = {"t": now, "delta": delta}
        if not quiet:
            old = old or {k: 0 for k in STAT_KEYS}
            who = f"{p['name']} ({p['pos']}, {p['team']})"
            for k, word in (("pass_td", "passing TD"), ("rush_td", "rushing TD"), ("rec_td", "receiving TD")):
                if line[k] > old[k]:
                    feed.append({"t": now, "gid": p["gid"], "pid": p["id"], "kind": "td",
                                 "text": f"{who} {word} (now {int(line[k])})", "game": label})
            for k, marks in MILESTONES.items():
                for m in marks:
                    if old[k] < m <= line[k]:
                        what = {"pass_yds": "passing", "rush_yds": "rushing", "rec_yds": "receiving"}[k]
                        feed.append({"t": now, "gid": p["gid"], "pid": p["id"], "kind": "mark",
                                     "text": f"{who} passes {m} {what} yards ({int(line[k])})", "game": label})
        prev[key] = line
        c = changed.get(key)
        if c:
            p["upd"] = c["t"]
            p["delta"] = c["delta"]
    del feed[:-200]


def poll_once() -> None:
    games = fetch_scoreboard()
    live = [g for g in games if g["state"] == "in"]
    done = [g for g in games if g["state"] == "post" and g["id"] not in final_cache]
    by_id = {g["id"]: g for g in games}
    players: list[dict] = []
    with ThreadPoolExecutor(max_workers=8) as pool:
        wanted = live + done
        results = pool.map(lambda g: (g, _safe_summary(g["id"])), wanted)
        for game, rows in results:
            if rows is None:
                continue
            note_changes(game, rows, quiet=game["id"] not in seen_games)
            seen_games.add(game["id"])
            if game["state"] == "post":
                final_cache[game["id"]] = rows
            else:
                players.extend(rows)
    for gid, rows in final_cache.items():
        if gid in by_id:
            players.extend(rows)
    with lock:
        state.update(updated=time.time(), games=games, players=players,
                     feed=list(reversed(feed[-60:])), error=None)


def _safe_summary(game_id: str):
    try:
        return fetch_summary(game_id)
    except Exception as exc:
        print(f"summary {game_id} failed: {exc}", flush=True)
        return None


def poll_forever() -> None:
    load_positions()
    while True:
        started = time.time()
        try:
            poll_once()
        except Exception as exc:
            print(f"poll failed: {exc}", flush=True)
            with lock:
                state["error"] = str(exc)
        # Nothing live: check back less often.
        with lock:
            any_live = any(g["state"] == "in" for g in state["games"])
        wait = POLL_SECONDS if any_live else max(POLL_SECONDS, 60)
        time.sleep(max(1.0, wait - (time.time() - started)))


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        path = self.path.split("?")[0]
        if path == "/api/state":
            with lock:
                body = json.dumps({**state, "now": time.time(), "poll": POLL_SECONDS}).encode()
            self._send(200, "application/json", body)
        elif path in ("/", "/index.html"):
            self._send(200, "text/html; charset=utf-8", (HERE / "index.html").read_bytes())
        elif path == "/healthz":
            self._send(200, "text/plain", b"ok")
        else:
            self._send(404, "text/plain", b"not found")

    def _send(self, code, ctype, body):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass


if __name__ == "__main__":
    threading.Thread(target=poll_forever, daemon=True).start()
    port = int(os.environ.get("PORT", "8080"))
    print(f"cfb live tracker on :{port}, polling every {POLL_SECONDS}s", flush=True)
    ThreadingHTTPServer(("0.0.0.0", port), Handler).serve_forever()
