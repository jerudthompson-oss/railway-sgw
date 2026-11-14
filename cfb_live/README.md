# CFB Live Players

A live web page showing every QB, RB, WR and TE stat line in all live college
football games. It refreshes itself, highlights rows whose numbers just
changed, and lists touchdowns and yardage milestones as they happen.

Data comes from ESPN's public scoreboard feed. No API keys, no cost.

## Deploy on Railway

1. In your Railway project: **New** → **GitHub Repo** → pick this repo.
2. Service **Settings** → **Root Directory** → `cfb_live`.
3. **Settings** → **Networking** → **Generate Domain**. Open that URL.

It is a separate service from the SGW worker and shares no code or variables.

## Optional variables

| Variable | Default | Meaning |
|---|---|---|
| `POLL_SECONDS` | `20` | How often live games are re-read |
| `GROUPS` | `80` | ESPN groups: `80` is FBS, `80,81` adds FCS |
| `SEASON` | current year | Roster season used for player positions |

## Run locally

    pip install -r requirements.txt
    python app.py        # then open http://localhost:8080
