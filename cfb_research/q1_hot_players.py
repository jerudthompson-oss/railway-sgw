"""Hot players: does recent form predict the game, and does a big Q1 carry over more?

    python q1_hot_players.py DATA_DIR [SEASON ...]     (default 2026)

Everything is point-in-time: a player's norm is his average over the games he
played BEFORE this one in the same season (at least 3), which is all a bettor
would know at kickoff.

  hot (yards)     his last 3 games averaged >= 125% of his norm
  cold (yards)    his last 3 games averaged <= 75% of his norm
  rising usage    his last 3 games averaged >= 125% of his normal carries/targets
  falling usage   <= 75%

Roles (RB1/RB2, WR1-3, TE1/TE2) are ranked by season usage within each team.
Big Q1 = his top 15% of first quarters relative to his norm, within role.
"""
import sys
import duckdb
import numpy as np

DATA = sys.argv[1] if len(sys.argv) > 1 else "."
SEASONS = tuple(int(a) for a in sys.argv[2:]) or (2026,)
con = duckdb.connect()

con.execute("create table p as " + " union all ".join(f"""
  select {y} season, game_id, period, pos_team, yards_gained, play_type, rush,
    rush_player_id, reception_player_id, target_player_id,
    pos_team_score, def_pos_team_score, game_play_number,
    coalesce(try_cast(start_date as varchar), try_cast(wallclock as varchar)) ts
  from '{DATA}/pbp_{y}.parquet' where period between 1 and 4""" for y in SEASONS))
con.execute("create table ros as " + " union all ".join(
    f"select {y} season, try_cast(athlete_id as bigint) pid, any_value(position_abbreviation) pos "
    f"from '{DATA}/ros_{y}.parquet' group by 2" for y in SEASONS))
con.execute("create table gdate as select season, game_id, min(ts) ts from p group by all")

con.execute("""
create table pg as
with plays as (
  select season, game_id, period, pos_team team, try_cast(rush_player_id as bigint) pid, 'RB' grp, yards_gained yds
  from p where rush and rush_player_id is not null
  union all
  select season, game_id, period, pos_team, try_cast(coalesce(reception_player_id, target_player_id) as bigint), 'REC',
    case when play_type in ('Pass Reception','Passing Touchdown') then yards_gained else 0 end
  from p where coalesce(reception_player_id, target_player_id) is not null
)
select pl.season, pl.game_id, any_value(pl.team) team, pl.pid, r.pos,
  coalesce(sum(yds) filter (period = 1), 0) q1, coalesce(sum(yds) filter (period > 1), 0) rest,
  count(*) filter (period = 1) q1o, count(*) filter (period > 1) resto,
  sum(yds) tot, count(*) opp
from plays pl join ros r on r.season = pl.season and r.pid = pl.pid
where (r.pos = 'RB' and grp = 'RB') or (r.pos in ('WR','TE') and grp = 'REC')
group by pl.season, pl.game_id, pl.pid, r.pos
""")

# Point-in-time norms and last-3 form.
con.execute("""
create table g as
with x as (
  select pg.*, d.ts,
    count(*) over prior n_prior,
    avg(q1) over prior norm_q1, avg(rest) over prior norm_rest, avg(tot) over prior norm_tot,
    avg(resto) over prior norm_resto, avg(opp) over prior norm_opp,
    avg(tot) over last3 l3_tot, avg(opp) over last3 l3_opp,
    sum(opp) over (partition by pg.season, pid) season_opp
  from pg join gdate d using (season, game_id)
  window prior as (partition by pg.season, pid order by d.ts rows between unbounded preceding and 1 preceding),
         last3 as (partition by pg.season, pid order by d.ts rows between 3 preceding and 1 preceding)
)
select * from x where n_prior >= 3
""")

con.execute("""
create table q1score as
with last as (
  select season, game_id, pos_team, pos_team_score - def_pos_team_score lead,
    row_number() over (partition by season, game_id order by game_play_number desc) rn
  from p where period = 1 and pos_team_score is not null
), teams as (select distinct season, game_id, pos_team team from p)
select t.season, t.game_id, t.team, case when t.team = l.pos_team then l.lead else -l.lead end lead
from teams t join last l on l.season = t.season and l.game_id = t.game_id and l.rn = 1
""")

con.execute("""
create table r as
with pl as (select season, team, pid, pos, any_value(season_opp) o from g group by all),
rk as (select *, pos || row_number() over (partition by season, team, pos order by o desc, pid) role from pl)
select g.*, rk.role, s.lead from g join rk using (season, team, pid, pos)
left join q1score s on s.season = g.season and s.game_id = g.game_id and s.team = g.team
""")

df = con.sql("select * from r where role in ('RB1','RB2','WR1','WR2','WR3','TE1','TE2')").df()
df = df[df.norm_tot >= 5]  # ignore players with almost no production so far
df["form"] = np.where(df.l3_tot >= 1.25 * df.norm_tot, "hot",
              np.where(df.l3_tot <= 0.75 * df.norm_tot, "cold", "normal"))
df["usage"] = np.where(df.l3_opp >= 1.25 * df.norm_opp, "rising",
               np.where(df.l3_opp <= 0.75 * df.norm_opp, "falling", "steady"))
df["dq1"] = df.q1 - df.norm_q1
df["dr"] = df.rest - df.norm_rest
df["dtot"] = df.tot - df.norm_tot
df["dro"] = df.resto - df.norm_resto
df["big"] = False
for role in df.role.unique():
    m = df.role == role
    df.loc[m, "big"] = df.loc[m, "dq1"] >= df.loc[m, "dq1"].quantile(0.85)

rng = np.random.default_rng(7)
def ci(vals, pid, reps=500):
    """Mean with a 95% bootstrap interval over players."""
    vals, pid = np.asarray(vals), np.asarray(pid)
    if len(vals) < 15:
        return None
    ids = np.unique(pid); idx = {i: np.where(pid == i)[0] for i in ids}
    bs = [vals[np.concatenate([idx[i] for i in rng.choice(ids, len(ids))])].mean() for _ in range(reps)]
    lo, hi = np.percentile(bs, [2.5, 97.5])
    return vals.mean(), lo, hi

def fmt(sub, col, base):
    r = ci(sub[col], sub.pid)
    if r is None:
        return f"n={len(sub):<4}         -          "
    m, lo, hi = r
    star = "*" if lo > 0 or hi < 0 else " "
    pct = m / sub[base].mean() if sub[base].mean() else 0
    return f"n={len(sub):<4} {m:+6.1f} ({pct:+4.0%}){star} [{lo:+.1f},{hi:+.1f}]"

print(f"Seasons {SEASONS}. All comparisons vs his point-in-time norm. * = 95% interval excludes zero.\n")
for role in ["RB1", "RB2", "WR1", "WR2", "WR3", "TE1", "TE2"]:
    rd = df[df.role == role]
    print(f"===== {role}  ({len(rd)} games; norm game {rd.norm_tot.mean():.1f} yds, norm rest-of-game {rd.norm_rest.mean():.1f}) =====")
    print("  A) Does form predict the WHOLE game?        game yds vs norm")
    for lab, m in (("hot (last 3 >= 125%)", rd.form == "hot"), ("normal", rd.form == "normal"),
                   ("cold (last 3 <= 75%)", rd.form == "cold"),
                   ("rising usage", rd.usage == "rising"), ("falling usage", rd.usage == "falling")):
        print(f"     {lab:<26} {fmt(rd[m], 'dtot', 'norm_tot')}")
    b = rd[rd.big]
    print("  B) After a BIG Q1, rest of game vs norm      (rest-of-game yds)")
    for lab, m in (("all big Q1", b.form.notna()), ("big Q1 + hot", b.form == "hot"),
                   ("big Q1 + normal form", b.form == "normal"), ("big Q1 + cold", b.form == "cold"),
                   ("big Q1 + rising usage", b.usage == "rising"), ("big Q1 + steady usage", b.usage == "steady"),
                   ("big Q1 + hot + close game", (b.form == "hot") & (b.lead.abs() <= 13))):
        x = b[m]
        extra = f"  touches {x.dro.mean():+.2f}" if len(x) >= 15 else ""
        print(f"     {lab:<26} {fmt(x, 'dr', 'norm_rest')}{extra}")
    print()
