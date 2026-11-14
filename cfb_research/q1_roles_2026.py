"""Does a big first quarter predict a big rest of the game?

For each player-game: Q1 output vs Q2-Q4 output, both measured against the
player's own average in his OTHER games that season (leave-one-out), so a star
having a normal day does not count as a "big" game.

Stats: QB passing yards, RB rushing yards, WR/TE receiving yards (+ usage).
"""
import sys
import duckdb
import numpy as np

DATA = sys.argv[1] if len(sys.argv) > 1 else "."
SEASONS = (2026,)
con = duckdb.connect()

parts = []
for y in SEASONS:
    parts.append(f"""
    select {y} season, game_id, period, pos_team, yards_gained, play_type, rush,
      rush_player_id, completion_player_id, incompletion_player_id, interception_thrown_player_id,
      reception_player_id, target_player_id, pos_team_score, def_pos_team_score, game_play_number
    from '{DATA}/pbp_{y}.parquet' where period between 1 and 4""")
con.execute("create table p as " + " union all ".join(parts))
con.execute("create table ros as " + " union all ".join(
    f"select {y} season, try_cast(athlete_id as bigint) pid, any_value(position_abbreviation) pos "
    f"from '{DATA}/ros_{y}.parquet' group by 2" for y in SEASONS))

# Team lead at the end of Q1 (from the team's point of view).
con.execute("""
create table q1score as
with last as (
  select season, game_id, pos_team, pos_team_score, def_pos_team_score,
         row_number() over (partition by season, game_id order by game_play_number desc) rn
  from p where period = 1 and pos_team_score is not null
)
select season, game_id, pos_team team, pos_team_score - def_pos_team_score lead from last where rn = 1
union all
select l.season, l.game_id, t.team, l.def_pos_team_score - l.pos_team_score
from last l join (select distinct season, game_id, pos_team team from p) t
  on t.season = l.season and t.game_id = l.game_id and t.team <> l.pos_team
where l.rn = 1
""")

con.execute("""
create table lines as
with l as (
  select season, game_id, pos_team team, period,
         try_cast(coalesce(completion_player_id, incompletion_player_id, interception_thrown_player_id) as bigint) pid,
         'pass' kind, 1 opp,
         case when play_type in ('Pass Reception','Passing Touchdown') then yards_gained else 0 end yds
  from p where coalesce(completion_player_id, incompletion_player_id, interception_thrown_player_id) is not null
  union all
  select season, game_id, pos_team, period, try_cast(rush_player_id as bigint), 'rush', 1, yards_gained
  from p where rush_player_id is not null and rush
  union all
  select season, game_id, pos_team, period,
         try_cast(coalesce(reception_player_id, target_player_id) as bigint), 'rec', 1,
         case when play_type in ('Pass Reception','Passing Touchdown') then yards_gained else 0 end
  from p where coalesce(reception_player_id, target_player_id) is not null
)
select l.season, l.game_id, any_value(l.team) team, l.pid, r.pos, l.kind,
  sum(yds) filter (period = 1) q1, sum(yds) filter (period > 1) rest,
  sum(opp) filter (period = 1) q1_opp, sum(opp) filter (period > 1) rest_opp
from l join ros r on r.season = l.season and r.pid = l.pid
where (r.pos = 'QB' and kind = 'pass') or (r.pos = 'RB' and kind = 'rush')
   or (r.pos in ('WR','TE') and kind = 'rec')
group by all
""")

# Leave-one-out baselines: the player's average in his other games that season.
con.execute("""
create table g as
with x as (
  select *, coalesce(q1,0) q1y, coalesce(rest,0) resty, coalesce(q1_opp,0) q1o, coalesce(rest_opp,0) resto,
    count(*) over w n, sum(coalesce(q1,0)) over w sq1, sum(coalesce(rest,0)) over w srest,
    sum(coalesce(q1_opp,0)) over w sq1o, sum(coalesce(rest_opp,0)) over w sresto
  from lines window w as (partition by season, pid, kind)
)
select x.season, x.game_id, x.team, x.pid, case when pos='TE' then 'TE' else pos end pos,
  q1y q1, resty rest, q1o, resto,
  (sq1 - q1y) / (n - 1) base_q1, (srest - resty) / (n - 1) base_rest,
  (sq1o - q1o) / (n - 1) base_q1o, (sresto - resto) / (n - 1) base_resto,
  n - 1 others, (sq1 + srest) / n season_avg, (sq1o + sresto) season_opp, n games, s.lead
from x left join q1score s on s.season = x.season and s.game_id = x.game_id and s.team = x.team
where n - 1 >= 3
""")


# Depth role: rank within team-season by total usage (carries / targets).
con.execute("""
create table r as
with pl as (
  select season, team, pid, pos, any_value(season_opp) opp from g
  where pos in ('RB','WR','TE') group by all
), rk as (
  select *, pos || row_number() over (partition by season, team, pos order by opp desc, pid) role from pl
)
select g.*, rk.role from g join rk using (season, team, pid, pos)
""")
rng = np.random.default_rng(7)
def boot(x, y, pid, reps=1000):
    ids = np.unique(pid); idx = {i: np.where(pid == i)[0] for i in ids}
    bs = [np.polyfit(x[t], y[t], 1)[0] for t in
          (np.concatenate([idx[i] for i in rng.choice(ids, len(ids))]) for _ in range(reps))]
    return np.polyfit(x, y, 1)[0], *np.percentile(bs, [2.5, 97.5])

ROLES = ["RB1", "RB2", "WR1", "WR2", "WR3", "TE1", "TE2"]
OPP = {"RB": "car", "WR": "tgt", "TE": "tgt"}
print(f"{'role':<4} {'games':>5} {'norm Q1':>7} {'norm rest':>9} | {'per Q1 yd':>9} {'95% CI':>15} | "
      f"{'big Q1 n':>8} {'Q1':>5} {'rest':>5} {'norm':>5} {'diff':>6} {'opp diff':>8} {'4xQ1':>5} | {'close':>12} {'lead14+':>12}")
out = []
for role in ROLES:
    df = con.sql(f"select * from r where role = '{role}'").df()
    pos = role[:2]
    dq1 = (df.q1 - df.base_q1).to_numpy(); dr = (df.rest - df.base_rest).to_numpy()
    s, lo, hi = boot(dq1, dr, df.pid.to_numpy())
    # big Q1 = top 15% of Q1 relative to his own norm, within this role
    cut = np.percentile(dq1, 85)
    b = df[dq1 >= cut]
    def split(m):
        x = b[m]
        return f"{(x.rest - x.base_rest).mean():+5.1f} n={len(x):<3}" if len(x) >= 8 else "    -       "
    print(f"{role:<4} {len(df):>5} {df.base_q1.mean():>7.1f} {df.base_rest.mean():>9.1f} | {s:>+9.2f} {f'{lo:+.2f} to {hi:+.2f}':>15} | "
          f"{len(b):>8} {b.q1.mean():>5.1f} {b.rest.mean():>5.1f} {b.base_rest.mean():>5.1f} {(b.rest - b.base_rest).mean():>+6.1f} "
          f"{(b.resto - b.base_resto).mean():>+8.2f} {4 * b.q1.mean():>5.0f} | {split(b.lead.abs() <= 13):>12} {split(b.lead >= 14):>12}")
    # hit rate: rest of game beats his norm?
    out.append((role, len(b), (b.rest > b.base_rest).mean(), (df.rest > df.base_rest).mean()))
print()
print("How often the rest of the game beat his norm:  after a big Q1  vs  all his games")
for role, n, hb, ha in out:
    print(f"  {role}: {hb:5.0%} (n={n})  vs  {ha:5.0%}")
# Absolute thresholds for backups (what you see on a live screen)
print()
for role, col, th in (("RB2", "rush yds", 30), ("WR2", "rec yds", 35), ("TE2", "rec yds", 20), ("WR3", "rec yds", 30)):
    df = con.sql(f"select * from r where role = '{role}' and q1 >= {th}").df()
    if len(df):
        print(f"{role} with {th}+ {col} in Q1: n={len(df)}, Q1 avg {df.q1.mean():.1f}, rest {df.rest.mean():.1f} vs norm {df.base_rest.mean():.1f} "
              f"({(df.rest - df.base_rest).mean():+.1f}), {OPP[role[:2]]} {(df.resto - df.base_resto).mean():+.2f}, beat norm {(df.rest > df.base_rest).mean():.0%}")
