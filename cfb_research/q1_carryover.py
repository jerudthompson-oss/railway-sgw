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
SEASONS = (2024, 2025, 2026)
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
  n - 1 others, (sq1 + srest) / n season_avg, s.lead
from x left join q1score s on s.season = x.season and s.game_id = x.game_id and s.team = x.team
where n - 1 >= 4
""")

# Only players who matter for props: a real role (baseline full-game output).
MIN_BASE = {"QB": 150, "RB": 45, "WR": 35, "TE": 25}
BIG = {"QB": 100, "RB": 50, "WR": 45, "TE": 35}   # "big Q1" absolute thresholds
STAT = {"QB": "pass yds", "RB": "rush yds", "WR": "rec yds", "TE": "rec yds"}
OPP = {"QB": "att", "RB": "car", "WR": "tgt", "TE": "tgt"}
rng = np.random.default_rng(7)

def boot_slope(x, y, pid, reps=1000):
    """OLS slope with a bootstrap over players (a player's games are not independent)."""
    ids = np.unique(pid)
    idx = {i: np.where(pid == i)[0] for i in ids}
    slope = np.polyfit(x, y, 1)[0]
    bs = []
    for _ in range(reps):
        take = np.concatenate([idx[i] for i in rng.choice(ids, len(ids))])
        bs.append(np.polyfit(x[take], y[take], 1)[0])
    lo, hi = np.percentile(bs, [2.5, 97.5])
    return slope, lo, hi

for pos in ("QB", "RB", "WR", "TE"):
    df = con.sql(f"""select * from g where pos = '{pos}'
                     and season_avg >= {MIN_BASE[pos]}""").df()
    if df.empty:
        continue
    d_q1 = (df.q1 - df.base_q1).to_numpy()
    d_rest = (df.rest - df.base_rest).to_numpy()
    d_reso = (df.resto - df.base_resto).to_numpy()
    pid = df.pid.to_numpy()
    for yr in SEASONS:
        m = (df.season == yr).to_numpy()
        big_m = m & (df.q1 >= BIG[pos]).to_numpy()
        print(f"   {yr}: slope {np.polyfit(d_q1[m], d_rest[m], 1)[0]:+.2f}  big-Q1 rest vs norm {d_rest[big_m].mean():+.1f} (n={big_m.sum()})")
    s, lo, hi = boot_slope(d_q1, d_rest, pid)
    so, olo, ohi = boot_slope(d_q1, d_reso, pid, reps=300)
    print(f"\n==== {pos} ({STAT[pos]}) — {len(df):,} player-games, {df.pid.nunique():,} players ====")
    print(f"Overall rest vs norm (should be ~0): {d_rest.mean():+.1f} yds")
    print(f"Every extra Q1 yard above his norm -> {s:+.2f} rest-of-game yards (95% CI {lo:+.2f} to {hi:+.2f})")
    print(f"                                   -> {so:+.3f} rest-of-game {OPP[pos]} (95% CI {olo:+.3f} to {ohi:+.3f})")
    # Bins by Q1 relative to his norm.
    df["bin"] = np.digitize(d_q1, [-20, -5, 10, 25, 50]) if pos != "QB" else np.digitize(d_q1, [-40, -10, 20, 50, 90])
    labels = (["<-20", "-20..-5", "-5..+10", "+10..+25", "+25..+50", ">+50"] if pos != "QB"
              else ["<-40", "-40..-10", "-10..+20", "+20..+50", "+50..+90", ">+90"])
    print(f"{'Q1 vs norm':>10} {'games':>6} {'Q1':>6} {'norm rest':>9} {'actual rest':>11} {'rest vs norm':>12} {'3xQ1 pace':>9} {'rest opp vs norm':>16}")
    for b, lab in enumerate(labels):
        m = df["bin"] == b
        if m.sum() < 20:
            continue
        sub = df[m]
        print(f"{lab:>10} {m.sum():>6} {sub.q1.mean():>6.1f} {sub.base_rest.mean():>9.1f} {sub.rest.mean():>11.1f} "
              f"{(sub.rest - sub.base_rest).mean():>+12.1f} {3 * sub.q1.mean():>9.1f} {(sub.resto - sub.base_resto).mean():>+16.2f}")
    big = df[df.q1 >= BIG[pos]]
    if len(big):
        print(f"Big Q1 (>= {BIG[pos]} {STAT[pos]}): {len(big)} games. Rest of game {big.rest.mean():.1f} vs his norm "
              f"{big.base_rest.mean():.1f} ({(big.rest - big.base_rest).mean():+.1f}); a 3x-Q1 pace would say {3 * big.q1.mean():.1f}.")
        for name, m in (("team led by 14+ after Q1", big.lead >= 14), ("within 13 either way", big.lead.abs() <= 13),
                        ("team trailed by 14+", big.lead <= -14)):
            sub = big[m]
            if len(sub) >= 10:
                print(f"   {name:<26} n={len(sub):>4}  rest vs norm {(sub.rest - sub.base_rest).mean():+6.1f} yds, "
                      f"{(sub.resto - sub.base_resto).mean():+5.2f} {OPP[pos]}")
