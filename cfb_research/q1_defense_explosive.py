"""Q1 carryover by depth role, split by opponent defense quality and by player type.

    python q1_defense_explosive.py DATA_DIR [SEASON ...]     (default 2026)

Opponent defense: yards per carry allowed (RBs) or yards per pass attempt
allowed (WRs/TEs), computed from the defense's OTHER games (leave-one-out).
"Poor defense" = worst third of defenses faced.

Explosive player: share of his touches that went 15+ (runs) or 20+ (catches),
from his OTHER games. "Explosive" = top third within his role.

Big Q1 = his top 15% of first quarters relative to his own norm, within role.
"""
import sys
import duckdb
import numpy as np

DATA = sys.argv[1] if len(sys.argv) > 1 else "."
SEASONS = tuple(int(a) for a in sys.argv[2:]) or (2026,)
con = duckdb.connect()

con.execute("create table p as " + " union all ".join(f"""
  select {y} season, game_id, period, pos_team, def_pos_team, yards_gained, play_type, rush,
    pass_attempt, rush_player_id, reception_player_id, target_player_id,
    pos_team_score, def_pos_team_score, game_play_number
  from '{DATA}/pbp_{y}.parquet' where period between 1 and 4""" for y in SEASONS))
con.execute("create table ros as " + " union all ".join(
    f"select {y} season, try_cast(athlete_id as bigint) pid, any_value(position_abbreviation) pos "
    f"from '{DATA}/ros_{y}.parquet' group by 2" for y in SEASONS))

# One row per touch/target for RBs (runs) and WRs/TEs (targets).
con.execute("""
create table plays as
select p.season, p.game_id, p.period, p.pos_team team, p.def_pos_team opp,
  try_cast(p.rush_player_id as bigint) pid, 'RB' grp, p.yards_gained yds, 1 opp_n,
  (p.yards_gained >= 15)::int expl, 1 touch
from p where p.rush and p.rush_player_id is not null
union all
select p.season, p.game_id, p.period, p.pos_team, p.def_pos_team,
  try_cast(coalesce(p.reception_player_id, p.target_player_id) as bigint), 'REC',
  case when p.play_type in ('Pass Reception','Passing Touchdown') then p.yards_gained else 0 end, 1,
  (p.play_type in ('Pass Reception','Passing Touchdown') and p.yards_gained >= 20)::int,
  (p.play_type in ('Pass Reception','Passing Touchdown'))::int
from p where coalesce(p.reception_player_id, p.target_player_id) is not null
""")

# Defense quality per game, leave-one-out over the defense's other games.
con.execute("""
create table def_game as
with g as (
  select season, game_id, def_pos_team d,
    sum(yards_gained) filter (rush) ry, count(*) filter (rush) rn,
    sum(case when play_type in ('Pass Reception','Passing Touchdown') then yards_gained else 0 end)
      filter (pass_attempt) py, count(*) filter (pass_attempt) pn
  from p where def_pos_team is not null group by all
), t as (
  select *, sum(ry) over w try, sum(rn) over w trn, sum(py) over w tpy, sum(pn) over w tpn,
    count(*) over w gms
  from g window w as (partition by season, d)
)
select season, game_id, d opp, gms - 1 other_games,
  (try - ry) / nullif(trn - rn, 0) ypc_allowed, (tpy - py) / nullif(tpn - pn, 0) ypa_allowed
from t
""")

con.execute("""
create table pg as
select pl.season, pl.game_id, any_value(pl.team) team, any_value(pl.opp) opp, pl.pid, r.pos,
  sum(yds) filter (period = 1) q1, sum(yds) filter (period > 1) rest,
  count(*) filter (period = 1) q1o, count(*) filter (period > 1) resto,
  max(yds) filter (period = 1) q1_long,
  sum(expl) expl, sum(touch) touches
from plays pl join ros r on r.season = pl.season and r.pid = pl.pid
where (r.pos = 'RB' and grp = 'RB') or (r.pos in ('WR','TE') and grp = 'REC')
group by pl.season, pl.game_id, pl.pid, r.pos
""")

con.execute("""
create table g as
with x as (
  select *, coalesce(q1,0) q1y, coalesce(rest,0) resty, coalesce(q1o,0) q1oy, coalesce(resto,0) restoy,
    count(*) over w n, sum(coalesce(q1,0)) over w sq1, sum(coalesce(rest,0)) over w srest,
    sum(coalesce(q1o,0)) over w sq1o, sum(coalesce(resto,0)) over w sresto,
    sum(expl) over w sexpl, sum(touches) over w stouch
  from pg window w as (partition by season, pid)
)
select season, game_id, team, opp, pid, pos, q1y q1, resty rest, q1oy q1o, restoy resto, q1_long,
  (sq1 - q1y) / (n - 1) base_q1, (srest - resty) / (n - 1) base_rest,
  (sresto - restoy) / (n - 1) base_resto,
  (sexpl - expl) / nullif(stouch - touches, 0) expl_rate, stouch - touches other_touches,
  sq1o + sresto season_opp
from x where n - 1 >= 3
""")

# Team lead at end of Q1, both teams.
con.execute("""
create table q1score as
with last as (
  select season, game_id, pos_team, def_pos_team, pos_team_score - def_pos_team_score lead,
    row_number() over (partition by season, game_id order by game_play_number desc) rn
  from p where period = 1 and pos_team_score is not null
)
select season, game_id, pos_team team, lead from last where rn = 1
union all select season, game_id, def_pos_team, -lead from last where rn = 1
""")

con.execute("""
create table r as
with pl as (select season, team, pid, pos, any_value(season_opp) o from g group by all),
rk as (select *, pos || row_number() over (partition by season, team, pos order by o desc, pid) role from pl)
select g.*, rk.role, s.lead,
  case when g.pos = 'RB' then d.ypc_allowed else d.ypa_allowed end def_allowed, d.other_games def_games
from g join rk using (season, team, pid, pos)
left join def_game d on d.season = g.season and d.game_id = g.game_id and d.opp = g.opp
left join q1score s on s.season = g.season and s.game_id = g.game_id and s.team = g.team
""")

df_all = con.sql("select * from r where role in ('RB1','RB2','WR1','WR2','WR3','TE1','TE2')").df()
df_all = df_all[df_all.def_games >= 3]  # opponent has enough other games to rate (drops most FCS)

def tag_tiers(df):
    df = df.copy()
    # Poor defense: worst third of defenses faced at that position group.
    for grp in ("RB", "REC"):
        m = (df.pos == "RB") if grp == "RB" else (df.pos != "RB")
        cut = df.loc[m, "def_allowed"].quantile(2 / 3)
        good = df.loc[m, "def_allowed"].quantile(1 / 3)
        df.loc[m, "defense"] = np.where(df.loc[m, "def_allowed"] >= cut, "poor",
                                np.where(df.loc[m, "def_allowed"] <= good, "good", "avg"))
    # Explosive player: top third of explosive rate within role (min 15 other touches).
    df["ptype"] = "steady"
    for role in df.role.unique():
        m = (df.role == role) & (df.other_touches >= 15)
        cut = df.loc[m, "expl_rate"].quantile(2 / 3)
        df.loc[m & (df.expl_rate >= cut), "ptype"] = "explosive"
        df.loc[(df.role == role) & (df.other_touches < 15), "ptype"] = "few touches"
    df["dq1"] = df.q1 - df.base_q1
    df["dr"] = df.rest - df.base_rest
    df["dro"] = df.resto - df.base_resto
    df["big"] = False
    for role in df.role.unique():
        m = df.role == role
        df.loc[m, "big"] = df.loc[m, "dq1"] >= df.loc[m, "dq1"].quantile(0.85)
    # Was the big Q1 mostly one play?
    df["one_play"] = df.q1_long >= 0.6 * df.q1
    return df

df = tag_tiers(df_all)
rng = np.random.default_rng(7)

def slope(sub):
    if len(sub) < 40:
        return "      -      "
    x, y, pid = sub.dq1.to_numpy(), sub.dr.to_numpy(), sub.pid.to_numpy()
    ids = np.unique(pid); idx = {i: np.where(pid == i)[0] for i in ids}
    bs = []
    for _ in range(400):
        t = np.concatenate([idx[i] for i in rng.choice(ids, len(ids))])
        bs.append(np.polyfit(x[t], y[t], 1)[0])
    lo, hi = np.percentile(bs, [2.5, 97.5])
    star = "*" if lo > 0 or hi < 0 else " "
    return f"{np.polyfit(x, y, 1)[0]:+.2f} [{lo:+.2f},{hi:+.2f}]{star}"

def big_line(sub):
    b = sub[sub.big]
    if len(b) < 12:
        return f"n={len(b):<4}    -"
    pct = (b.dr.mean() / b.base_rest.mean()) if b.base_rest.mean() else 0
    return (f"n={len(b):<4} {b.dr.mean():+6.1f} ({pct:+4.0%})  opp {b.dro.mean():+5.2f}  "
            f"beat norm {(b.rest > b.base_rest).mean():4.0%} (all {(sub.rest > sub.base_rest).mean():3.0%})")

print(f"Seasons {SEASONS}. Slope = rest-of-game yards per Q1 yard above norm, [95% CI], * = clearly non-zero.")
print("Big-Q1 line: rest of game vs his norm (yards and %), touches vs norm, how often he beat his norm.\n")
for role in ["RB1", "RB2", "WR1", "WR2", "WR3", "TE1", "TE2"]:
    rd = df[df.role == role]
    print(f"===== {role}  ({len(rd)} games, norm rest {rd.base_rest.mean():.1f} yds) =====")
    print(f"  {'all games':<26} slope {slope(rd)}   big Q1 {big_line(rd)}")
    for lab, m in (("vs POOR defense", rd.defense == "poor"), ("vs average defense", rd.defense == "avg"),
                   ("vs GOOD defense", rd.defense == "good")):
        print(f"  {lab:<26} slope {slope(rd[m])}   big Q1 {big_line(rd[m])}")
    for lab, m in (("EXPLOSIVE player", rd.ptype == "explosive"), ("steady player", rd.ptype == "steady")):
        print(f"  {lab:<26} slope {slope(rd[m])}   big Q1 {big_line(rd[m])}")
    for lab, m in (("explosive vs poor D", (rd.ptype == "explosive") & (rd.defense == "poor")),
                   ("steady vs poor D", (rd.ptype == "steady") & (rd.defense == "poor"))):
        print(f"  {lab:<26}                               big Q1 {big_line(rd[m])}")
    b = rd[rd.big]
    for lab, m in (("big Q1 mostly ONE play", b.one_play), ("big Q1 from volume", ~b.one_play)):
        x = b[m]
        if len(x) >= 12:
            print(f"  {lab:<26} n={len(x):<4} rest vs norm {x.dr.mean():+6.1f}  touches {x.dro.mean():+5.2f}")
    print()
