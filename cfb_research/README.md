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
