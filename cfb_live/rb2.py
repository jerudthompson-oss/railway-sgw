"""RB2 first-quarter alerts.

Research (cfb_research/q1_*.py, 2024-2026 play-by-play): when a team's RB2 has
a big first quarter, he keeps running well for the rest of the game, about 25%
above his norm. It is stronger for steady (non-explosive) backs, against poor
run defenses, in close games, and when the yards came on volume rather than
one long run. It mostly disappears when his team leads by 14+.

This module:
  * loads each team's RB depth (RB1/RB2 by season carries), each back's
    per-game norm and explosive-play rate, and every defense's yards per carry
    allowed, from free sportsdataverse files (refreshed every 12 hours);
  * snapshots every RB2's rushing line through the end of the first quarter;
  * raises one graded alert per qualifying RB2 when the second quarter starts.
"""
from __future__ import annotations

import os
import time
from pathlib import Path

BASE = "https://github.com/sportsdataverse/sportsdataverse-data/releases/download"
USAGE = BASE + "/espn_cfb_usage_players/usage_players_{season}.parquet"
TEAM_BOX = BASE + "/espn_cfb_team_box/team_box_{season}.parquet"

Q1_MIN_YDS = float(os.environ.get("RB2_Q1_YARDS", "30"))  # alert threshold
REST_SHARE = 0.76    # share of an RB2's game rushing yards that come in Q2-Q4
ONE_PLAY = 0.6       # longest run >= 60% of Q1 yards = "mostly one play"

roles: dict[str, dict] = {}       # athlete id -> {"role", "team_id", "rush_pg", "car_pg", "explosive"}
defense: dict[str, dict] = {}     # team id -> {"ypc", "tier", "rank", "teams"}
loaded_at = 0.0
q1_latest: dict[str, dict] = {}   # game id -> {athlete id: rush line} while game is in Q1
q1_final: dict[str, dict] = {}    # game id -> frozen Q1 lines
alerts: list[dict] = []


def load_context(session, season: int) -> None:
    """Depth roles, norms, explosiveness and run defense. Best effort."""
    global loaded_at
    try:
        import duckdb
        files = {}
        for name, url in (("usage", USAGE), ("team_box", TEAM_BOX)):
            path = f"/tmp/cfb_{name}_{season}.parquet"
            r = session.get(url.format(season=season), timeout=120)
            r.raise_for_status()
            Path(path).write_bytes(r.content)
            files[name] = path
        con = duckdb.connect()
        rows = con.execute(f"""
            with rb as (
              select player_id::varchar pid, pos_team_id::varchar team_id, rushes, rush_yards, games,
                     explosive_rate, touches,
                     row_number() over (partition by pos_team_id order by rushes desc, player_id) depth
              from '{files["usage"]}' where position_group = 'RB' and games > 0
            ), rb2 as (
              select quantile_cont(explosive_rate, 2/3) cut from rb where depth = 2 and touches >= 15
            )
            select pid, team_id, depth, rush_yards / games, rushes / games,
                   case when touches >= 15 and explosive_rate >= (select cut from rb2) then 1
                        when touches >= 15 then 0 end
            from rb where depth <= 3
        """).fetchall()
        new_roles = {pid: {"role": f"RB{depth}", "team_id": team, "rush_pg": round(ypg or 0, 1),
                           "car_pg": round(cpg or 0, 1),
                           "explosive": None if expl is None else bool(expl)}
                     for pid, team, depth, ypg, cpg, expl in rows}
        drows = con.execute(f"""
            with g as (
              select game_id, team_id::varchar team_id,
                     try_cast(rushingYards as double) ry, try_cast(rushingAttempts as double) ra
              from '{files["team_box"]}'
            ), allowed as (
              select a.team_id, sum(b.ry) / nullif(sum(b.ra), 0) ypc, count(*) games
              from g a join g b on a.game_id = b.game_id and a.team_id <> b.team_id
              group by a.team_id
            )
            select team_id, ypc, games from allowed where games >= 4 and ypc is not null
        """).fetchall()
        ranked = sorted(drows, key=lambda r: -r[1])  # worst (most allowed) first
        n = len(ranked)
        new_def = {}
        for i, (team, ypc, _) in enumerate(ranked):
            tier = "poor" if i < n / 3 else ("avg" if i < 2 * n / 3 else "good")
            new_def[team] = {"ypc": round(ypc, 2), "tier": tier, "rank": i + 1, "teams": n}
        roles.clear(); roles.update(new_roles)
        defense.clear(); defense.update(new_def)
        loaded_at = time.time()
        print(f"rb2: {sum(1 for r in roles.values() if r['role'] == 'RB2')} RB2s, "
              f"{len(defense)} run defenses rated", flush=True)
    except Exception as exc:
        print(f"rb2: context unavailable ({exc}); RB2 alerts paused", flush=True)


def role_of(pid: str) -> str | None:
    r = roles.get(pid)
    return r["role"] if r else None


def observe(game: dict, players: list[dict]) -> None:
    """Call every poll for every live game with its parsed player lines."""
    gid = game["id"]
    if game["state"] != "in" or not roles:
        return
    if game["period"] == 1:
        q1_latest[gid] = {p["id"]: {"rush_att": p["rush_att"], "rush_yds": p["rush_yds"],
                                    "rush_long": p["rush_long"], "team_id": p.get("team_id")}
                          for p in players if p.get("pos") == "RB"}
        q1_latest[gid]["_score"] = {game["home"].get("id"): num(game["home"].get("score")),
                                    game["away"].get("id"): num(game["away"].get("score"))}
        return
    if gid in q1_final or gid not in q1_latest:
        return  # already evaluated, or we never saw this game's first quarter
    q1_final[gid] = q1_latest.pop(gid)
    evaluate(game, q1_final[gid], players)


def num(x) -> float:
    try:
        return float(x)
    except (TypeError, ValueError):
        return 0.0


def evaluate(game: dict, q1: dict, players: list[dict]) -> None:
    score = q1.get("_score", {})
    names = {p["id"]: p for p in players}
    for pid, line in q1.items():
        if pid.startswith("_"):
            continue
        info = roles.get(pid)
        if not info or info["role"] != "RB2" or line["rush_yds"] < Q1_MIN_YDS:
            continue
        team = line.get("team_id") or info["team_id"]
        opp = next((s.get("id") for s in (game["home"], game["away"]) if s.get("id") != team), None)
        lead = score.get(team, 0) - score.get(opp, 0)
        d = defense.get(opp or "", {})
        steady = info["explosive"] is False
        explosive = info["explosive"] is True
        one_play = line["rush_long"] >= ONE_PLAY * line["rush_yds"]
        poor_d = d.get("tier") == "poor"
        close = abs(lead) <= 13

        # Rest-of-game boost over his norm, from the 2024-2026 study.
        if explosive:
            boost = 0.07
        elif steady and poor_d:
            boost = 0.48
        elif steady:
            boost = 0.38
        elif poor_d:
            boost = 0.35
        else:
            boost = 0.25
        if one_play:
            boost = min(boost, 0.19)
        if lead >= 14:
            boost = min(boost, 0.13)
        if lead <= -14:
            boost = min(boost, 0.0)

        good = [steady, poor_d, close, not one_play]
        score_n = sum(good)
        grade = "strong" if (score_n >= 3 and not explosive and lead < 14) else (
            "moderate" if score_n >= 2 and boost >= 0.19 else "weak")
        norm_rest = info["rush_pg"] * REST_SHARE
        proj_rest = norm_rest * (1 + boost)
        p = names.get(pid, {})
        reasons = []
        reasons.append("steady back" if steady else "explosive back" if explosive else "few carries so far")
        if d:
            reasons.append(f"{d['tier']} run D ({d['ypc']:.1f} ypc allowed, #{d['rank']} of {d['teams']} worst)")
        reasons.append(f"{'leads' if lead > 0 else 'trails' if lead < 0 else 'tied'}"
                       f"{' by ' + str(int(abs(lead))) if lead else ''} after Q1")
        reasons.append("yards mostly one run" if one_play else "yards on volume")
        alerts.append({
            "id": f"{game['id']}-{pid}", "t": time.time(), "gid": game["id"], "pid": pid,
            "name": p.get("name", pid), "team": p.get("team", ""),
            "game": f"{game['away'].get('abbr')} @ {game['home'].get('abbr')}",
            "grade": grade, "q1_car": int(line["rush_att"]), "q1_yds": int(line["rush_yds"]),
            "q1_long": int(line["rush_long"]),
            "norm_game": info["rush_pg"], "norm_rest": round(norm_rest, 1),
            "boost": round(boost * 100), "proj_rest": round(proj_rest, 1),
            "proj_game": round(line["rush_yds"] + proj_rest, 1),
            "reasons": reasons,
        })
    del alerts[:-100]
