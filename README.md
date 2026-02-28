# PBP Reminder Bot

A free, serverless bot for **play-by-post tabletop RPG groups on Telegram**.
Runs hourly via GitHub Actions, tracks activity across campaigns, and posts
summaries, nudges and awards directly into your group topics.

No hosting, no server, no cost. Just a GitHub repo, a Telegram bot, and a config file.

---

## Features

| Feature                    | Frequency                         | Where it posts             |
|----------------------------|-----------------------------------|----------------------------|
| **Inactivity alerts**      | Every N hours of silence          | Campaign chat topics       |
| **Player warnings**        | At 1, 2, 3 weeks inactive         | Campaign chat topics       |
| **Auto-removal**           | At 4 weeks inactive               | Campaign chat topics       |
| **Party roster**           | Every 3 days                      | Campaign chat topics       |
| **Player of the Week**     | Weekly                            | Campaign chat topics       |
| **Pace report**            | Weekly                            | Campaign chat topics       |
| **Campaign leaderboard**   | Every 3 days                      | Dedicated topic            |
| **Combat turn pinger**     | During combat                     | Campaign PBP topics        |
| **Recruitment notices**    | Every 2 weeks (if under capacity) | Campaign chat topics       |
| **Campaign anniversaries** | Yearly                            | Campaign chat topics       |
| **Weekly archive**         | Weekly                            | `data/weekly_archive.json` |

All intervals are configurable. All features run automatically once set up.

---

## Example output

**Inactivity alert:**
```
No new posts in Grand Explorers PBP for 1d 6h.
Last post was from Tyler Link (42 total posts) on 2026-02-20.
```

**Party roster:**
```
Party roster for Riddleport:

GM
- 60 posts total.
- 12 posting sessions.
- 9 posts in the last week.
- Average gap between posting: 21.4 hours.
- Last post: today (2026-02-24).

Lunnes
- @LuNneS_B.
- 15 posts total.
- 6 posting sessions.
- 4 posts in the last week.
- Average gap between posting: 47.5 hours.
- Last post: today (2026-02-24).

Party size: 5/6.
Riddleport needs 1 more player!
```

**Player of the Week:**
```
Player of the Week for Riddleport: Lunnes (@LuNneS_B)!
(2026-02-17 to 2026-02-24)

6 posts this week with an average gap of 18.3h between posts.
The most consistent driver of the story.

Choose your boon:
1. A stray cat follows you and hisses at anyone who lies to you.
2. You find a coin in your boot that wasn't there before.
3. The next innkeeper insists your money is no good here.
4. +1 circumstance bonus on your next skill check.
```

---

## How it works

```
GitHub Actions (hourly cron)
    |
    v
checker.py  -->  Telegram Bot API (fetch messages, send alerts)
    |
    v
GitHub Gist (persisted state between runs)
```

The bot expects a Telegram supergroup with **forum topics** enabled.
Each campaign needs two topics: a **PBP topic** (where the game happens)
and a **Chat topic** (where the bot posts summaries and alerts).

Posts within 10 minutes of each other are treated as a single "posting session"
so rapid back-and-forth doesn't inflate counts.

Campaigns can have multiple PBP topics (e.g. if you split scenes across threads).
The bot merges them under one canonical ID for all tracking.

---

## Setup

Takes about 15 minutes.

### 1. Create a Telegram bot

1. Message **@BotFather** on Telegram.
2. Send `/newbot`, follow the prompts, copy the **bot token**.
3. Send `/setprivacy`, select your bot, set to **Disable**
   (so it can read all messages, not just `/commands`).
4. Add the bot to your supergroup.
5. Make it an **admin** (needs: Read Messages, Send Messages).

### 2. Find your topic IDs

Open this URL in a browser (replace `YOUR_TOKEN`):
```
https://api.telegram.org/botYOUR_TOKEN/getUpdates
```

Send one message in each PBP topic and each Chat topic, then refresh.
You'll see JSON like:
```json
{
  "message": {
    "chat": { "id": -1001234567890 },
    "message_thread_id": 12345
  }
}
```

Note down:
- `chat.id` is your **group_id** (same for all topics).
- `message_thread_id` is the **topic ID** (unique per topic).
- Your Telegram **user ID** (visible in the `from.id` field). This is your GM ID.

### 3. Create a GitHub Gist

1. Go to [gist.github.com](https://gist.github.com).
2. Create a gist with filename `pbp_state.json` and content `{}`.
3. Save it. Copy the **Gist ID** from the URL.

### 4. Create a GitHub token

1. Go to [github.com/settings/tokens](https://github.com/settings/tokens).
2. Generate a classic token with the **gist** scope only.
3. Copy the token.

### 5. Fork or create the repo

1. Fork this repo (or create a new one and copy the files).
2. Go to **Settings > Secrets and variables > Actions** and add:

| Secret               | Value                      |
|----------------------|----------------------------|
| `TELEGRAM_BOT_TOKEN` | Bot token from BotFather   |
| `GIST_TOKEN`         | GitHub PAT with gist scope |
| `GIST_ID`            | Gist ID from step 3        |

### 6. Configure

Copy `config.example.json` to `config.json` and fill it in:

```json
{
    "group_id": -1001234567890,
    "alert_after_hours": 4,
    "gm_user_ids": [123456789],
    "leaderboard_topic_id": null,
    "topic_pairs": [
        {
            "name": "My Campaign",
            "chat_topic_id": 11111,
            "pbp_topic_ids": [22222],
            "created": "2025-01-15"
        }
    ]
}
```

Key fields:
- **group_id**: Your supergroup's chat ID (negative number).
- **gm_user_ids**: Array of GM Telegram user IDs. GMs are excluded from player stats.
- **leaderboard_topic_id**: Topic ID for the cross-campaign leaderboard (or `null` to disable).
- **topic_pairs**: One entry per campaign. Each needs a name, a chat topic, and one or more PBP topics.
- **created**: Campaign start date for anniversary alerts (optional, `YYYY-MM-DD`).
- **gm_user_ids** (per-campaign): Optional override that replaces the global GM list for this campaign only. Useful when a campaign has a different GM.
- **characters** (per-campaign): Optional mapping of `"user_id": "Character Name"`. Enables `/party` command and shows character names in rosters, stats, and transcripts.
- **disabled_features**: Optional list of features to disable for this campaign.
  Valid values: `alerts`, `warnings`, `roster`, `potw`, `pace`, `recruitment`, `combat`, `anniversary`, `smart_alerts`.

### 7. Add boons (optional)

The Player of the Week feature picks 3 random flavour boons from `boons.json`
plus 1 mechanical boon. Copy `boons.example.json` to `boons.json` and add your own,
or use the example as-is. Each entry is a plain string.

### 8. Test

Go to **Actions > PBP Inactivity Reminder > Run workflow**.
Check the logs. You should see:
```
Loaded state. Offset: 0 | Tracking 0 topics, 0 players
Received N new updates
Done
```

The bot will start tracking from this point. Features like rosters and POTW
will activate once enough data has accumulated.

---

## Configuration reference

All settings go in the `settings` block of `config.json`.
Every setting has a sensible default, so the entire block is optional.

| Setting                     | Default   | Description                                      |
|-----------------------------|-----------|--------------------------------------------------|
| `roster_interval_days`      | 3         | Days between party roster posts                  |
| `potw_interval_days`        | 7         | Days between Player of the Week awards           |
| `potw_min_posts`            | 5         | Minimum posting sessions to qualify for POTW     |
| `pace_interval_days`        | 7         | Days between pace comparison reports             |
| `leaderboard_interval_days` | 3         | Days between cross-campaign leaderboard          |
| `combat_ping_hours`         | 4         | Hours before pinging players who haven't acted   |
| `recruitment_interval_days` | 14        | Days between recruitment notices                 |
| `required_players`          | 6         | Target party size (triggers recruitment notices) |
| `post_session_minutes`      | 10        | Posts within this window count as one session    |
| `player_warn_weeks`         | [1, 2, 3] | Weeks of inactivity before each warning          |
| `player_remove_weeks`       | 4         | Weeks of inactivity before auto-removal          |

Top-level settings:

| Setting             | Default | Description                                    |
|---------------------|---------|------------------------------------------------|
| `alert_after_hours` | 4       | Hours of topic silence before inactivity alert |

---

## Commands

The bot responds to these commands in any monitored PBP topic:

**Everyone:**
- `/help` - List bot features and commands.
- `/status` - Campaign health snapshot: party size, last post, posts this week, at-risk players, current scene.
- `/campaign` - Full scoreboard: header, weekly pace with trends, complete roster with stats, at-risk warnings, combat state, scene, notes.
- `/mystats` (alias `/me`) - Your personal stats: total posts, sessions, average gap, weekly count, streak.
- `/myhistory` - 8-week posting sparkline with trend.
- `/whosturn` - Combat status: who has acted, who the party is waiting on.
- `/catchup` - What happened since your last post.
- `/overview` - Compact summary of all campaigns.
- `/party` - In-fiction party composition (requires character config).
- `/notes` - View GM notes for the current campaign.
- `/activity` - Posting patterns: busiest hours, days, and time blocks.
- `/profile @player` - Cross-campaign stats for any player.
- `/away [duration] [reason]` - Declare an absence (skips warnings/combat pings).
- `/back` - Return from absence.
- `/recap [N]` - Show last N transcript entries (default 10, max 25).
- `/roll <dice> [label]` - Roll dice (e.g. `1d20+5 Stealth`, `4d6kh3`).
- `/quests` - View active and completed quest objectives.

### GM commands

- `/round <N> players` - Start round N, players' turn.
- `/round <N> enemies` - Start round N, enemies' turn.
- `/endcombat` - End combat tracking.
- `/pause [reason]` - Pause inactivity tracking (for breaks, holidays, between arcs).
- `/resume` - Resume inactivity tracking.
- `/kick @player` - Remove a player from tracking.
- `/addplayer @user Name` - Pre-register a player before they post.
- `/scene <n>` - Mark a scene boundary in the transcript.
- `/note <text>` - Add a persistent GM note (max 20 per campaign).
- `/delnote <N>` - Delete a GM note by number.
- `/quest <text>` - Add an active quest/objective.
- `/done <N>` - Mark quest N as completed.
- `/delquest <N>` - Delete quest N.
- `/gm` - GM dashboard: all campaigns at a glance.

## Versioning

The bot uses [Semantic Versioning](https://semver.org/). The current version is in `VERSION`.
All changes are documented in `CHANGELOG.md`.

When a new version is pushed, the changelog is automatically posted to the
[Foundry & GitHub](https://t.me/Path_Wars/71537) Telegram topic via the
`changelog-notify.yml` workflow.

Version bumps:
- **MAJOR** (x.0.0): Breaking config changes or workflow restructuring.
- **MINOR** (0.x.0): New commands, new features, new bot behaviours.
- **PATCH** (0.0.x): Bug fixes, test additions, refactors, documentation.

**GM only:**
- `/round 1 players` - Start round 1 player phase.
- `/round 1 enemies` - Start round 1 enemy phase.
- `/endcombat` - End combat tracking.

During the player phase, the bot tracks which players have posted.
After `combat_ping_hours` hours, it pings players who haven't acted yet.

---

## Multi-topic campaigns

If a campaign uses multiple PBP threads (e.g. split scenes), list them all:
```json
{
    "name": "My Campaign",
    "chat_topic_id": 11111,
    "pbp_topic_ids": [22222, 33333, 44444]
}
```
The first ID becomes the canonical ID. All posts across the listed topics
are merged for stats, rosters, POTW, and leaderboards.

---

## File structure

```
.github/workflows/
  pbp-reminder.yml      # Hourly cron job (tests + checker)
  changelog-notify.yml  # Posts changelog to Telegram on push
scripts/
  checker.py            # Main orchestrator (all features)
  helpers.py            # Utilities, formatting, topic maps
  telegram.py           # Telegram Bot API wrapper
  state.py              # Gist-based state persistence
  post_changelog.py     # Changelog parser and Telegram poster
  test_helpers.py       # Test suite for helpers (37 tests)
  test_checker.py       # Test suite for checker (123 tests)
  test_import_history.py # Test suite for import (18 tests)
  import_history.py     # Historical transcript backfill from Telegram export
config.json             # Your configuration
config.example.json     # Template configuration
boons.json              # Flavour boons for POTW (optional)
boons.example.json      # Sample boons file
docs/
  index.html            # Archive dashboard (Chart.js)
data/
  weekly_archive.json   # Auto-committed weekly stats archive
  pbp_logs/             # PBP transcript archive (monthly .md per campaign)
    README.md           # Auto-generated index of all transcripts
VERSION                 # Current semver version
CHANGELOG.md            # Release notes
```

---

## Importing Historical Messages

The bot only logs messages going forward. To backfill transcripts with
historical PBP messages, export the chat from Telegram Desktop and run
the import script.

### Steps

1. **Export from Telegram Desktop:**
   - Settings → Advanced → Export chat history
   - Select the Path_Wars supergroup
   - Format: **Machine-readable JSON**
   - Uncheck everything except "Text messages" (media metadata is preserved)
   - Click Export

2. **Run the import script:**
   ```bash
   # Preview what would be imported (no files written)
   python3 scripts/import_history.py path/to/result.json --dry-run

   # Actually import
   python3 scripts/import_history.py path/to/result.json
   ```

3. **Commit the transcripts:**
   ```bash
   git add data/pbp_logs
   git commit -m "Import historical PBP transcripts"
   git push
   ```

The script is idempotent — safe to run multiple times on the same export.
It tracks imported message IDs per campaign and only appends new ones.

---

## Troubleshooting

**Bot not seeing messages:**
Privacy mode must be disabled (`/setprivacy` > Disable via @BotFather)
and the bot must be a group admin.

**No updates showing:**
Send a message in a monitored topic after the bot is added, then check logs.

**Wrong topic IDs:**
Topic IDs are the `message_thread_id` field in the Telegram API response,
not the message ID. Each forum topic has a unique thread ID.

**GitHub Actions not running:**
Check the Actions tab for errors. Free tier allows 2,000 minutes/month;
this bot uses roughly 30 minutes/month.

**Features not activating:**
Most features need accumulated data. Rosters need posts with timestamps,
POTW needs a week of data, pace reports need two weeks. Give it time.

---

## Cost

Zero. GitHub Actions free tier gives 2,000 minutes/month.
This bot uses about 30 seconds per run, 720 runs/month = ~36 minutes.
The Gist and Telegram Bot API are also free.

---

## Archive Dashboard

The bot archives weekly stats to `data/weekly_archive.json`.
A built-in dashboard at `docs/index.html` visualizes this data with charts and tables.

To enable it via GitHub Pages:
1. Go to repo **Settings > Pages**.
2. Set source to **Deploy from a branch**, branch `main`, folder `/docs`.
3. Visit `https://yourusername.github.io/your-repo-name/`.

Or open `docs/index.html` locally (it fetches the JSON via relative path).
