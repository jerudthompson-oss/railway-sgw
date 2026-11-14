# CFB research scripts

## q1_carryover.py — does a big first quarter carry over?

For every QB, RB, WR and TE game in 2024-2026, compares first-quarter output and
rest-of-game (Q2-Q4) output against the player's own average in his other games
that season. Data is free cfbfastR play-by-play from sportsdataverse-data.

    pip install duckdb numpy pandas
    # download pbp_{2024,2025,2026}.parquet and ros_{2024,2025,2026}.parquet
    # from github.com/sportsdataverse/sportsdataverse-data releases
    # (cfbfastR_cfb_pbp/play_by_play_YYYY.parquet, espn_cfb_rosters/cfb_rosters_YYYY.parquet)
    python q1_carryover.py path/to/data

## q1_roles_2026.py — the same test by depth role, 2026 only

Ranks each team's RBs by carries and WRs/TEs by targets (RB1/RB2, WR1-WR3, TE1/TE2),
then repeats the first-quarter carryover test for each role.

    python q1_roles_2026.py path/to/data   # needs pbp_2026.parquet and ros_2026.parquet

## q1_defense_explosive.py — by role, split by opponent defense and player type

Splits the carryover test by the opponent's defense (yards per carry or per pass
attempt allowed in its other games) and by how explosive the player is (share of
15+ yard runs / 20+ yard catches in his other games). Also compares big first
quarters built on one long play against ones built on volume.

    python q1_defense_explosive.py path/to/data 2026            # this season
    python q1_defense_explosive.py path/to/data 2024 2025 2026  # bigger sample
