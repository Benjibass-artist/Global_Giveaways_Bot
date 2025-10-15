# Global Giveaways Bot

A Discord bot that scans configured sources for real giveaways (gleam.io and wn.nr prioritized), posts them to a channel, avoids duplicates per channel, and cleans up expired posts. Admins can start/stop posting per channel and manage settings via slash commands.

## Features
- Daily background scan and cleanup loops (24h cadence).
- Filters for gleam.io/wn.nr while skipping utility pages.
- Per-channel de-duplication and persistent state of posted links.
- Slash commands:
	- /setchannel (admin) — set the channel for posts.
	- /start (admin) — start posting in current channel.
	- /stop (admin) — stop posting in current channel.
	- /scan (admin) — manual scan (per channel 1/day).
	- /clear (admin) — delete the bot’s posts in the configured channel.
	- /preview — show what the scraper finds (per channel 1/day).
	- /help — list available commands.

## Setup
```powershell
python -m venv .venv ; .\.venv\Scripts\Activate.ps1 ; pip install -r requirements.txt
```

Create a `.env` from `.env.example` and set your bot token:
```
DISCORD_TOKEN=YOUR_BOT_TOKEN
```

Optionally adjust paths and intervals in `.env`:
```
# SCAN_INTERVAL_MINUTES=12
# SOURCES_FILE=sources.json
# STATE_FILE=data/state.json
# CHANNELS_FILE=data/channels.json
```

Create `sources.json` with a list of pages that contain giveaway links:
```
{
	"sources": [
		"https://gleam.io/giveaways"
	]
}
```

## Run
```powershell
python bot.py
```

## Notes
- The bot stores runtime state in `data/` (gitignored).
- No privileged intents are required; all actions use slash commands and standard message sends.
