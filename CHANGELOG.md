# Changelog

All notable changes to the PBP Reminder Bot are documented here.

Format: [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).
Versioning: [Semantic Versioning](https://semver.org/).

- **MAJOR** (x.0.0): Breaking config changes, workflow restructuring.
- **MINOR** (0.x.0): New commands, new features, new bot behaviours.
- **PATCH** (0.0.x): Bug fixes, test additions, refactors, documentation.

---

## [2.1.0] - 2026-02-26

### Summary
Scene markers and GM notes. GMs can now mark narrative scene boundaries
in transcripts and maintain persistent notes per campaign.

### Added — Scene markers
- **`/scene <name>`** (GM only): marks a scene boundary in the campaign's
  transcript file with a styled divider. Scene name stored in state and
  displayed in `/status` and `/campaign` output.
- Transcript entries: `### 🎭 Scene: <name>` with timestamp, surrounded
  by horizontal rules for clear visual separation.

### Added — GM notes
- **`/note <text>`** (GM only): adds a persistent note to the campaign.
  Max 20 notes per campaign. Timestamped on creation.
- **`/notes`** (everyone): view all GM notes for the current campaign,
  numbered with creation dates.
- **`/delnote <N>`** (GM only): delete a note by its number.
- Latest 3 notes shown in `/campaign` output with "see all" hint.

### Added
- 2 new daily tips (scene markers, GM notes).
- New state fields: `current_scenes`, `campaign_notes`.

### Tests
- 14 new tests: scene command, scene no-name, scene non-GM, scene in
  status, scene in campaign, note command, note no-text, note max limit,
  notes command, notes empty, delnote, delnote invalid, notes in campaign,
  write_scene_marker transcript.
- Total: 200 tests (37 helpers + 145 checker + 18 import).

---

## [2.0.0] - 2026-02-26

### Summary
Dashboard v2 and smart alerts. The GitHub Pages dashboard now has summary
cards, week filtering, sortable columns, and click-to-expand player
drill-downs. Smart alerts detect pace drops (>40% week-over-week) and
total silence (48h+ from everyone including GM).

### Added — Dashboard v2
- **Summary cards**: campaigns, weekly posts, active players, avg response gap.
- **Week filter**: dropdown to view any archived week's data.
- **Sortable columns**: click any table header to sort asc/desc.
- **Player drill-down**: click a campaign row to see per-player stats
  (posts, sessions, avg gap) for that week.
- **Health indicators**: colour-coded dots (green/yellow/orange/red) by
  weekly post volume.
- **Trend arrows**: week-over-week change shown with colour-coded percentages.
- **Mobile-responsive**: works on phone screens with adapted grid layout.

### Added — Player breakdown in archive
- `player_breakdown` field in `weekly_archive.json` stores per-player
  stats for each week: posts, sessions (unique days), and avg gap.
- Powers the dashboard drill-down feature.

### Added — Smart alerts
- **Pace drop detection**: if a campaign's posts drop >40% vs the
  previous week (minimum 5 posts/week baseline), a gentle alert is sent
  to the chat topic. Weekly cadence, won't spam.
- **Conversation dying**: if ALL participants (including GM) go silent
  for 48h+, a one-time alert fires. Resets automatically when anyone
  posts. Skips paused campaigns. Use `/pause` to silence during breaks.
- Both gated behind `smart_alerts` feature flag (enabled by default,
  disable per-campaign via `disabled_features`).

### Added — New daily tips
- Tip explaining smart alerts and how to silence them with `/pause`.
- Tip explaining the `/overview` command for cross-campaign monitoring.

---

## [1.9.0] - 2026-02-26

### Summary
Character awareness. Campaigns can now map player IDs to character names.
Characters appear in rosters, `/mystats`, `/party`, and transcripts.

### Added — `/party` command
- Shows the in-fiction party: character names, who plays them, activity status.
- Active vs inactive breakdown.
- Requires `characters` config on the campaign's topic_pair.

### Added — Character names throughout
- **Roster summaries**: player lines show "Alice (Cardigan)" when configured.
- **`/mystats`**: header shows "playing Cardigan" when configured.
- **Transcripts**: log entries show "**Alice** (Cardigan)" for player messages.
- Config field: `"characters": {"user_id": "Character Name"}` per campaign.

### Added
- `helpers.get_characters()` and `helpers.character_name()` lookup functions.
- New daily tip for `/party`.

### Tests
- 6 new tests: character_name helper, get_characters, party with/without
  characters, mystats with character, transcript with character.
- Total: 178 tests (37 helpers + 123 checker + 18 import).

---

## [1.8.0] - 2026-02-26

### Summary
Message milestone celebrations. The bot now celebrates every 500th PBP
message per campaign and every 5,000th message across all campaigns.

### Added — Message milestones
- Campaign milestones: every 500 messages (500, 1000, 1500, ...) posted
  to the campaign's chat topic with a unique icon per tier.
- Global milestones: every 5,000 messages across all campaigns, posted
  to the leaderboard topic.
- Tracked in `state["celebrated_milestones"]` to prevent duplicate posts.
- Icons progress: 🎯 → 🏅 → ⚡ → 🔥 → ⭐ → 💎 → 🌟 → 👑 → 🏆 → 🎆

### Added
- New daily tip for message milestones.
- Added to `_run_checks` scheduler.

### Note
Milestones are based on the bot's live message count (messages tracked
since the bot was deployed). Historical imports populate transcripts
but don't retroactively update the live counts. Milestones will fire
naturally as campaigns continue posting.

### Tests
- 4 new tests: campaign 500, not repeated, campaign 1000, global 5000.
- Total: 172 tests (37 helpers + 117 checker + 18 import).

---

## [1.7.0] - 2026-02-26

### Summary
New `/catchup` command shows players what happened since they last posted.
Perfect for PBP where you might come back after a few days to find 30+ new
messages across multiple people.

### Added — `/catchup` command
- Shows how many messages were posted since your last one and who posted them.
- Tells you if combat started while you were away (round, phase).
- Handles edge cases: no history, just posted, nobody posted since you.
- New daily tip for `/catchup`.
- Added to help text.

### Tests
- 5 new tests: no history, caught up, nobody posted, messages with counts,
  combat awareness.
- Total: 165 tests (37 helpers + 112 checker + 16 import).

---

## [1.6.0] - 2026-02-26

### Summary
GM roster management commands. GMs can now manually add and remove players
from campaign tracking without waiting for automatic processes.

### Added — `/kick` command (GM only)
- `/kick @username` or `/kick PlayerName` removes a player from this
  campaign's roster immediately.
- Player is moved to the removed list (same as auto-removal at 4 weeks).
- Kicked players can rejoin by posting in PBP again.
- Matches by username, first name, or full name (case-insensitive).

### Added — `/addplayer` command (GM only)
- `/addplayer @username Player Name` pre-registers a player on the roster
  before they've posted.
- Creates a placeholder entry that updates with full stats on first post.
- Prevents duplicates (checks existing roster).
- Clears any previous removal record for that player.

### Added
- 2 new daily tips for `/kick` and `/addplayer`.
- Help text updated with new commands.

### Tests
- 6 new tests: kick by username, kick by name, kick no match,
  addplayer, addplayer duplicate, addplayer clears removed.
- Total: 160 tests (37 helpers + 107 checker + 16 import).

---

## [1.5.0] - 2026-02-26

### Summary
Historical transcript backfill. A new import script reads Telegram Desktop
JSON exports and populates the transcript archive with all past PBP messages.
Also adds Theria (C08) to the tracked campaigns with per-campaign GM support.

### Added — History Import
- `scripts/import_history.py`: imports historical PBP messages from Telegram
  Desktop JSON exports into the same `data/pbp_logs/` format the live bot uses.
- Supports `--dry-run` to preview without writing files.
- Idempotent: tracks imported message IDs per campaign, safe to run repeatedly.
- Handles Telegram's mixed text/entity format, media detection, GM tagging.
- 16 tests for the import script.

### Added — Theria (C08)
- New campaign: PBP topic 107151, Chat topic 107141, started 2025-10-06.
- Disabled features: warnings, recruitment (not Lewis's campaign).
- Per-campaign `gm_user_ids` override: when a campaign has its own `gm_user_ids`
  in config, it replaces the global list for that campaign only. All 8 functions
  that check GM status now use per-campaign resolution.
- 3 new helper tests for `gm_ids_for_campaign`.

### Tests
- 16 new import tests + 3 new helper tests.
- Total: 154 tests (37 helpers + 101 checker + 16 import).
- CI updated to run import tests.

---

## [1.4.0] - 2026-02-26

### Summary
PBP transcript archiving. Every message in every PBP topic is now logged to
persistent markdown files in the repo — a complete, readable backup of every
campaign's story. If Telegram dies, the campaigns live on.

### Added — PBP Transcript Archive
- Every non-command message in every PBP topic is now appended to a monthly
  markdown transcript file at `data/pbp_logs/{CampaignName}/{YYYY-MM}.md`.
- Transcripts include: timestamp, player/GM name, role tag, message text.
- Media is logged with type markers: `*[image]*`, `*[sticker 😂]*`, `*[gif]*`,
  `*[video]*`, `*[voice message]*`, `*[document:filename.pdf]*`. Captions are
  preserved alongside media markers.
- An auto-generated `data/pbp_logs/README.md` index lists all campaigns with
  message counts and links to monthly log files.
- Files are committed to the repo hourly via GitHub Actions alongside the
  existing weekly archive.
- Only PBP topic messages are logged. Chat topics and bot commands are excluded.

### How It Works
The transcript files are standard markdown, readable directly on GitHub or any
markdown viewer. Each monthly file has a header and chronological entries:

```
# Doomsday Funtime — 2026-02

*PBP transcript archived by PathWarsNudge bot.*

---

**Alice** (2026-02-26 14:30:05):
I attack the goblin with my longsword!

**Lewis** [GM] (2026-02-26 14:32:10):
The goblin shrieks as the blade connects. Roll damage.

**Bob** (2026-02-26 14:35:22):
*[image]* battle map update
```

### Changed
- `_parse_message` now extracts media type (photo, sticker, gif, video, voice,
  document) and caption from Telegram messages.
- GitHub Actions workflow commit step updated to include transcript data.

### Tests
- 7 new tests: _sanitize_dirname, _format_log_entry (text, GM, image, sticker),
  _append_to_transcript (write + append), _parse_message media capture.
- All test suites redirected to temp directory for transcript writes.
- Total: 135 tests (34 helpers + 101 checker).

---

## [1.3.0] - 2026-02-26

### Summary
GM tools and personal history. GMs can now pause/resume inactivity tracking
for breaks between arcs or holidays. Players can view their 8-week posting
history as a text sparkline chart.

### Added — New Commands
- **/myhistory**: Shows a text sparkline of your weekly post counts over
  the last 8 weeks. Includes total posts, peak week, current week, and
  trend direction. The sparkline uses Unicode block characters (▁▂▃▄▅▆▇█)
  for a compact visual at-a-glance view of posting patterns.
- **/pause [reason]** (GM only): Pauses inactivity tracking for the campaign.
  All topic alerts and player warnings are suppressed while paused. The
  pause reason is shown in `/status` and `/campaign`. Use for planned breaks,
  holidays, or between-arc downtime. Non-GMs cannot use this command.
- **/resume** (GM only): Resumes inactivity tracking after a pause. Confirms
  in chat when tracking is re-enabled.

### Changed
- `/status` and `/campaign` now show ⏸️ PAUSED with reason when a campaign
  is paused.
- `check_and_alert` and `check_player_activity` both skip paused campaigns.
- 2 new daily tips added (covering /myhistory and /pause).
- Help text updated with all new commands.

### Tests
- 13 new tests: sparkline (3), myhistory (3), /pause command (2),
  /resume command (1), pause blocking (2), pause display (2).
- Total: 128 tests (34 helpers + 94 checker).

---

## [1.2.0] - 2026-02-26

### Summary
Streaks, celebrations, and cross-campaign intelligence. The bot now celebrates
posting milestones, shows streaks in rosters and leaderboards, and posts a
compact weekly digest with health-scored campaign summaries.

### Added — Streak Milestones
- The bot automatically celebrates when a player crosses a streak milestone:
  7, 14, 30, 60, or 90 consecutive days of posting. Each milestone has a
  unique message (scaling from 🔥 to 👑). Milestones are tracked per player
  per campaign and never posted twice for the same milestone. The streak must
  be continuous — missing a single day resets it.

### Added — Streak in Roster & Leaderboard
- **Roster**: Each player's entry now shows their current streak with a 🔥
  emoji if 2+ days. Adds one line to roster blocks only when relevant.
- **Leaderboard**: New "🔥 Longest Active Streaks" section at the bottom of
  the weekly leaderboard. Shows top 5 players across all campaigns, with
  streak length and campaign name.

### Added — Weekly Digest
- A compact one-line-per-campaign newsletter posted to the leaderboard topic
  once per week. Each line shows: health icon (🟢🟡🟠🔴 based on post volume),
  campaign name, post count with trend arrow, party size, active combat flag,
  and the week's MVP (most active player). Includes a colour legend.
  Designed to be scannable in under 10 seconds.
- Health scoring: 🟢 = 20+ posts/week, 🟡 = 10-19, 🟠 = 5-9, 🔴 = under 5.

### Changed
- `_gather_leaderboard_stats` now returns a 3-tuple including streak data.
- `_format_leaderboard` accepts optional `streaks` parameter.
- `_roster_user_stats` return dict now includes `streak` field.
- `_roster_block` displays streak when ≥ 2 days.
- `_run_checks` now includes streak milestones (14 scheduled checks total).

### Tests
- 8 new tests: streak milestones (3), weekly digest (2), leaderboard streaks (1),
  roster streak display (2).
- Total: 115 tests (34 helpers + 81 checker).

---

## [1.1.0] - 2026-02-26

### Summary
Player self-service update. Three new commands let players check their own stats,
inspect combat status, and discover features through daily tips. Plus a roadmap,
versioning pipeline, and 20 new tests.

### Added — New Commands
- **/mystats** (alias: **/me**): Players type `/mystats` in any PBP topic to see
  their personal stats: total posts, posting sessions, average gap between posts,
  weekly activity count, last post time, and current posting streak. Works for both
  players and GMs. No need to wait for roster day — check any time.
- **/whosturn**: Anyone can check combat status on demand. Shows: current round,
  whose phase it is (players/enemies), who has already acted (✅), and who the party
  is waiting on (⏳). During enemy phase, shows "Waiting for GM." Works outside the
  ping timer schedule so players can check without waiting for the automatic ping.

### Added — Daily Tips
- The bot now posts one random tip per day to a randomly chosen PBP chat topic.
  Each tip explains a bot feature (commands, combat tracking, POTW, streaks, etc).
  Tips rotate through all 12 entries before repeating, so every feature gets explained.
  This helps players who don't read GitHub or the issues topic discover what the bot
  can do. Tips are posted with HTML formatting for readability.

### Added — Posting Streaks
- The bot now tracks consecutive days with posts and displays the streak in `/mystats`.
  A "streak" means posting at least once per day with no gaps. Posts yesterday count
  as maintaining the streak. Streak resets if you miss a day. Shows 🔥 emoji for
  streaks of 2+ days.

### Added — Infrastructure
- **ROADMAP.md**: Full feature roadmap through v1.4.0+ with planned features
  (streaks leaderboard, weekly digest, campaign health scoring, dashboard improvements,
  GM tools, smart alerts, character awareness, AI summaries) and status tracking.
- **Changelog notifications**: When CHANGELOG.md is pushed, the `changelog-notify.yml`
  workflow posts the latest entry (formatted as Telegram HTML) to the Foundry & GitHub
  topic (https://t.me/Path_Wars/71537). Uses `post_changelog.py` which parses markdown,
  converts bold/italic/code/headers to HTML tags, and splits messages if they exceed
  Telegram's 4096 char limit.
- **VERSION file**: Semver-based version tracking. MAJOR = breaking config changes,
  MINOR = new features/commands, PATCH = fixes/tests/docs.

### Changed
- `telegram.py`: `send_message()` now accepts optional `parse_mode` parameter for
  HTML-formatted messages (used by daily tips).
- Help text updated with `/mystats`, `/me`, `/whosturn`, and daily tips.

### Tests
- 20 new tests: _build_mystats (4), _calc_streak (5), _build_whosturn (4),
  /whosturn command (1), /mystats command (2), daily tips (4).
- Total: 107 tests (34 helpers + 73 checker).

---

## [1.0.0] - 2026-02-26

### Summary
First versioned release. Consolidates all prior refactoring work (sessions 1–4)
plus today's new features into a stable, tested baseline.

### Added — New Features
- **/campaign command**: Type `/campaign` in any PBP topic to get a full scoreboard:
  campaign age, party size, weekly pace with trend arrows, complete roster with
  per-player stats (total posts, sessions, weekly count, average gap, last post),
  at-risk player warnings, and active combat state. This replaces the need to wait
  for scheduled roster/pace reports — players can check on demand.
- **/status command**: Quick health snapshot — party size, last post time, posts
  this week, at-risk players, combat state.
- **/help command**: Lists all bot features and GM commands in-chat.
- **Per-campaign feature toggles**: Add `"disabled_features": ["potw", "recruitment"]`
  to any campaign in config to turn off specific features per campaign. Valid toggles:
  alerts, warnings, roster, potw, pace, recruitment, combat, anniversary.
- **Config validation on startup**: Bot checks config structure before running —
  catches bad group_id, duplicate topic IDs, unknown feature names, malformed dates.
  Errors prevent the run; warnings are logged but continue.
- **Archive dashboard** (docs/index.html): Interactive web dashboard for
  weekly_archive.json. Line charts for posts per week, GM vs player splits, response
  gap trends, and a sortable campaign comparison table. Dark RPG-themed design.
  Works on GitHub Pages or locally.
- **Changelog notifications**: Bot posts release notes to the Foundry & GitHub
  topic automatically after each push.
- **Versioning**: Semver-based VERSION file and CHANGELOG.md.

### Added — Code Quality
- **87 tests** (32 helpers, 55 checker) covering: message parsing, combat state
  machine, boon selection/expiry, player warnings and removal, leaderboard stats,
  anniversary detection, recruitment checks, feature toggles, config validation,
  pace calculations, roster formatting, and all helper utilities.
- **CI test gate**: Tests run before the checker in GitHub Actions. If tests fail,
  the checker doesn't execute.
- **Extracted `_parse_message`**: Message validation and field extraction pulled out
  of `process_updates`, reducing the main loop from 111 to 75 lines.
- **Extracted `pace_split` helper**: Deduplicated GM/player weekly post split logic
  used by both `/campaign` and pace reports.
- **Shared timestamps**: All 11 per-run features now receive identical `now` and
  `maps` objects, eliminating 13 redundant `datetime.now()` calls per run.

### Removed
- `pbp_summary_feature.py`: Unused 206-line AI summary prototype. The `/campaign`
  command now fills this role without requiring an API key.

### Architecture (for reference — pre-v1.0.0 refactoring)
The codebase was restructured across 4 sessions from a single 1,200-line file into:
- `checker.py` (1,468 lines, 27 functions): All bot features and orchestration.
- `helpers.py` (418 lines, 28 functions): Pure utilities, constants, config loading.
- `telegram.py` (105 lines, 7 functions): Telegram Bot API wrapper.
- `state.py` (103 lines, 3 functions): Gist-backed state persistence.
- `test_helpers.py` (314 lines, 32 tests): Helper function test suite.
- `test_checker.py` (1,069 lines, 55 tests): Checker integration and unit tests.
- `docs/index.html` (411 lines): Archive dashboard.

Every function has docstrings and return type hints. Max nesting: 4 levels.
All settings are configurable via `config.json` with sensible defaults.
