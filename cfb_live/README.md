# CFB Live Players

A live web page showing every QB, RB, WR and TE stat line in all live college
football games. It refreshes itself, highlights rows whose numbers just
changed, and lists touchdowns and yardage milestones as they happen.

Data comes from ESPN's public scoreboard feed. No API keys, no cost.

## Deploy on Railway

1. In your Railway project: **New** → **GitHub Repo** → pick this repo.
2. Service **Settings** → **Root Directory** → `cfb_live`.
3. **Settings** → **Networking** → **Generate Domain**. Open that URL.

## RB2 alerts

When the second quarter starts, any team's RB2 (second in carries this season)
with 30+ rushing yards in the first quarter gets an alert card with:

- his Q1 carries, yards and longest run
- a projected rest-of-game and full-game rushing total
- a grade (strong / moderate / weak) from what the research found matters:
  steady vs explosive back, the opponent's run defense, the score after Q1,
  and whether the yards came on volume or one long run

Tap **Notify me** on the page to also get a phone/browser notification while
the page is open. Depth charts and defense ratings refresh every 12 hours from
free sportsdataverse files. The tracker has to be running during a game's first
quarter to alert on it.

It is a separate service from the SGW worker and shares no code or variables.

## Optional variables

| Variable | Default | Meaning |
|---|---|---|
| `POLL_SECONDS` | `20` | How often live games are re-read |
| `GROUPS` | `80` | ESPN groups: `80` is FBS, `80,81` adds FCS |
| `SEASON` | current year | Roster season used for player positions |
| `RB2_Q1_YARDS` | `30` | First-quarter rushing yards that trigger an RB2 alert |

## Run locally

    pip install -r requirements.txt
    python app.py        # then open http://localhost:8080
